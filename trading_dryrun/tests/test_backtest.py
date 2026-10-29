"""Online backtest: dataset listing, running through the API, agreement with the replay engine, and the error cases."""
import random
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from dryrun.app import create_app
from dryrun.backtests import BacktestError, run_backtest, summarise
from dryrun.config import Settings
from dryrun.db import Candle, Instrument
from dryrun.replay import replay_range
from dryrun.sim_feed import ReplayFeed
from demo_strategy import P, jp


def candles(seed=5, n_days=30, bars=375, start=datetime(2026, 1, 5, 9, 15), short_day=None):
    rnd, out, price, day = random.Random(seed), [], 100.0, start
    for i in range(n_days):
        nb = 60 if i == short_day else bars
        for k in range(nb):
            o = price
            c = o * (1 + rnd.gauss(0, 0.0009))
            out.append({"ts": day + timedelta(minutes=k), "open": o, "high": max(o, c) * (1 + abs(rnd.gauss(0, 0.0003))),
                        "low": min(o, c) * (1 - abs(rnd.gauss(0, 0.0003))), "close": c})
            price = c
        day += timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
    return out


@pytest.fixture
def app_with_data(tmp_path):
    st = Settings(database_url=f"sqlite:///{tmp_path}/b.db", feed_mode="sim")
    app = create_app(st, feed_factory=lambda t, n, s: ReplayFeed(t, [], datetime.now(), on_note=n, on_status=s), load_master=False)
    with TestClient(app) as c:
        with app.state.factory() as s:
            inst = s.scalar(select(Instrument).where(Instrument.symbol == "BAJFINANCE"))
            rows = [{"instrument_id": inst.id, "interval": "1m", "source": "api", "volume": 1.0, **x} for x in candles(short_day=10)]
            rows += [{"instrument_id": inst.id, "interval": "1m", "source": "sim", "volume": 1.0, **x} for x in candles(seed=9, n_days=5, start=datetime(2026, 6, 1, 9, 15))]
            s.execute(insert(Candle), rows)
            s.commit()
            iid = inst.id
        yield c, app, iid


