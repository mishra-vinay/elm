"""Speed check, no network and no Dhan account needed.

    python scripts/benchmark.py [--ticks 600000] [--candles 94000]

Builds a throw-away database with a lot of stored data (default: 600k ticks and 5 x 94k one-minute candles, about a
year of 1-minute data for 5 instruments), then times what the dashboard asks the server for, and measures how many
live ticks per second the strategy + database path can absorb with several runs active.
"""
import argparse
import resource
import statistics
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient            # noqa: E402
from sqlalchemy import insert, select                 # noqa: E402

from dryrun.app import create_app                     # noqa: E402
from dryrun.config import Settings                    # noqa: E402
from dryrun.db import Candle, Instrument, Tick, now_ist   # noqa: E402
from dryrun.dhan_feed import FeedTick                 # noqa: E402
from dryrun.sim_feed import ReplayFeed                # noqa: E402


def ms(fn, n=15):
    t = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        t.append((time.perf_counter() - t0) * 1000)
    t.sort()
    return statistics.median(t), t[int(len(t) * 0.95) - 1]


def seed(factory, n_ticks, n_candles):
    with factory() as s:
        insts = s.scalars(select(Instrument).limit(5)).all()
    t0 = time.time()
    base = datetime(2025, 10, 10, 9, 15)
    for inst in insts:
        rows, i = [], 0
        day = base
        while i < n_candles:
            for k in range(375):
                rows.append({"instrument_id": inst.id, "interval": "1m", "ts": day + timedelta(minutes=k), "open": 100.0, "high": 101.0,
                             "low": 99.0, "close": 100.5, "volume": 10.0, "source": "api"})
                i += 1
                if i >= n_candles:
                    break
            day += timedelta(days=1)
            while day.weekday() >= 5:
                day += timedelta(days=1)
            if len(rows) >= 50000:
                with factory() as s:
                    s.execute(insert(Candle), rows)
                    s.commit()
                rows = []
        with factory() as s:
            if rows:
                s.execute(insert(Candle), rows)
            s.commit()
    start = now_ist() - timedelta(days=20)
    for chunk in range(0, n_ticks, 50000):
        rows = [{"instrument_id": insts[(chunk + k) % len(insts)].id, "ts": start + timedelta(seconds=(chunk + k) * 0.5), "ltp": 100.0 + (k % 50) * 0.05,
                 "ltt": None, "volume": k, "day_high": None, "day_low": None} for k in range(min(50000, n_ticks - chunk))]
        with factory() as s:
            s.execute(insert(Tick), rows)
            s.commit()
    return insts, time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", type=int, default=600000)
    ap.add_argument("--candles", type=int, default=94000)
    ap.add_argument("--live-ticks", type=int, default=60000)
    a = ap.parse_args()
    tmp = tempfile.mkdtemp()
    st = Settings(database_url=f"sqlite:///{tmp}/bench.db", feed_mode="sim")
    app = create_app(st, feed_factory=lambda t, n, s: ReplayFeed(t, [], datetime.now(), on_note=n, on_status=s), load_master=False)
    insts, secs = seed(app.state.factory, a.ticks, a.candles)
    print(f"seeded {a.ticks:,} ticks and {a.candles * len(insts):,} candles in {secs:.1f}s\n")
    with TestClient(app) as c:
        m = app.state.manager
        for inst in insts[:5]:
            r = c.post("/api/runs", json={"instrument_id": inst.id, "prev_high": 101, "prev_low": 99, "label": f"bench {inst.symbol}",
                                       "params": {"pullback_pct": 0.05, "target_pct": 0.1, "stop_pct": 0.1, "max_trades_per_day": 50, "max_trades_per_side": 10}})
            assert r.status_code == 200, r.text
        print(f"{'what the page asks for':44s} {'median ms':>10s} {'p95 ms':>9s}")
        paths = [("/api/status (header, every 2 s)", "/api/status"), ("/api/state (dry-runs page, every 2 s)", "/api/state"),
                 ("/api/activity?limit=200 (every 3 s)", "/api/activity?limit=200"), ("/api/activity filtered by source", "/api/activity?limit=200&source=run"),
                 ("/api/data/summary (market page, every 4 s)", "/api/data/summary"), ("/api/instruments/search?q=bajaj", "/api/instruments/search?q=bajaj"),
                 ("/ (page HTML)", "/"), ("/static/common.js", "/static/common.js")]
        for label, path in paths:
            med, p95 = ms(lambda: c.get(path))
            print(f"{label:44s} {med:10.1f} {p95:9.1f}")
        # live tick throughput: ticks go straight into the manager the way the WebSocket client delivers them
        keys = [(i.segment, i.security_id) for i in insts[:5]]
        n = a.live_ticks
        t0 = time.perf_counter()
        ts = now_ist()
        for k in range(n):
            seg, sid = keys[k % 5]
            price = 100.0 + ((k // 5) % 400 - 200) * 0.01
            m._on_tick(FeedTick(seg, sid, round(price, 2), None, k, None, None, ts + timedelta(milliseconds=k * 20)))
        t_feed = time.perf_counter() - t0
        m._flush_ticks()
        t1 = time.perf_counter()
        m.writer.flush()
        t_drain = time.perf_counter() - t1
        print(f"\nlive ticks, 5 active runs: {n:,} ticks processed in {t_feed:.2f}s = {n / t_feed:,.0f} ticks/s (strategy + candles); "
              f"database drained {t_drain:.2f}s after the last tick")
        print("(a busy single instrument is roughly 5-50 ticks/s; the Dhan feed for a handful of instruments is far below this)")
        print("\nafter the tick load:")
        for label, path in paths[:5]:
            med, p95 = ms(lambda: c.get(path), 8)
            print(f"{label:44s} {med:10.1f} {p95:9.1f}")
        c.post("/api/runs/stop-all")
        for _ in range(3):                                   # give the history table some rows, as after a few days of use
            for inst in insts[:5]:
                c.post("/api/runs", json={"instrument_id": inst.id, "prev_high": 101, "prev_low": 99})
            c.post("/api/runs/stop-all")
        med, p95 = ms(lambda: c.get("/api/state"), 8)
        print(f"{'/api/state with 30 finished runs listed':44s} {med:10.1f} {p95:9.1f}")
        raw = c.get("/api/state", headers={"Accept-Encoding": "identity"}).content
        gz = c.get("/api/state", headers={"Accept-Encoding": "gzip"})
        print(f"\n/api/state payload: {len(raw) / 1024:.0f} KB uncompressed, {int(gz.headers.get('content-length', 0)) / 1024:.0f} KB gzipped "
              f"(content-encoding: {gz.headers.get('content-encoding')})")
        print(f"\nwriter errors: {m.writer.errors}; peak memory: {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB")


if __name__ == "__main__":
    main()
