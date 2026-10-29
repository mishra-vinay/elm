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


def test_runner_produces_dashboard_json(tmp_path):
    import json
    import time
    from dryrun.depth_research import to_json
    from dryrun.depth_service import ResearchRunner
    db = str(tmp_path / "d.db")
    make_db(db, days=2)
    assert json.dumps(to_json(run_all(db, "574320")), allow_nan=False)           # no infinity/NaN reaches the browser
    import os
    os.chdir(tmp_path)                                                          # the dashboard runs with a relative data/ path
    rr = ResearchRunner(db, "out")
    assert rr.start("574320", 10, 0.06, 8.0) and not rr.start("574320", 10, 0.06, 8.0)
    for _ in range(120):
        if not rr.running:
            break
        time.sleep(0.5)
    st = rr.state()
    assert not st["running"] and st["error"] is None and len(st["result"]["results"]) == len(RULES) and st["finished"]


def test_depth_post_routes_accept_json_bodies():
    """Request-body models must be module-level (FastAPI cannot resolve locally defined ones under postponed annotations)."""
    import dryrun.app as appmod
    for name in ("DepthTargetIn", "ResearchIn"):
        assert hasattr(appmod, name)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()

    @app.post("/t")
    def t(body: appmod.DepthTargetIn):
        return {"sid": body.security_id}

    @app.post("/r")
    def r(body: appmod.ResearchIn):
        return {"b": body.bar_seconds}
    c = TestClient(app)
    assert c.post("/t", json={"segment": "MCX_COMM", "security_id": "574320"}).json() == {"sid": "574320"}
    assert c.post("/r", json={"security_id": "574320"}).json() == {"b": 10}
