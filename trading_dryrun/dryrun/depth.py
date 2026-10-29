"""Market-depth recording: parse Dhan's Full packet (last price, volume, 5 bid + 5 offer levels) and store every
update in a SQLite file. Standalone: needs only the standard library, so it runs on the Mac's Python 3.9.

Full packet (code 8, little-endian, 162 bytes), offsets from https://dhanhq.co/docs/v2/live-market-feed/ :
   0 header: code u8, length u16, segment u8, security id i32
   8 ltp f32 | 12 ltq i16 | 14 ltt i32 | 18 atp f32 | 22 volume i32 | 26 total sell qty i32 | 30 total buy qty i32
  34 oi i32 | 38 highest oi i32 | 42 lowest oi i32 | 46 day open f32 | 50 day close f32 | 54 day high f32 | 58 day low f32
  62 five depth levels of 20 bytes: bid qty i32, ask qty i32, bid orders i16, ask orders i16, bid price f32, ask price f32
The raw packet is stored next to the parsed columns, so a wrong offset can be fixed later by re-parsing.
"""
from __future__ import annotations

import sqlite3
import struct
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
CODE_FULL = 8
FULL_LEN = 162
LEVELS = 5
SEGMENT_BY_CODE = {0: "IDX_I", 1: "NSE_EQ", 2: "NSE_FNO", 3: "NSE_CURRENCY", 4: "BSE_EQ", 5: "MCX_COMM",
                   7: "BSE_CURRENCY", 8: "BSE_FNO"}


@dataclass
class DepthRow:
    segment: str
    security_id: str
    recv_ms: int                       # local arrival time, epoch ms: the only clock that is certain
    ltp: float
    ltq: int
    ltt: int                           # exchange last-trade time, raw
    atp: float
    volume: int
    total_sell: int
    total_buy: int
    oi: int
    day_open: float
    day_close: float
    day_high: float
    day_low: float
    bids: list = field(default_factory=list)     # [(price, qty, orders)] best first
    asks: list = field(default_factory=list)
    raw: bytes = b""


def now_ms() -> int:
    return int(datetime.now(IST).timestamp() * 1000)


def parse_full(data: bytes, recv_ms: Optional[int] = None) -> list:
    """All Full packets in one WebSocket message. Never raises on bad input; unknown packet codes are skipped."""
    recv_ms = recv_ms if recv_ms is not None else now_ms()
    out, off, n = [], 0, len(data)
    while off + 8 <= n:
        code = data[off]
        length = struct.unpack_from("<H", data, off + 1)[0]
        end = off + length if 8 <= length <= n - off else n
        pkt = data[off:end]
        if code == CODE_FULL and len(pkt) >= FULL_LEN:
            try:
                seg = SEGMENT_BY_CODE.get(pkt[3], "SEG%d" % pkt[3])
                sid = str(struct.unpack_from("<i", pkt, 4)[0])
                ltp, ltq, ltt, atp, vol, sell, buy, oi, _hoi, _loi, o, c, h, l = struct.unpack_from("<fhifiiiiiiffff", pkt, 8)
                bids, asks = [], []
                for i in range(LEVELS):
                    bq, aq, bo, ao, bp, ap = struct.unpack_from("<iihhff", pkt, 62 + 20 * i)
                    bids.append((round(bp, 4), bq, bo))
                    asks.append((round(ap, 4), aq, ao))
                out.append(DepthRow(seg, sid, recv_ms, round(ltp, 4), ltq, ltt, round(atp, 4), vol, sell, buy, oi,
                                    round(o, 4), round(c, 4), round(h, 4), round(l, 4), bids, asks, bytes(pkt[:FULL_LEN])))
            except struct.error:
                pass
        if end <= off:
            break
        off = end
    return out


def subscribe_full(segment: str, security_id: str) -> str:
    import json
    return json.dumps({"RequestCode": 21, "InstrumentCount": 1,
                       "InstrumentList": [{"ExchangeSegment": segment, "SecurityId": str(security_id)}]})


