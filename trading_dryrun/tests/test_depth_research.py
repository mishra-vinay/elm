from datetime import datetime

from dryrun.config import IST
from dryrun.depth import insert_rows, open_db, parse_full
from dryrun.depth_research import RULES, load_bars, run_all, write_report
from tests.test_depth import full_packet


def make_db(path, days=2, drift=0.1):
    con = open_db(path)
    for d in range(days):
        base = int(datetime(2026, 10, 26 + d, 10, 0, tzinfo=IST).timestamp() * 1000)
        px, vol, rows = 290.0, 1000, []
        for i in range(2400):                                     # 2 updates per 10 s bar, 200 minutes
            px = round(px + drift, 1)
            vol += 10
            bids = [(round(px - 0.1 - 0.1 * k, 1), 20, 3) for k in range(5)]
            asks = [(round(px + 0.1 + 0.1 * k, 1), 20, 3) for k in range(5)]
            rows += parse_full(full_packet(sid=574320, ltp=px, vol=vol, bids=bids, asks=asks), base + i * 5000)
        insert_rows(con, rows)
    con.close()


def test_bars_classify_buyers_and_dense(tmp_path):
    db = str(tmp_path / "d.db")
    make_db(db, days=1)
    bars = load_bars(db, "574320", 10)
    [(day, b)] = bars.items()
    assert len(b) == 1200 and all(x.n == 2 for x in b)
    assert sum(x.buyv for x in b) > 0 and sum(x.sellv for x in b) == 0          # price always rises into the offer: all buying
    assert b[5].ask_first > b[5].bid_first


def test_flow_follow_wins_on_uptrend_and_fade_loses(tmp_path):
    db = str(tmp_path / "d.db")
    make_db(db, days=2)
    res = run_all(db, "574320")
    by = {r["rule"]: r for r in res["results"]}
    follow = by["flow-follow: net aggressive volume share >= 0.5 over 3 min, hold 2m"]
    fade = by["flow-fade:   net aggressive volume share >= 0.5 over 3 min, hold 2m"]
    assert follow["full"]["n"] > 20 and follow["full"]["mean"] > 0 and follow["full"]["days_pos"] == 2
    assert fade["full"]["mean"] < 0
    assert follow["mode"] == "days" and follow["verdict"] in ("WORTH A CLOSER LOOK", "no")


def test_one_day_cannot_be_flagged_and_report_writes(tmp_path):
    db = str(tmp_path / "d.db")
    make_db(db, days=1)
    res = run_all(db, "574320")
    assert all(r["verdict"] in ("need more days", "no") or r["full"].get("n", 0) == 0 for r in res["results"])
    text = write_report(res, str(tmp_path / "r.md"), str(tmp_path / "t.csv"))
    assert "Depth research report" in text and len(RULES) == len(res["results"])
    assert (tmp_path / "t.csv").read_text().startswith("rule,day,side")