def wait(c, jid, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        j = c.get(f"/api/backtest/{jid}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.1)
    raise AssertionError("backtest did not finish")


def test_dataset_is_listed_with_names_and_daily_is_separate(app_with_data):
    c, _app, iid = app_with_data
    cs = [x for x in c.get("/api/data/summary").json()["candles"] if x["instrument_id"] == iid]
    assert cs and cs[0]["interval"] == "1m" and cs[0]["name"] and cs[0]["count"] > 10000


def test_pullback_backtest_matches_the_replay_engine(app_with_data):
    c, app, iid = app_with_data
    r = c.post("/api/backtest", json={"instrument_id": iid, "interval": "1m", "date_from": "2026-01-01", "date_to": "2026-03-31",
                                      "params": jp(pullback_pct=0.2, target_pct=0.4, stop_pct=0.2)})
    assert r.status_code == 200, r.text
    j = wait(c, r.json()["id"])
    assert j["status"] == "done", j["error"]
    res = j["result"]
    # the same candles through replay_range directly (the short session and the sim candles are excluded by the backtest)
    with app.state.factory() as s:
        rows = s.scalars(select(Candle).where(Candle.instrument_id == iid, Candle.source == "api").order_by(Candle.ts)).all()
    keep = [x for x in rows if x.ts.date().isoformat() not in res["dropped_sessions"]]
    ref = replay_range([{"ts": x.ts, "open": x.open, "high": x.high, "low": x.low, "close": x.close} for x in keep],
                       P(pullback_pct=0.2, target_pct=0.4, stop_pct=0.2), "NSE_EQ")
    assert res["trades"] == ref["trades"] > 5
    assert res["total"] == pytest.approx(sum(t["net_pct"] for d in ref["per_day"].values() for t in d["trade_list"]), abs=0.01)
    assert len(res["dropped_sessions"]) == 1                                   # the 60-bar session
    assert res["candles"] == len(keep) and res["trades"] == len(res["trade_list"]) == len(res["equity"])
    assert {x["key"] for x in res["by_side"]} <= {"BUY", "SELL"} and res["by_month"]


def test_sim_candles_are_ignored_and_a_second_strategy_setting_runs(app_with_data):
    c, _app, iid = app_with_data
    r = c.post("/api/backtest", json={"instrument_id": iid, "interval": "1m", "date_from": "2026-05-25", "date_to": "2026-06-30", "params": jp()})
    assert r.status_code == 200
    j = wait(c, r.json()["id"])
    assert j["status"] == "error" and "no candles" in j["error"]                # only 'sim' candles exist in June
    r = c.post("/api/backtest", json={"instrument_id": iid, "interval": "1m", "params": jp(pullback_pct=0.1, target_pct=0.3)})
    j = wait(c, r.json()["id"])
    assert j["status"] == "done" and j["result"]["params"]["strategy"] == "demo" and j["result"]["to_date"] < "2026-06-01"


def test_error_cases(app_with_data):
    c, _app, iid = app_with_data
    assert c.post("/api/backtest", json={"instrument_id": iid, "interval": "1m", "params": jp(target_pct=0)}).status_code == 400
    assert c.post("/api/backtest", json={"instrument_id": 999999, "interval": "1m"}).status_code == 404
    r = c.post("/api/backtest", json={"instrument_id": iid, "interval": "D"})
    assert wait(c, r.json()["id"])["status"] == "error"
    assert c.get("/api/backtest/doesnotexist").status_code == 404
    with pytest.raises(BacktestError):
        run_backtest(_app.state.factory, iid, "1m", P(), date_from=datetime(2030, 1, 1).date())


def test_summarise_statistics():
    trades = [{"date": f"2026-01-{i + 1:02d}", "side": "BUY" if i % 2 else "SELL", "net_pct": v, "exit_reason": "target" if v > 0 else "stop"}
              for i, v in enumerate([1.0, -0.5, 1.0, -0.5, 1.0, -0.5])]
    sm = summarise(trades, {f"x{i}": 0 for i in range(0)}, 6)
    assert sm["trades"] == 6 and sm["wins"] == 3 and sm["win_rate"] == 50.0 and sm["total"] == 1.5 and sm["profit_factor"] == 2.0
    assert sm["max_drawdown"] == 0.5 and sm["equity"][-1][1] == 1.5 and [x["key"] for x in sm["by_exit"]] == ["stop", "target"]
    assert any("Only 6 trades" in w for w in sm["warnings"])


def test_summarise_adds_the_rupee_result_when_trades_carry_net_pnl():
    pnl = [100.0, -40.0, 100.0, -40.0, -80.0, 100.0]
    trades = [{"date": f"2026-01-{i + 1:02d}", "exit_dt": f"2026-01-{i + 1:02d} 10:00:00", "side": "BUY", "net_pct": v / 100, "net_pnl": v, "exit_reason": "target" if v > 0 else "stop"}
              for i, v in enumerate(pnl)]
    sm = summarise(trades, {}, 6, capital=1000.0)
    assert sm["capital"] == 1000.0 and sm["net_pnl"] == 140.0 and sm["net_pnl_pct"] == 14.0
    assert sm["max_drawdown_rs"] == 120.0 and sm["equity_rs"][-1] == ["2026-01-06 10:00:00", 140.0] and len(sm["equity_rs"]) == 6
    assert sm["max_drawdown_rs_pct"] == pytest.approx(120 / 1160 * 100, abs=0.01)           # equity 100, 60, 160, 120, 40, 140: a fall of 120 from the peak of 160 (capital 1000 + 160)
    assert "net_pnl" not in summarise([{"date": "2026-01-01", "side": "BUY", "net_pct": 1.0, "exit_reason": "target"}], {}, 1)       # old-style trades: no rupee block


def test_result_carries_rupees_levels_and_a_day_view_for_the_chart(app_with_data):
    c, _app, iid = app_with_data
    r = c.post("/api/backtest", json={"instrument_id": iid, "interval": "1m", "date_from": "2026-01-01", "date_to": "2026-03-31", "capital": 50000,
                                      "params": jp(pullback_pct=0.2, target_pct=0.4, stop_pct=0.2, qty=10)})
    j = wait(c, r.json()["id"])
    assert j["status"] == "done", j["error"]
    res = j["result"]
    assert res["capital"] == 50000 and res["trades"] > 5 and res["net_pnl"] == pytest.approx(sum(t["net_pnl"] for t in res["trade_list"]), abs=0.05)
    t = res["trade_list"][0]
    assert t["qty"] == 10 and t["entry_dt"] < t["exit_dt"] and t["target"] and "stop" in t and abs(t["net_pnl"]) > 0
    assert res["instrument_id"] == iid and res["day_levels"] and all(isinstance(v, list) and v[0]["name"] for v in res["day_levels"].values())
    day = t["date"]
    v = c.get(f"/api/backtest/{j['id']}/day", params={"date": day})
    assert v.status_code == 200, v.text
    v = v.json()
    assert v["date"] == day and v["interval"] == "1m" and len(v["candles"]) == 375 and v["candles"][0][0] == "09:15" and len(v["candles"][0]) == 5
    assert [x["name"] for x in v["levels"]] == ["prev High", "buy trigger", "sell trigger", "prev Low"] and any(x["entry_dt"] == t["entry_dt"] for x in v["trades"])
    first_day = min(res["day_levels"])
    assert c.get(f"/api/backtest/{j['id']}/day", params={"date": "2020-01-01"}).status_code == 404       # not a tested date
    assert c.get(f"/api/backtest/nope/day", params={"date": day}).status_code == 404
    assert c.get(f"/api/backtest/{j['id']}/day", params={"date": "not-a-date"}).status_code == 422
    assert first_day > "2026-01-04"                                                                         # the first session only supplies the previous-day High/Low


def test_cutting_the_data_off_earlier_never_changes_earlier_trades(tmp_path):
    """The completeness filter compares a session with a full exchange session (375 bars), not with the median of the selected range."""
    st = Settings(database_url=f"sqlite:///{tmp_path}/c.db", feed_mode="sim")
    app = create_app(st, feed_factory=lambda t, n, s: ReplayFeed(t, [], datetime.now(), on_note=n, on_status=s), load_master=False)
    with TestClient(app):
        data = candles(seed=11, n_days=40)
        by_day = {}
        for x in data:
            by_day.setdefault(x["ts"].date(), []).append(x)
        days = sorted(by_day)
        rows = [x for d in days[:8] for x in by_day[d][:290]] + [x for d in days[8:] for x in by_day[d]]       # eight thin sessions (290 of 375 bars = 77%) first
        with app.state.factory() as s:
            inst = s.scalar(select(Instrument).where(Instrument.symbol == "BAJFINANCE"))
            s.execute(insert(Candle), [{"instrument_id": inst.id, "interval": "1m", "source": "api", "volume": 1.0, **x} for x in rows])
            s.commit()
            iid = inst.id
        full = run_backtest(app.state.factory, iid, "1m", P(pullback_pct=0.2, target_pct=0.4, stop_pct=0.2))
        cut_day = days[11]
        part = run_backtest(app.state.factory, iid, "1m", P(pullback_pct=0.2, target_pct=0.4, stop_pct=0.2), date_to=cut_day)
        assert [d.isoformat() for d in days[:8]] == full["dropped_sessions"][:8] and full["dropped_sessions"][:8] == part["dropped_sessions"][:8]       # the thin sessions are dropped in both
        key = lambda t: (t["date"], t["side"], t["entry_dt"], t["exit_dt"], t["entry"], t["exit"])        # noqa: E731
        assert {key(t) for t in part["trade_list"]} == {key(t) for t in full["trade_list"] if t["date"] <= cut_day.isoformat()} and part["trades"] > 0
