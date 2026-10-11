import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from dryrun.app import create_app
from dryrun.config import Settings
from dryrun.replay import replay_day, replay_range
from dryrun.sim_feed import ReplayFeed
from dryrun.strategy import Params

START = datetime(2026, 10, 9, 10, 0, 0)
# H=100, L=95: break up, pullback to 99.0 (BUY), then the +2% target (about 101.02)
BUY_PATH = [99.0, 99.5, 100.4, 101.0, 100.2, 99.0, 99.8, 100.5, 101.2, 101.5]


def make_app(tmp_path, prices, **kw):
    st = Settings(database_url=f"sqlite:///{tmp_path}/app.db", feed_mode="sim", **kw)
    return create_app(st, feed_factory=lambda t, n, s: ReplayFeed(t, prices, START, on_note=n, on_status=s), load_master=False)


def wait_for(fn, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("timed out")


def test_full_dry_run_buy_to_target_is_saved(tmp_path):
    app = make_app(tmp_path, BUY_PATH)
    with TestClient(app) as c:
        r = c.post("/api/runs", json={"query": "BAJFINANCE", "prev_high": 100, "prev_low": 95,
                                       "params": {"slippage_pct": 0, "cost_pct": 0}})
        assert r.status_code == 200, r.text
        rid = r.json()["id"]
        wait_for(lambda: c.get("/api/state").json()["runs"][0]["summary"]["trades"] == 1)
        assert c.post(f"/api/runs/{rid}/stop").status_code == 200
        d = c.get(f"/api/runs/{rid}").json()
        assert d["status"] == "stopped" and d["ran_ok"] is True and d["ticks"] == len(BUY_PATH)
        assert [s["kind"] for s in d["signals"]] == ["break", "entry"] and d["signals"][1]["action"] == "taken"
        [t] = d["trades"]
        assert t["side"] == "BUY" and t["entry_price"] == 99.0 and t["exit_reason"] == "target"
        assert t["exit_price"] == pytest.approx(99.0 * 1.02) and t["net_pnl"] == pytest.approx(99.0 * 0.02)
        assert d["summary"]["trades"] == 1 and d["summary"]["wins"] == 1 and d["summary"]["win_rate"] == 1.0
        assert d["summary"]["levels"]["source"] == "manual"
        hist = c.get("/api/state").json()["history"]
        assert hist[0]["id"] == rid and hist[0]["ran_ok"] is True and hist[0]["net_pnl"] == pytest.approx(99.0 * 0.02)
        csv = c.get("/api/export/trades.csv").text
        assert "BAJFINANCE" in csv and "target" in csv
        # ticks and 1-minute candles of the run were stored for later research
        data = wait_for(lambda: (lambda d: d if d["ticks"] == len(BUY_PATH) else None)(c.get("/api/data/summary").json()))
        assert data["ticks"] == len(BUY_PATH)


def test_levels_needed_then_manual_entry(tmp_path):
    app = make_app(tmp_path, [99.0])
    with TestClient(app) as c:
        r = c.post("/api/runs", json={"query": "NIFTY 50", "params": {}})
        assert r.status_code == 409 and r.json()["code"] == "levels_needed"
        assert c.post("/api/runs", json={"query": "NIFTY 50", "prev_high": 90, "prev_low": 95}).status_code == 400
        assert c.post("/api/runs", json={"query": "NIFTY 50", "prev_high": 100, "prev_low": 95}).status_code == 200
        assert c.post("/api/runs", json={"query": "NIFTY 50", "prev_high": 100, "prev_low": 95}).status_code == 400   # duplicate run


def test_validation_and_unknown_instrument(tmp_path):
    app = make_app(tmp_path, [1.0])
    with TestClient(app) as c:
        assert c.post("/api/runs", json={"query": "nothing-like-this", "params": {}}).status_code == 404
        assert c.post("/api/runs", json={"query": "BAJFINANCE", "params": {"target_pct": 0}, "prev_high": 2, "prev_low": 1}).status_code == 400
        assert c.get("/api/instruments/search", params={"q": "nifty"}).json()[0]["symbol"] == "NIFTY"
        assert c.post("/api/runs/999/stop").status_code == 404
        assert c.post("/api/backfill", json={"instrument_id": 1}).status_code == 400          # no Dhan credentials in sim mode
        assert c.get("/").status_code == 200 and c.get("/health").json() == {"ok": True}


def test_interrupted_runs_are_marked_on_restart(tmp_path):
    app = make_app(tmp_path, [99.0])
    with TestClient(app) as c:
        c.post("/api/runs", json={"query": "BAJFINANCE", "prev_high": 100, "prev_low": 95})
    # simulate a crash: run row still 'waiting' when a new process starts
    from sqlalchemy import update
    from dryrun.db import Run
    with app.state.factory() as s:
        s.execute(update(Run).values(status="waiting", ran_ok=None))
        s.commit()
    app2 = make_app(tmp_path, [99.0])
    with TestClient(app2) as c:
        h = c.get("/api/state").json()["history"]
        assert h[0]["status"] == "interrupted" and h[0]["ran_ok"] is False


def test_password_login_gate(tmp_path):
    st = Settings(database_url=f"sqlite:///{tmp_path}/a.db", feed_mode="sim", host="0.0.0.0", dashboard_password="secret")
    app = create_app(st, feed_factory=lambda t, n, s: ReplayFeed(t, [1.0], START), load_master=False)
    with TestClient(app) as c:
        assert c.get("/api/state").status_code == 401
        assert c.post("/api/auth/login", json={"password": "nope"}).status_code == 401
        assert c.post("/api/auth/login", json={"password": "secret"}).status_code == 200
        assert c.get("/api/state").status_code == 200
        assert c.get("/health").status_code == 200


def test_public_bind_without_login_refuses_to_start(tmp_path):
    with pytest.raises(RuntimeError):
        create_app(Settings(database_url=f"sqlite:///{tmp_path}/b.db", feed_mode="sim", host="0.0.0.0"), load_master=False)


# ---- the replay (backtest) path uses the same engine ---------------------------------------------------------------
def candle(ts, o, h, l, c):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c}


