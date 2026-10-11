"""First-day check against the real Dhan API. Run it on a trading day with your credentials in .env:

    python scripts/check_dhan.py

It confirms (1) the token and Data API plan, (2) that daily and intraday history decode to the right dates and
times, and (3) that the WebSocket delivers ticks, printing the raw exchange time next to your clock so the
timestamp format can be confirmed. It places no orders and writes nothing to the database.
"""
import asyncio
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dryrun.__main__ import _load_env      # noqa: E402

_load_env()
from dryrun.config import IST               # noqa: E402
from dryrun.dhan_feed import DhanFeed, FeedNote, FeedTick   # noqa: E402
from dryrun.dhan_rest import DhanRest       # noqa: E402

CID, TOKEN = os.environ.get("DHAN_CLIENT_ID", ""), os.environ.get("DHAN_ACCESS_TOKEN", "")
if not (CID and TOKEN):
    sys.exit("Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in .env first.")

rest = DhanRest(CID, TOKEN)
print("1. profile")
try:
    p = rest.profile()
    print("   dataPlan:", p.get("dataPlan"), "| dataValidity:", p.get("dataValidity"), "| tokenValidity:", p.get("tokenValidity"))
except Exception as e:      # noqa: BLE001
    print("   FAILED:", e)

today = date.today()
print("2. history (NIFTY 50, IDX_I:13)")
try:
    d = rest.daily("IDX_I", "13", "INDEX", today - timedelta(days=10), today + timedelta(days=1))
    for r in d[-3:]:
        print("   daily", r["ts"].date(), "O/H/L/C", r["open"], r["high"], r["low"], r["close"])
    i = rest.intraday("IDX_I", "13", "INDEX", 5, today - timedelta(days=4), today)
    print(f"   intraday 5-min bars: {len(i)}; first {i[0]['ts']}  last {i[-1]['ts']}  (expect 09:15 .. 15:25 IST)")
except Exception as e:      # noqa: BLE001
    print("   FAILED:", e)

print("3. WebSocket (20 seconds; run during market hours to see ticks)")
seen = {"ticks": 0, "notes": 0}


def on_tick(t: FeedTick):
    seen["ticks"] += 1
    if seen["ticks"] <= 5:
        if t.ltt:
            print(f"   tick {t.segment}:{t.security_id} ltp={t.ltp} vol={t.volume} raw_ltt={t.ltt} "
                  f"(as unix->IST {datetime.fromtimestamp(t.ltt, IST):%H:%M:%S} | local now {datetime.now(IST):%H:%M:%S})")
        else:
            print("   tick", t)


def on_note(n: FeedNote):
    seen["notes"] += 1
    print("   note", n.kind, n.segment, n.security_id, n.value)


async def main():
    feed = DhanFeed(CID, TOKEN, on_tick, on_note, lambda lvl, msg: print(f"   [{lvl}] {msg}"), packet="quote")
    await feed.subscribe([("IDX_I", "13"), ("NSE_EQ", "317")])
    await feed.start()
    await asyncio.sleep(20)
    await feed.stop()


asyncio.run(main())
print(f"   ticks received: {seen['ticks']}, notes: {seen['notes']}")
