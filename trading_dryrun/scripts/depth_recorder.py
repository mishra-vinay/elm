"""Record every market-depth update (5 bid + 5 offer levels, volume, last price) for one instrument into SQLite.

    python scripts/depth_recorder.py                     # NATGASMINI Nov futures, MCX_COMM:574320, data/depth.db
    python scripts/depth_recorder.py --security-id 574320 --db data/depth.db --until 23:35
    python scripts/depth_recorder.py --summary           # what is already in the file

Credentials come from DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN (environment or a .env file in the current folder).
Read-only: it only subscribes to market data. Runs on Python 3.9+ and needs just `websockets`.
It is separate from the dry-run service (own connection, own file), so it cannot disturb live runs.
Verify the security id first: the dashboard's instrument search lists it as [MCX_COMM:<id>].
"""
import argparse
import asyncio
import os
import re
import sqlite3
import sys
from datetime import datetime, time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dryrun.depth import IST, insert_rows, log_event, now_ms, open_db, parse_full, sanity, subscribe_full   # noqa: E402

FATAL = {806, 807, 808, 809, 810}        # data plan missing / token expired or invalid


def load_env(path=".env"):
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            v = v.strip()
            if v[:1] not in ('"', "'"):
                v = re.split(r"\s+#", v, maxsplit=1)[0].strip()
            os.environ.setdefault(k.strip(), v.strip('"').strip("'"))


def summary(db):
    if not Path(db).exists():
        sys.exit("no such file: %s" % db)
    con = sqlite3.connect(db)
    n, a, b = con.execute("SELECT COUNT(*), MIN(recv_ts), MAX(recv_ts) FROM depth").fetchone()
    print("rows: %d   first: %s   last: %s" % (n, a, b))
    for r in con.execute("SELECT substr(recv_ts,1,10), COUNT(*), MIN(ltp), MAX(ltp), MAX(volume) FROM depth GROUP BY 1"):
        print("  %s  rows=%d  ltp %.1f..%.1f  volume(max)=%s" % r)
    print("last 5 packets (bid1 / ask1 / 5-level qty):")
    for r in con.execute("SELECT recv_ts, ltp, volume, b1p, b1q, a1p, a1q, imbalance5 FROM depth_view ORDER BY id DESC LIMIT 5"):
        print("  ", r)
    print("events:")
    for r in con.execute("SELECT recv_ts, kind, message FROM events ORDER BY id DESC LIMIT 8"):
        print("  ", r)


async def record(args):
    import websockets
    cid, token = os.environ.get("DHAN_CLIENT_ID", ""), os.environ.get("DHAN_ACCESS_TOKEN", "")
    if not (cid and token):
        sys.exit("Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN (environment or .env).")
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    con = open_db(args.db)
    log_event(con, "start", "recording %s:%s to %s until %s" % (args.segment, args.security_id, args.db, args.until))
    h, m = args.until.split(":")[:2]
    stop_at = dtime(int(h), int(m), int(args.until.split(":")[2]) if args.until.count(":") > 1 else 0)
    buf, total, bad, first_len, backoff = [], 0, 0, None, 1.0
    last_flush, last_report = asyncio.get_event_loop().time(), 0.0

    def flush():
        nonlocal buf
        if buf:
            insert_rows(con, buf)
            buf = []

    while datetime.now(IST).time() < stop_at:
        uri = "%s?version=2&token=%s&clientId=%s&authType=2" % (args.feed_url, token, cid)
        try:
            async with websockets.connect(uri, ping_interval=10, ping_timeout=40, max_size=2 ** 20, open_timeout=15) as ws:
                await ws.send(subscribe_full(args.segment, args.security_id))
                log_event(con, "connected", "subscribed to Full packets")
                print("%s connected" % datetime.now(IST).strftime("%H:%M:%S"))
                backoff = 1.0
                while datetime.now(IST).time() < stop_at:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        msg = None
                    now = asyncio.get_event_loop().time()
                    if isinstance(msg, (bytes, bytearray)):
                        if msg[:1] == b"\x32" and len(msg) >= 10:                # code 50: server disconnect
                            code = int.from_bytes(msg[8:10], "little", signed=True)
                            log_event(con, "disconnect", "server code %s" % code)
                            if code in FATAL:
                                flush()
                                sys.exit("Dhan refused the feed (code %s): token or Data API plan problem." % code)
                        if first_len is None:
                            first_len = len(msg)
                            log_event(con, "first_packet", "length %d, code %d" % (len(msg), msg[0]))
                        for r in parse_full(bytes(msg), now_ms()):
                            if r.security_id != str(args.security_id):
                                continue
                            if sanity(r):
                                bad += 1
                                if bad <= 5:
                                    log_event(con, "sanity", "%s ltp=%s bid1=%s ask1=%s" % (",".join(sanity(r)), r.ltp, r.bids[0][0], r.asks[0][0]))
                            buf.append(r)
                            total += 1
                    if buf and (now - last_flush >= 1.0 or len(buf) >= 500):
                        flush()
                        last_flush = now
                    if now - last_report >= 60:
                        last_report = now
                        print("%s rows=%d (suspect=%d)" % (datetime.now(IST).strftime("%H:%M:%S"), total, bad))
        except Exception as e:      # noqa: BLE001
            flush()
            log_event(con, "error", "%s: %s" % (type(e).__name__, e))
            print("feed error: %s: %s; retrying in %.0fs" % (type(e).__name__, e, backoff))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
    flush()
    log_event(con, "stop", "finished, %d rows this run" % total)
    print("done, %d rows" % total)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--segment", default="MCX_COMM")
    ap.add_argument("--security-id", default="574320", help="Dhan SecurityId of the NATGASMINI Nov 2026 future (the number inside [MCX_COMM:...] is NOT it; see the instruments table)")
    ap.add_argument("--db", default="data/depth.db")
    ap.add_argument("--until", default="23:35", help="IST time to stop (MCX closes 23:30)")
    ap.add_argument("--feed-url", default="wss://api-feed.dhan.co", help="only changed for tests")
    ap.add_argument("--summary", action="store_true")
    args = ap.parse_args()
    if args.summary:
        return summary(args.db)
    load_env()
    try:
        asyncio.run(record(args))
    except KeyboardInterrupt:
        print("stopped")


if __name__ == "__main__":
    main()
