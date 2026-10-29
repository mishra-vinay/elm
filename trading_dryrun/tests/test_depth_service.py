import asyncio
import struct
from datetime import datetime

from dryrun.config import Settings
from dryrun.depth import insert_rows, open_db, parse_full
from dryrun.depth_service import DepthService, in_window
from tests.test_depth import full_packet


def test_window_weekdays_only():
    assert in_window(datetime(2026, 10, 29, 9, 0)) and in_window(datetime(2026, 10, 29, 23, 58))
    assert not in_window(datetime(2026, 10, 29, 8, 30)) and not in_window(datetime(2026, 10, 31, 12, 0))


def test_targets_series_days_export(tmp_path):
    db = str(tmp_path / "depth.db")
    svc = DepthService(Settings(feed_mode="sim"), db, default_target=lambda: ("MCX_COMM", "574320", "NATGASMINI NOV"))

    async def go():
        await svc.start()
        assert svc.targets() == [{"segment": "MCX_COMM", "security_id": "574320", "label": "NATGASMINI NOV", "enabled": True}]
        base = int(datetime(2026, 10, 29, 10, 0).timestamp() * 1000)          # any day; the series uses IST day bounds
        rows = []
        for i in range(6):
            rows += parse_full(full_packet(sid=574320, ltp=290 + i, vol=100 + 10 * i), base + i * 20_000)
        con = open_db(db)
        insert_rows(con, rows)
        day = rows[0] and __import__("datetime").datetime.fromtimestamp(base / 1000, __import__("dryrun.config", fromlist=["IST"]).IST).strftime("%Y-%m-%d")
        d = svc.days("574320")
        assert d and d[0]["rows"] == 6
        s = svc.series("574320", day, 30)["points"]
        assert len(s) == 4 and s[0]["o"] == 290 and s[0]["c"] == 291 and s[0]["bid5"] == 64 and s[0]["ask5"] == 68
        assert s[1]["vol"] == 10 and s[0]["imb"] == round((64 - 68) / 132, 4)
        assert 'buyv' in s[0] and 'sellv' in s[0]
        st = svc.stats('574320', day, 30, 1)
        assert st['buckets'] == 4 and st['summary']['volume'] >= 0 and len(st['imbalance']) == 5
        c, cur, header = svc.export_rows("574320", day)
        assert header[0] == "recv_ts" and len(cur.fetchall()) == 6
        c.close()
        import threading
        out = []
        th = threading.Thread(target=lambda: out.append(svc.status()))       # the web routes run in another thread
        th.start(); th.join()
        assert out and out[0]['targets'][0]['security_id'] == '574320'
        svc.remove_target("MCX_COMM", "574320")
        assert svc.targets() == []
        await svc.stop()
    asyncio.run(go())
