"""Collect tender-offer buyback events from NSE public APIs and filings (cached under .cache/buyback/).

  events         : NSE corporate-actions feed (subject 'Buy Back')  -> symbol, record date, ex date
  announcements  : NSE corporate-announcements feed per symbol, window record date -300d .. +1d
  offer price    : parsed from the text of the earliest buyback announcement PDF (pdftotext)

    python -m strategy_lab.buyback_data
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

import pandas as pd
import requests

CACHE = Path(__file__).parent / ".cache" / "buyback"
H = {"User-Agent": "Mozilla/5.0", "Accept": "application/json", "Referer": "https://www.nseindia.com/"}
BB = re.compile(r"buy[\s\-]?back", re.I)


def session():
    s = requests.Session(); s.headers.update(H)
    s.get("https://www.nseindia.com/", timeout=30)
    return s


def get_json(s, url, tries=4):
    for i in range(tries):
        try:
            r = s.get(url, timeout=60)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (401, 403):
                s.get("https://www.nseindia.com/", timeout=30)
        except Exception:
            pass
        time.sleep(2 * (i + 1))
    return None


def events(s) -> pd.DataFrame:
    CACHE.mkdir(parents=True, exist_ok=True)
    p = CACHE / "events.csv"
    if p.exists():
        return pd.read_csv(p, parse_dates=["rec_date", "ex_date"])
    d = get_json(s, "https://www.nseindia.com/api/corporates-corporateActions?index=equities&subject=Buy%20Back"
                    "&from_date=01-01-2016&to_date=08-10-2026")
    df = pd.DataFrame([{"symbol": r["symbol"], "company": r["comp"], "isin": r["isin"], "subject": r["subject"].strip(),
                        "rec_date": pd.to_datetime(r["recDate"], format="%d-%b-%Y", errors="coerce"),
                        "ex_date": pd.to_datetime(r["exDate"], format="%d-%b-%Y", errors="coerce")} for r in d])
    df = df.dropna(subset=["rec_date"]).drop_duplicates(["symbol", "rec_date"]).sort_values("rec_date").reset_index(drop=True)
    df.to_csv(p, index=False)
    return df


def announcements(s, symbol, rec_date) -> list[dict]:
    CACHE.mkdir(parents=True, exist_ok=True)
    p = CACHE / f"annall_{symbol.replace('&', '_')}_{rec_date:%Y%m%d}.json"
    if p.exists():
        return json.loads(p.read_text())
    lo, hi = rec_date - pd.Timedelta(days=300), rec_date + pd.Timedelta(days=1)
    d = get_json(s, f"https://www.nseindia.com/api/corporate-announcements?index=equities&symbol={symbol}"
                    f"&from_date={lo:%d-%m-%Y}&to_date={hi:%d-%m-%Y}")
    out = [{k: r.get(k) for k in ("an_dt", "desc", "attchmntText", "attchmntFile")} for r in (d or [])]
    p.write_text(json.dumps(out))
    time.sleep(0.4)
    return out


def pdf_text(s, url) -> str:
    if not url:
        return ""
    p = CACHE / ("pdf_" + re.sub(r"[^A-Za-z0-9]", "_", url[-80:]) + ".txt")
    if p.exists():
        return p.read_text()
    try:
        r = s.get(url, timeout=90)
        if r.status_code != 200:
            return ""
        tmp = CACHE / "tmp.pdf"; tmp.write_bytes(r.content)
        txt = subprocess.run(["pdftotext", "-layout", str(tmp), "-"], capture_output=True, text=True, timeout=60).stdout
    except Exception:
        txt = ""
    p.write_text(txt)
    time.sleep(0.3)
    return txt


PRICE = re.compile(r"(?:price\s+of|at\s+(?:a\s+)?(?:maximum\s+)?price\s+(?:of\s+)?|buy[\s\-]?back\s+price\s*(?:of|is|:)?|per\s+(?:equity\s+)?share\s+at)\s*(?:upto\s+|up\s+to\s+|not\s+exceeding\s+)?"
                   r"(?:Rs\.?|INR|₹|`)\s*/?-?\s*(\d[\d,]*(?:\.\d+)?)", re.I)
PRICE2 = re.compile(r"(?:Rs\.?|INR|₹)\s*(\d[\d,]*(?:\.\d+)?)\s*(?:/-)?\s*\(?[^.\n]{0,60}?per\s+(?:fully\s+paid[\s\-]up\s+)?(?:equity\s+)?share", re.I)


def price_candidates(txt: str) -> list[float]:
    vals = []
    for rx in (PRICE, PRICE2):
        vals += [float(m.replace(",", "")) for m in rx.findall(txt)]
    return [v for v in vals if v >= 2]


def parse_price(txt: str):
    for rx in (PRICE, PRICE2):
        vals = [float(m.replace(",", "")) for m in rx.findall(txt)]
        vals = [v for v in vals if v >= 2]          # drop face values/obvious noise
        if vals:
            from collections import Counter
            return Counter(vals).most_common(1)[0][0]
    return None


CANDIDATE_DESC = {"Outcome of Board Meeting", "Buyback", "Public Announcement - Buyback of Shares", "Press Release", "Updates",
                  "General Updates", "Board Meeting Intimation", "Record Date", "Letter of Offer", "Corporate Action", "Postal Ballot",
                  "Newspaper Publication", "Copy of Newspaper Publication", "Disclosure", "Shareholders meeting", "Announcement under Regulation 30 (LODR)-Updates"}


def find_events(s, anns, rec_date, max_pdfs=14, window_days=150):
    """Board-approval filing = first filing, within `window_days` before the record date, whose text mentions a buyback
    AND 'approv...' (board outcome / public announcement). Offer-price candidates are collected from that and later filings."""
    cand = []
    for a in anns:
        ts = pd.to_datetime(a["an_dt"], format="%d-%b-%Y %H:%M:%S", errors="coerce")
        if pd.isna(ts) or ts < rec_date - pd.Timedelta(days=window_days) or ts >= rec_date:
            continue
        if a["desc"] in CANDIDATE_DESC or BB.search(f"{a['desc']} {a['attchmntText']}"):
            cand.append((ts, a))
    cand.sort(key=lambda x: x[0])
    approval = None; prices = []; n = 0
    for ts, a in cand:
        if n >= max_pdfs:
            break
        pdf = a["attchmntFile"] if a["attchmntFile"] and str(a["attchmntFile"]).lower().endswith(".pdf") else None
        txt = pdf_text(s, pdf) if pdf else ""
        n += 1
        meta_hit = bool(BB.search(f"{a['desc']} {a['attchmntText']}"))
        if not (meta_hit or BB.search(txt)):
            continue
        if a["desc"] == "Board Meeting Intimation":
            continue
        is_appr = bool(BB.search(txt) and re.search(r"approv", txt, re.I)) or a["desc"] in ("Buyback", "Public Announcement - Buyback of Shares")
        if approval is None and is_appr:
            approval = (ts, a["desc"])
        if approval is not None:
            prices += price_candidates(txt)
            if prices and n >= 3:
                break
    return dict(approval_ts=approval[0] if approval else pd.NaT, approval_desc=approval[1] if approval else None,
                price_candidates=";".join(str(p) for p in prices), n_candidates=len(cand))


def main():
    s = session()
    ev = events(s)
    print(len(ev), "events", ev.rec_date.min().date(), "->", ev.rec_date.max().date())
    rows = []
    for i, r in ev.iterrows():
        anns = announcements(s, r.symbol, r.rec_date)
        info = find_events(s, anns, r.rec_date)
        rows.append({**r.to_dict(), **info})
        if i % 20 == 0:
            print(f"  {i}/{len(ev)} {r.symbol} {info['approval_ts']} cands={info['price_candidates'][:40]}", flush=True)
    out = pd.DataFrame(rows)
    out.to_csv(CACHE / "buyback_events.csv", index=False)
    print("announcement found:", out.approval_ts.notna().mean(), "| price candidates:", (out.price_candidates.str.len() > 0).mean())
    return out


if __name__ == "__main__":
    main()
