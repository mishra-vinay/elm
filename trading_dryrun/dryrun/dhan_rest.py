"""Dhan REST data calls (historical candles, profile). Data only; there is no order endpoint in this module.

Docs: https://dhanhq.co/docs/v2/historical-data/
  POST https://api.dhan.co/v2/charts/historical  (daily; fromDate/toDate YYYY-MM-DD, toDate exclusive)
  POST https://api.dhan.co/v2/charts/intraday    (interval 1|5|15|25|60; 'YYYY-MM-DD HH:MM:SS'; max 90 days per call;
                                                  about 5 years of history)
  Response: parallel arrays open/high/low/close/volume/timestamp(epoch).
The docs do not state the epoch base or time zone of `timestamp`, so `decode_times` checks the result against the
requested window and the exchange session instead of assuming (and logs which interpretation it chose).
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from .config import IST, SESSIONS

log = logging.getLogger("dryrun.rest")
BASE = "https://api.dhan.co/v2"
EPOCH_1980 = datetime(1980, 1, 1, tzinfo=timezone.utc)
INTRADAY_CHUNK_DAYS = 85          # docs: 90-day maximum per request


class DhanError(RuntimeError):
    pass


def decode_times(stamps: list[float], lo: datetime, hi: datetime, segment: str, daily: bool) -> list[datetime]:
    """Epoch -> naive IST datetimes. Tries Unix(UTC), Unix-as-IST-wall-clock and 1980-based IST; picks the first whose
    results sit inside [lo-2d, hi+2d] and (for intraday) inside the session window for most bars."""
    if not stamps:
        return []
    utc = ZoneInfo("UTC")

    def unix_utc(x):
        return datetime.fromtimestamp(x, utc).astimezone(IST).replace(tzinfo=None)

    def unix_wall(x):
        return datetime.fromtimestamp(x, utc).replace(tzinfo=None)

    def d1980(x):
        return (EPOCH_1980 + timedelta(seconds=x)).replace(tzinfo=None)

    open_t, _last, _sq, close_t = SESSIONS.get(segment, SESSIONS["NSE_EQ"])
    lo2, hi2 = lo - timedelta(days=2), hi + timedelta(days=2)
    for name, fn in (("unix-utc", unix_utc), ("unix-ist-wallclock", unix_wall), ("epoch-1980-ist", d1980)):
        try:
            vals = [fn(x) for x in stamps]
        except (OverflowError, OSError, ValueError):
            continue
        if not all(lo2 <= v <= hi2 for v in vals[:: max(1, len(vals) // 50)]):
            continue
        if not daily:
            sample = vals[:: max(1, len(vals) // 200)]
            inside = sum(1 for v in sample if open_t <= v.time() <= close_t)
            if inside < 0.9 * len(sample):
                continue
        log.debug("timestamps decoded as %s", name)
        return vals
    raise DhanError("could not interpret candle timestamps; check scripts/check_dhan.py output")


class DhanRest:
    def __init__(self, client_id: str, token: str, session: requests.Session | None = None, pause: float = 0.25):
        self.client_id, self.token, self.pause = client_id, token, pause
        self.http = session or requests.Session()
        self.last_latency_ms: float | None = None

    def _post(self, path: str, body: dict) -> dict:
        headers = {"access-token": self.token, "client-id": self.client_id,
                   "Content-Type": "application/json", "Accept": "application/json"}
        t0 = time.time()
        r = self.http.post(f"{BASE}{path}", json=body, headers=headers, timeout=30)
        self.last_latency_ms = (time.time() - t0) * 1000
        time.sleep(self.pause)
        if r.status_code != 200:
            raise DhanError(f"{path} HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        if isinstance(data, dict) and data.get("errorType"):
            raise DhanError(f"{path}: {data.get('errorCode')} {data.get('errorMessage') or data}")
        return data

    def profile(self) -> dict:
        r = self.http.get(f"{BASE}/profile", headers={"access-token": self.token}, timeout=20)
        if r.status_code != 200:
            raise DhanError(f"profile HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    @staticmethod
    def _rows(resp: dict, times: list[datetime]) -> list[dict]:
        n = len(times)
        get = lambda k: resp.get(k) or [None] * n      # noqa: E731
        o, h, l, c, v = get("open"), get("high"), get("low"), get("close"), get("volume")
        return [{"ts": times[i], "open": o[i], "high": h[i], "low": l[i], "close": c[i], "volume": v[i]}
                for i in range(n) if None not in (o[i], h[i], l[i], c[i])]

    def daily(self, segment: str, security_id: str, instrument: str, start: date, end: date,
              expiry_code: int | None = None) -> list[dict]:
        body = {"securityId": str(security_id), "exchangeSegment": segment, "instrument": instrument,
                "fromDate": start.isoformat(), "toDate": end.isoformat(), "oi": False}
        if expiry_code is not None:
            body["expiryCode"] = expiry_code
        resp = self._post("/charts/historical", body)
        stamps = resp.get("timestamp") or []
        times = decode_times(stamps, datetime.combine(start, datetime.min.time()),
                             datetime.combine(end, datetime.min.time()), segment, daily=True)
        rows = self._rows(resp, times)
        for r in rows:
            r["ts"] = datetime.combine(r["ts"].date(), datetime.min.time())
        return rows

    def intraday(self, segment: str, security_id: str, instrument: str, interval: int, start: date, end: date,
                 expiry_code: int | None = None) -> list[dict]:
        """Fetch [start, end] in <=85-day chunks. interval in {1,5,15,25,60} minutes."""
        if interval not in (1, 5, 15, 25, 60):
            raise ValueError("interval must be one of 1, 5, 15, 25, 60")
        rows: list[dict] = []
        cur = start
        while cur <= end:
            nxt = min(cur + timedelta(days=INTRADAY_CHUNK_DAYS), end)
            frm = datetime.combine(cur, datetime.min.time())
            to = datetime.combine(nxt, datetime.max.time()).replace(microsecond=0)
            body = {"securityId": str(security_id), "exchangeSegment": segment, "instrument": instrument,
                    "interval": str(interval), "oi": False, "fromDate": frm.strftime("%Y-%m-%d %H:%M:%S"),
                    "toDate": to.strftime("%Y-%m-%d %H:%M:%S")}
            if expiry_code is not None:
                body["expiryCode"] = expiry_code
            resp = self._post("/charts/intraday", body)
            stamps = resp.get("timestamp") or []
            times = decode_times(stamps, frm, to, segment, daily=False)
            rows.extend(self._rows(resp, times))
            cur = nxt + timedelta(days=1)
        return rows
