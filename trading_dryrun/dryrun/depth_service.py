"""Daily market-depth recorder inside the dashboard process.

Every weekday from 08:55 to 23:59 IST it keeps its own Dhan WebSocket open (Full packets: last price, volume and the
5 bid + 5 offer levels) and stores every update in its own SQLite file (`data/depth.db`), apart from the trading
database. It uses the access token pasted on the Settings page, reads market data only and never places orders.
The window is wider than MCX's 23:30 close because MCX energy contracts close at 23:55 when US daylight saving ends.
"""
from __future__ import annotations

import asyncio
import logging
import os
import queue
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from .config import IST
from .depth import (DepthRow, insert_rows, log_event, now_ms, open_db, parse_full, sanity, subscribe_full)

log = logging.getLogger("dryrun.depth")
FATAL = {806, 807, 808, 809, 810}
WINDOW = ((8, 55), (23, 59))           # IST, Monday to Friday
BUCKETS = (5, 10, 30, 60, 300)         # seconds, for the chart


def in_window(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    t = (now.hour, now.minute)
    return WINDOW[0] <= t <= WINDOW[1]


def row_json(r: DepthRow) -> dict:
    return {"ts": datetime.fromtimestamp(r.recv_ms / 1000, IST).strftime("%H:%M:%S"), "recv_ms": r.recv_ms, "ltp": r.ltp,
            "ltq": r.ltq, "atp": r.atp, "volume": r.volume, "tot_buy": r.total_buy, "tot_sell": r.total_sell, "oi": r.oi,
            "day_open": r.day_open, "day_high": r.day_high, "day_low": r.day_low,
            "bids": [{"price": p, "qty": q, "orders": o} for p, q, o in r.bids],
            "asks": [{"price": p, "qty": q, "orders": o} for p, q, o in r.asks]}


class DepthService:
    def __init__(self, settings, db_path: str, default_target: Optional[Callable] = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(IST).replace(tzinfo=None),
                 feed_url: str = "wss://api-feed.dhan.co", retention_days: Optional[int] = None):
        self.s, self.db_path, self.default_target, self.clock, self.feed_url = settings, db_path, default_target, clock, feed_url
        self.retention_days = retention_days if retention_days is not None else int(os.environ.get("DEPTH_RETENTION_DAYS", "60"))
        self.q: "queue.Queue" = queue.Queue(maxsize=200_000)
        self.latest: dict = {}                 # security_id -> DepthRow (for the live ladder)
        self.connected = False
        self.fatal: Optional[str] = None
        self.packets = 0
        self.suspect = 0
        self.dropped = 0
        self.write_errors = 0
        self.last_packet_at: Optional[float] = None
        self.started_at: Optional[datetime] = None
        self._token_seen = ""
        self._task: Optional[asyncio.Task] = None
        self._stop = False
        self._wthread: Optional[threading.Thread] = None
        self._con_lock = threading.Lock()
        self.con: Optional[sqlite3.Connection] = None
        self._poke = False

    # ------------------------------------------------------------------ lifecycle
    async def start(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.con = open_db(self.db_path, shared=True)
        self.con.execute("CREATE TABLE IF NOT EXISTS targets (segment TEXT, security_id TEXT, label TEXT, enabled INTEGER DEFAULT 1, "
                         "PRIMARY KEY (segment, security_id))")
        self.con.commit()
        if not self.targets() and self.default_target:
            try:
                t = self.default_target()
                if t:
                    self.set_target(*t, on=True)
            except Exception as e:      # noqa: BLE001
                log.warning("could not choose a default depth instrument: %s", e)
        self._stop = False
        self._wthread = threading.Thread(target=self._writer, name="depth-writer", daemon=True)
        self._wthread.start()
        self._task = asyncio.create_task(self._run(), name="depth-recorder")
        self._purge()

    async def stop(self):
        self._stop = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):      # noqa: BLE001
                pass
        self.q.put(None)
        if self._wthread:
            self._wthread.join(timeout=10)

    def poke(self):
        """Reconnect soon: targets changed or a new token was saved."""
        self._poke = True

    # ------------------------------------------------------------------ targets
    def targets(self) -> list:
        with self._con_lock:
            return [{"segment": a, "security_id": b, "label": c, "enabled": bool(d)} for a, b, c, d in
                    self.con.execute("SELECT segment, security_id, label, enabled FROM targets ORDER BY label")]

    def set_target(self, segment: str, security_id: str, label: str, on: bool = True):
        with self._con_lock:
            self.con.execute("INSERT INTO targets (segment, security_id, label, enabled) VALUES (?,?,?,?) "
                             "ON CONFLICT(segment, security_id) DO UPDATE SET enabled=excluded.enabled, label=excluded.label",
                             (segment, str(security_id), label, 1 if on else 0))
            self.con.commit()
        self.poke()

    def remove_target(self, segment: str, security_id: str):
        with self._con_lock:
            self.con.execute("DELETE FROM targets WHERE segment=? AND security_id=?", (segment, str(security_id)))
            self.con.commit()
        self.latest.pop(str(security_id), None)
        self.poke()

    def _enabled(self) -> list:
        return [(t["segment"], t["security_id"]) for t in self.targets() if t["enabled"]]

    def _event(self, kind: str, msg: str):
        try:
            with self._con_lock:
                log_event(self.con, kind, msg[:400])
        except Exception:      # noqa: BLE001
            pass

    # ------------------------------------------------------------------ writer thread
    def _writer(self):
        con = sqlite3.connect(self.db_path, timeout=30)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        buf: list = []
        while True:
            try:
                item = self.q.get(timeout=1.0)
            except queue.Empty:
                item = "tick"
            if item is None:
                break
            if item != "tick":
                buf.append(item)
            if buf and (item == "tick" or len(buf) >= 500 or self.q.empty()):
                try:
                    insert_rows(con, buf)
                except Exception as e:      # noqa: BLE001
                    self.write_errors += 1
                    log.warning("depth write failed: %s", e)
                buf = []
        if buf:
            try:
                insert_rows(con, buf)
            except Exception:      # noqa: BLE001
                pass
        con.close()

    def _purge(self):
        if self.retention_days <= 0:
            return
        cutoff = int((datetime.now(IST) - timedelta(days=self.retention_days)).timestamp() * 1000)
        try:
            with self._con_lock:
                n = self.con.execute("DELETE FROM depth WHERE recv_ms < ?", (cutoff,)).rowcount
                self.con.commit()
            if n:
                self._event("purge", "deleted %d rows older than %d days" % (n, self.retention_days))
        except Exception as e:      # noqa: BLE001
            log.warning("depth purge failed: %s", e)

    # ------------------------------------------------------------------ feed loop
    async def _run(self):
        import websockets
        backoff, purged_day = 1.0, self.clock().date()
        while not self._stop:
            now = self.clock()
            if now.date() != purged_day:
                purged_day = now.date()
                await asyncio.to_thread(self._purge)
            token, cid = self.s.dhan_access_token, self.s.dhan_client_id
            if self.fatal and token == self._token_seen:
                await asyncio.sleep(5)
                continue
            self.fatal = None
            targets = self._enabled()
            if not in_window(now) or not targets or not (token and cid):
                await asyncio.sleep(10)
                continue
            self._token_seen, self._poke = token, False
            uri = "%s?version=2&token=%s&clientId=%s&authType=2" % (self.feed_url, token, cid)
            try:
                async with websockets.connect(uri, ping_interval=10, ping_timeout=40, max_size=2 ** 20, open_timeout=15) as ws:
                    for seg, sid in targets:
                        await ws.send(subscribe_full(seg, sid))
                    wanted = {sid for _, sid in targets}
                    self.connected, backoff = True, 1.0
                    self.started_at = self.started_at or self.clock()
                    self._event("connected", "subscribed to Full packets for %s" % ", ".join(sorted(wanted)))
                    while not self._stop and in_window(self.clock()) and not self._poke and self.s.dhan_access_token == token:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue
                        if not isinstance(msg, (bytes, bytearray)):
                            continue
                        if msg[:1] == b"\x32" and len(msg) >= 10:           # code 50: server disconnect
                            code = int.from_bytes(msg[8:10], "little", signed=True)
                            self._event("disconnect", "server code %s" % code)
                            if code in FATAL:
                                self.fatal = "Dhan refused the depth feed (code %s): token or Data API plan problem" % code
                                break
                        for r in parse_full(bytes(msg), now_ms()):
                            if r.security_id not in wanted:
                                continue
                            bad = sanity(r)
                            if bad:
                                self.suspect += 1
                                if self.suspect <= 5:
                                    self._event("sanity", "%s sid=%s ltp=%s bid1=%s ask1=%s" % (",".join(bad), r.security_id, r.ltp, r.bids[0][0], r.asks[0][0]))
                            self.packets += 1
                            self.last_packet_at = asyncio.get_running_loop().time()
                            self.latest[r.security_id] = r
                            try:
                                self.q.put_nowait(r)
                            except queue.Full:
                                self.dropped += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:      # noqa: BLE001
                self._event("error", "%s: %s" % (type(e).__name__, e))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                self.connected = False
            if self.fatal:
                self._event("fatal", self.fatal)
            elif not self._poke and in_window(self.clock()):
                await asyncio.sleep(1)

    # ------------------------------------------------------------------ queries (read-only connections)
    def _ro(self) -> sqlite3.Connection:
        con = sqlite3.connect("file:%s?mode=ro" % self.db_path, uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def status(self) -> dict:
        loop_now = None
        try:
            loop_now = asyncio.get_running_loop().time()
        except RuntimeError:
            pass
        age = None if self.last_packet_at is None or loop_now is None else round(loop_now - self.last_packet_at, 1)
        now = self.clock()
        return {"now": now.strftime("%Y-%m-%d %H:%M:%S"), "in_window": in_window(now), "window": "Mon-Fri 08:55-23:59 IST",
                "connected": self.connected, "fatal": self.fatal, "packets_since_start": self.packets, "suspect": self.suspect,
                "dropped": self.dropped, "write_errors": self.write_errors, "queue": self.q.qsize(), "last_packet_age": age,
                "has_token": bool(self.s.dhan_access_token), "targets": self.targets(),
                "db_mb": round(os.path.getsize(self.db_path) / 1e6, 1) if os.path.exists(self.db_path) else 0,
                "retention_days": self.retention_days}

    def days(self, sid: str) -> list:
        with self._ro() as c:
            rows = c.execute("SELECT substr(recv_ts,1,10) d, COUNT(*) n, MIN(recv_ts) a, MAX(recv_ts) b, MAX(volume) v FROM depth "
                             "WHERE sid=? GROUP BY d ORDER BY d DESC", (str(sid),)).fetchall()
        return [{"date": r["d"], "rows": r["n"], "first": r["a"][11:19], "last": r["b"][11:19], "volume": r["v"]} for r in rows]

    def series(self, sid: str, day: str, bucket: int, since_ms: int = 0) -> dict:
        """Bucketed series for one day: OHLC, volume traded in the bucket, and the book at the end of each bucket."""
        bucket = bucket if bucket in BUCKETS else 30
        d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=IST)
        lo, hi = int(d0.timestamp() * 1000), int((d0 + timedelta(days=1)).timestamp() * 1000)
        ms = bucket * 1000
        with self._ro() as c:
            base = c.execute("SELECT MAX(volume) v FROM depth WHERE sid=? AND recv_ms<? AND recv_ms>=?",
                             (str(sid), max(lo, since_ms) if since_ms else lo, lo)).fetchone()["v"] if since_ms else None
            rows = c.execute(
                "SELECT g.k, g.t0, g.hi, g.lo, g.vmax, g.n, g.i0, g.i1, "
                " o.ltp AS o, e.ltp AS c, e.b1p, e.a1p, e.b1q, e.a1q, "
                " (e.b1q+e.b2q+e.b3q+e.b4q+e.b5q) AS bid5, (e.a1q+e.a2q+e.a3q+e.a4q+e.a5q) AS ask5, e.tot_buy, e.tot_sell, e.oi "
                "FROM (SELECT recv_ms/? AS k, MIN(recv_ms) t0, MAX(ltp) hi, MIN(ltp) lo, MAX(volume) vmax, COUNT(*) n, MIN(id) i0, MAX(id) i1 "
                "      FROM depth WHERE sid=? AND recv_ms>=? AND recv_ms<? GROUP BY k) g "
                "JOIN depth o ON o.id=g.i0 JOIN depth e ON e.id=g.i1 ORDER BY g.k",
                (ms, str(sid), max(lo, since_ms), hi)).fetchall()
            agg = {r["k"]: (r["bv"] or 0, r["sv"] or 0) for r in c.execute(
                "SELECT recv_ms/? AS k, SUM(CASE WHEN dv>0 AND ltp>=pa THEN dv END) bv, SUM(CASE WHEN dv>0 AND NOT (ltp>=pa) AND ltp<=pb THEN dv END) sv "
                "FROM (SELECT recv_ms, ltp, vm-LAG(vm) OVER w AS dv, LAG(a1p) OVER w AS pa, LAG(b1p) OVER w AS pb "
                "      FROM (SELECT id, recv_ms, ltp, a1p, b1p, MAX(volume) OVER (ORDER BY id ROWS UNBOUNDED PRECEDING) AS vm FROM depth "
                "            WHERE sid=? AND recv_ms>=? AND recv_ms<?) WINDOW w AS (ORDER BY id)) GROUP BY k",
                (ms, str(sid), max(lo, since_ms), hi))}
        out, prev = [], base
        for r in rows:
            v = None if r["vmax"] is None else (r["vmax"] - prev if prev is not None else None)
            prev = r["vmax"] if r["vmax"] is not None else prev
            b5, a5 = r["bid5"] or 0, r["ask5"] or 0
            bv, sv = agg.get(r["k"], (0, 0))
            out.append({"t": r["k"] * ms, "buyv": bv, "sellv": sv, "o": r["o"], "h": r["hi"], "l": r["lo"], "c": r["c"], "vol": v, "cumvol": r["vmax"], "n": r["n"],
                        "bid": r["b1p"], "ask": r["a1p"], "b1q": r["b1q"], "a1q": r["a1q"], "bid5": b5, "ask5": a5,
                        "imb": round((b5 - a5) / (b5 + a5), 4) if b5 + a5 else 0.0, "tot_buy": r["tot_buy"], "tot_sell": r["tot_sell"], "oi": r["oi"]})
        return {"bucket": bucket, "points": out}

    def export_rows(self, sid: str, day: str):
        d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=IST)
        lo, hi = int(d0.timestamp() * 1000), int((d0 + timedelta(days=1)).timestamp() * 1000)
        c = self._ro()
        cols = ("recv_ts, ltp, ltq, atp, volume, tot_buy, tot_sell, oi, d_open, d_high, d_low, d_close, " +
                ", ".join("%s%d%s" % (s, i, f) for s in "ba" for i in range(1, 6) for f in "pqo"))
        cur = c.execute("SELECT %s FROM depth WHERE sid=? AND recv_ms>=? AND recv_ms<? ORDER BY id" % cols, (str(sid), lo, hi))
        header = [x[0] for x in cur.description]
        return c, cur, header

    def stats(self, sid: str, day: str, bucket: int = 60, horizon: int = 5) -> dict:
        """How price behaved after volume and queue imbalance, from the recorded buckets of one day (descriptive, not a forecast)."""
        pts = self.series(sid, day, bucket)["points"]
        n = len(pts)
        horizon = max(1, min(int(horizon), 60))
        res = {"bucket": bucket, "horizon": horizon, "buckets": n, "summary": None, "imbalance": [], "spikes": []}
        if n < 3:
            return res
        vols = [p["vol"] or 0 for p in pts]
        buyv, sellv = sum(p["buyv"] for p in pts), sum(p["sellv"] for p in pts)
        tot = sum(vols)
        move = [p["c"] - p["o"] for p in pts]
        delta = [p["buyv"] - p["sellv"] for p in pts]

        def corr(a, b):
            m = len(a)
            if m < 5:
                return None
            ma, mb = sum(a) / m, sum(b) / m
            sa = sum((x - ma) ** 2 for x in a) ** 0.5
            sb = sum((y - mb) ** 2 for y in b) ** 0.5
            return round(sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (sa * sb), 3) if sa and sb else None
        fwd = [(pts[i + horizon]["c"] - pts[i]["c"]) if i + horizon < n else None for i in range(n)]
        pairs = [(pts[i]["imb"], fwd[i]) for i in range(n) if fwd[i] is not None]
        res["summary"] = {"volume": tot, "buy_aggr": buyv, "sell_aggr": sellv, "unclassified": max(0, tot - buyv - sellv),
                          "net_delta": buyv - sellv, "avg_imbalance": round(sum(p["imb"] for p in pts) / n, 3),
                          "corr_delta_move": corr(delta, move), "corr_imb_fwd": corr([a for a, _ in pairs], [b for _, b in pairs]),
                          "price_open": pts[0]["o"], "price_close": pts[-1]["c"]}
        bins = [("bid heavy  (imbalance >= +0.40)", 0.4, 9), ("bid leaning (+0.15 to +0.40)", 0.15, 0.4), ("balanced (-0.15 to +0.15)", -0.15, 0.15),
                ("offer leaning (-0.40 to -0.15)", -0.4, -0.15), ("offer heavy (<= -0.40)", -9, -0.4)]
        for label, lo, hi in bins:
            ch = [f for (i, f) in pairs if (lo <= i < hi or (hi == 9 and i >= lo))]
            res["imbalance"].append({"label": label, "n": len(ch), "mean_fwd": round(sum(ch) / len(ch), 3) if ch else None,
                                     "pct_up": round(100 * sum(1 for f in ch if f > 0) / len(ch)) if ch else None})
        mean = tot / n
        sd = (sum((v - mean) ** 2 for v in vols) / n) ** 0.5
        for i, p in enumerate(pts):
            if sd and vols[i] > mean + 2 * sd:
                res["spikes"].append({"t": p["t"], "vol": vols[i], "x_avg": round(vols[i] / mean, 1) if mean else None, "buyv": p["buyv"], "sellv": p["sellv"],
                                      "move": round(move[i], 2), "imb": p["imb"], "fwd": None if fwd[i] is None else round(fwd[i], 2)})
        res["spikes"].sort(key=lambda x: -x["vol"])
        res["spikes"] = res["spikes"][:15]
        return res
