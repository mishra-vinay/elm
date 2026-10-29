"""Instrument master: download Dhan's scrip master CSV into the `instruments` table and search it.

Source: https://images.dhan.co/api-data/api-scrip-master-detailed.csv (about 35 MB, refreshed daily by Dhan).
Only what a dry run can trade is kept: indices, cash equities (NSE/BSE), index/stock futures (NSE/BSE) and MCX
commodity futures. Options and currency rows are skipped by default to keep the database small.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime

import requests
from sqlalchemy import func, or_, select
from sqlalchemy.orm import sessionmaker

from .db import Instrument

MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
SEGMENT_MAP = {("NSE", "I"): "IDX_I", ("BSE", "I"): "IDX_I", ("NSE", "E"): "NSE_EQ", ("BSE", "E"): "BSE_EQ",
               ("NSE", "D"): "NSE_FNO", ("BSE", "D"): "BSE_FNO", ("MCX", "M"): "MCX_COMM",
               ("NSE", "C"): "NSE_CURRENCY", ("BSE", "C"): "BSE_CURRENCY"}
KEEP_INSTRUMENTS = {"INDEX", "EQUITY", "FUTIDX", "FUTSTK", "FUTCOM"}
DIRECT = re.compile(r"^\s*([A-Za-z_]+)\s*[:/ ]\s*(\d+)\s*$")
LABEL = re.compile(r"\[([A-Za-z_]+):(\d+)\]\s*$")      # the dropdown label: "Bajaj Finance [NSE_EQ:317]"

# Used only until the master is downloaded. Security ids come from Dhan's master; verify them after loading it.
SEED = [("IDX_I", "13", "INDEX", "NIFTY", "Nifty 50"), ("IDX_I", "25", "INDEX", "BANKNIFTY", "Nifty Bank"),
        ("IDX_I", "51", "INDEX", "SENSEX", "BSE Sensex"), ("NSE_EQ", "317", "EQUITY", "BAJFINANCE", "Bajaj Finance"),
        ("NSE_EQ", "1333", "EQUITY", "HDFCBANK", "HDFC Bank"), ("NSE_EQ", "2885", "EQUITY", "RELIANCE", "Reliance Industries")]


def _f(x, default=None):
    try:
        v = float(x)
        return v
    except (TypeError, ValueError):
        return default


def parse_master(text: str, today: date | None = None, include=KEEP_INSTRUMENTS) -> list[dict]:
    today = today or date.today()
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        inst = (r.get("INSTRUMENT") or r.get("SEM_INSTRUMENT_NAME") or "").strip()
        if inst not in include:
            continue
        seg = SEGMENT_MAP.get(((r.get("EXCH_ID") or r.get("SEM_EXM_EXCH_ID") or "").strip(),
                               (r.get("SEGMENT") or r.get("SEM_SEGMENT") or "").strip()))
        sid = (r.get("SECURITY_ID") or r.get("SEM_SMST_SECURITY_ID") or "").strip()
        if not seg or not sid:
            continue
        exp = None
        raw = (r.get("SM_EXPIRY_DATE") or r.get("SEM_EXPIRY_DATE") or "").strip()[:10]
        if raw and not raw.startswith("0001"):
            try:
                exp = datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                exp = None
        if inst.startswith("FUT") and (exp is None or exp < today):
            continue                                            # expired contracts
        out.append({"segment": seg, "security_id": sid, "instrument": inst,
                    "symbol": (r.get("UNDERLYING_SYMBOL") or r.get("SYMBOL_NAME") or r.get("SEM_TRADING_SYMBOL") or "").strip(),
                    "name": (r.get("DISPLAY_NAME") or r.get("SEM_CUSTOM_SYMBOL") or r.get("SYMBOL_NAME") or "").strip(),
                    "lot_size": _f(r.get("LOT_SIZE") or r.get("SEM_LOT_UNITS"), 1.0), "tick_size": _f(r.get("TICK_SIZE") or r.get("SEM_TICK_SIZE")),
                    "expiry": exp, "isin": ((r.get("ISIN") or "").strip() or None)})
    return out


def upsert(factory: sessionmaker, rows: list[dict]) -> int:
    with factory() as s:
        existing = {(i.segment, i.security_id): i for i in s.scalars(select(Instrument))}
        n = 0
        for r in rows:
            key = (r["segment"], r["security_id"])
            cur = existing.get(key)
            if cur is None:
                s.add(Instrument(**r))
                n += 1
            else:
                for k, v in r.items():
                    setattr(cur, k, v)
        s.commit()
        return n


def refreshed_recently(factory: sessionmaker, hours: float = 20) -> bool:
    """True if the instrument list was loaded within `hours` (the activity log records every successful load)."""
    from datetime import timedelta
    from .db import ApiLog, now_ist
    with factory() as s:
        n_inst = s.scalar(select(func.count()).select_from(Instrument)) or 0
        last = s.scalar(select(func.max(ApiLog.ts)).where(ApiLog.source == "data", ApiLog.message.like("Instrument list loaded%")))
    return n_inst > 1000 and last is not None and now_ist() - last < timedelta(hours=hours)


def seed_defaults(factory: sessionmaker) -> None:
    with factory() as s:
        if s.scalar(select(Instrument.id).limit(1)) is not None:
            return
    upsert(factory, [{"segment": a, "security_id": b, "instrument": c, "symbol": d, "name": e, "lot_size": 1.0,
                      "tick_size": None, "expiry": None, "isin": None} for a, b, c, d, e in SEED])


def refresh_master(factory: sessionmaker, url: str = MASTER_URL, http=requests) -> int:
    r = http.get(url, timeout=120)
    r.raise_for_status()
    return upsert(factory, parse_master(r.text))


def search(factory: sessionmaker, q: str, limit: int = 20) -> list[Instrument]:
    q = (q or "").strip()
    if not q:
        return []
    with factory() as s:
        m = DIRECT.match(q) or LABEL.search(q)
        if m and m.group(1).upper() in {v for v in SEGMENT_MAP.values()}:
            hit = s.scalar(select(Instrument).where(Instrument.segment == m.group(1).upper(),
                                                    Instrument.security_id == m.group(2)))
            return [hit] if hit else []
        if q.isdigit():
            return list(s.scalars(select(Instrument).where(Instrument.security_id == q).limit(limit)))
        tokens = q.split()                      # every word must appear in the symbol or the display name
        conds = [or_(Instrument.symbol.ilike(f"%{t}%"), Instrument.name.ilike(f"%{t}%")) for t in tokens]
        rows = s.scalars(select(Instrument).where(*conds).limit(500)).all()
    ql = q.lower()

    def rank(i: Instrument):
        exact = i.symbol.lower() == ql or i.name.lower() == ql
        pref = {"INDEX": 0, "EQUITY": 1, "FUTIDX": 2, "FUTSTK": 3, "FUTCOM": 4}.get(i.instrument, 9)
        nse = 0 if i.segment in ("IDX_I", "NSE_EQ", "NSE_FNO", "MCX_COMM") else 1
        return (0 if exact else 1, pref, nse, i.expiry or date.max, len(i.name))

    return sorted(rows, key=rank)[:limit]


if __name__ == "__main__":      # python -m dryrun.instruments  -> download Dhan's instrument list into the database
    from .__main__ import _load_env
    from .config import Settings
    from .db import init_db
    _load_env()
    _engine, _factory = init_db(Settings().database_url)
    print(f"added {refresh_master(_factory)} new instruments")