def test_replay_day_matches_live_engine_on_same_prices():
    day = datetime(2026, 10, 9, 9, 15)
    candles = [candle(day, 99.5, 100.6, 99.4, 100.5),                    # bullish bar: O, L, H, C -> breaks above 100
               candle(day + timedelta(minutes=1), 100.5, 100.6, 98.9, 99.0),   # bearish: O, H, L, C -> pulls back to 99.0
               candle(day + timedelta(minutes=2), 99.0, 101.5, 99.0, 101.4)]
    res = replay_day(candles, 100, 95, Params(slippage_pct=0, cost_pct=0))
    assert res["trades"] == 1 and res["trade_list"][0]["exit_reason"] == "target"


def test_replay_range_uses_previous_day_levels():
    d1 = datetime(2026, 10, 8, 9, 15)
    d2 = datetime(2026, 10, 9, 9, 15)
    candles = [candle(d1, 97, 100, 95, 98), candle(d1 + timedelta(minutes=1), 98, 99, 96, 97)]
    candles += [candle(d2, 99.5, 100.6, 99.4, 100.5), candle(d2 + timedelta(minutes=1), 100.5, 100.6, 98.9, 99.0),
                candle(d2 + timedelta(minutes=2), 99.0, 101.5, 99.0, 101.4)]
    r = replay_range(candles, Params(slippage_pct=0, cost_pct=0))
    assert r["sessions"] == 1 and r["trades"] == 1 and r["wins"] == 1


def test_search_accepts_the_dropdown_label_and_status_text_explains_what_is_happening(tmp_path):
    app = make_app(tmp_path, BUY_PATH)
    with TestClient(app) as c:
        hit = c.get("/api/instruments/search", params={"q": "Bajaj Finance [NSE_EQ:317]"}).json()
        assert len(hit) == 1 and hit[0]["security_id"] == "317"          # exactly what the dropdown puts in the box
        r = c.post("/api/runs", json={"instrument_id": hit[0]["id"], "prev_high": 100, "prev_low": 95,
                                       "params": {"slippage_pct": 0, "cost_pct": 0}})
        assert r.status_code == 200, r.text
        wait_for(lambda: c.get("/api/state").json()["runs"][0]["summary"]["trades"] == 1)
        run = c.get("/api/state").json()["runs"][0]
        text = "\n".join(t for _ts, t in run["log"])
        assert "Run started. Previous-day High 100.00, Low 95.00 (source: manual)" in text
        assert "broke above the previous High" in text and "SIMULATED BUY entered at 99.00" in text and "SIMULATED exit (target)" in text
        assert "yesterday's range" in run["status_text"] and "95.00 to 100.00" in run["status_text"]


def test_status_text_before_any_price_arrives(tmp_path):
    app = make_app(tmp_path, [])
    with TestClient(app) as c:
        assert c.post("/api/runs", json={"query": "NIFTY 50", "prev_high": 100, "prev_low": 95}).status_code == 200
        run = c.get("/api/state").json()["runs"][0]
        assert "no price has arrived yet" in run["status_text"]
        assert any("Subscribed to Nifty 50" in t for _ts, t in run["log"])