def _columns() -> list:
    cols = ["sid TEXT", "seg TEXT", "recv_ms INTEGER", "recv_ts TEXT", "ltp REAL", "ltq INTEGER", "ltt INTEGER",
            "atp REAL", "volume INTEGER", "tot_sell INTEGER", "tot_buy INTEGER", "oi INTEGER",
            "d_open REAL", "d_high REAL", "d_low REAL", "d_close REAL"]
    for side in ("b", "a"):
        for i in range(1, LEVELS + 1):
            cols += ["%s%dp REAL" % (side, i), "%s%dq INTEGER" % (side, i), "%s%do INTEGER" % (side, i)]
    return cols + ["raw BLOB"]


COLUMNS = _columns()
COLUMN_NAMES = [c.split()[0] for c in COLUMNS]
SCHEMA = """
CREATE TABLE IF NOT EXISTS depth (id INTEGER PRIMARY KEY AUTOINCREMENT, %s);
CREATE INDEX IF NOT EXISTS ix_depth_sid_ms ON depth (sid, recv_ms);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, recv_ms INTEGER, recv_ts TEXT, kind TEXT, message TEXT);
CREATE VIEW IF NOT EXISTS depth_view AS
  SELECT id, sid, recv_ts, recv_ms, ltp, volume,
         volume - LAG(volume) OVER (PARTITION BY sid ORDER BY id) AS vol_delta,
         tot_buy, tot_sell, oi,
         b1p, b1q, a1p, a1q,
         (b1q + b2q + b3q + b4q + b5q) AS bid5_qty, (a1q + a2q + a3q + a4q + a5q) AS ask5_qty,
         ROUND(1.0 * ((b1q + b2q + b3q + b4q + b5q) - (a1q + a2q + a3q + a4q + a5q))
               / NULLIF((b1q + b2q + b3q + b4q + b5q) + (a1q + a2q + a3q + a4q + a5q), 0), 4) AS imbalance5,
         ROUND(a1p - b1p, 4) AS spread
  FROM depth;
""" % ", ".join(COLUMNS)


def row_values(r: DepthRow) -> tuple:
    ts = datetime.fromtimestamp(r.recv_ms / 1000, IST).strftime("%Y-%m-%d %H:%M:%S.") + "%03d" % (r.recv_ms % 1000)
    vals = [r.security_id, r.segment, r.recv_ms, ts, r.ltp, r.ltq, r.ltt, r.atp, r.volume, r.total_sell, r.total_buy,
            r.oi, r.day_open, r.day_high, r.day_low, r.day_close]
    for lv in (r.bids, r.asks):
        for p, q, o in lv:
            vals += [p, q, o]
    return tuple(vals + [r.raw])


INSERT = "INSERT INTO depth (%s) VALUES (%s)" % (", ".join(COLUMN_NAMES), ", ".join("?" * len(COLUMN_NAMES)))


def open_db(path: str, shared: bool = False) -> sqlite3.Connection:
    """shared=True lets several threads use the connection (the caller must serialise access with a lock)."""
    con = sqlite3.connect(path, timeout=30, check_same_thread=not shared)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    return con


def insert_rows(con: sqlite3.Connection, rows: list) -> None:
    con.executemany(INSERT, [row_values(r) for r in rows])
    con.commit()


def log_event(con: sqlite3.Connection, kind: str, message: str) -> None:
    ms = now_ms()
    con.execute("INSERT INTO events (recv_ms, recv_ts, kind, message) VALUES (?,?,?,?)",
                (ms, datetime.fromtimestamp(ms / 1000, IST).strftime("%Y-%m-%d %H:%M:%S"), kind, message))
    con.commit()


def sanity(r: DepthRow) -> list:
    """Reasons a parsed packet looks wrong (a hint that the layout differs from the docs)."""
    bad = []
    if r.ltp <= 0 or r.ltp != r.ltp:
        bad.append("ltp")
    b1, a1 = r.bids[0][0], r.asks[0][0]
    if b1 > 0 and a1 > 0 and b1 > a1:
        bad.append("crossed book")
    if any(q < 0 for _, q, _ in r.bids + r.asks):
        bad.append("negative qty")
    if b1 > 0 and r.ltp > 0 and abs(b1 - r.ltp) / r.ltp > 0.05:
        bad.append("bid far from ltp")
    return bad
