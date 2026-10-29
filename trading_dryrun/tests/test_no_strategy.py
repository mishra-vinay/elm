"""The shipped app has no strategy installed: everything that needs one says so clearly, and the registry works for a strategy added later."""
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from demo_strategy import jp
from dryrun import strategy as fw
from dryrun.app import create_app
from dryrun.config import Settings
from dryrun.sim_feed import ReplayFeed

ROOT = Path(__file__).resolve().parents[1]


def make(tmp_path):
    st = Settings(database_url=f"sqlite:///{tmp_path}/n.db", feed_mode="sim")
    return create_app(st, feed_factory=lambda t, n, s: ReplayFeed(t, [], datetime.now(), on_note=n, on_status=s), load_master=False)


def test_the_shipped_package_installs_the_two_strategies():
    out = subprocess.run([sys.executable, "-c", "import dryrun.strategies, dryrun.strategy as s; print(sorted(s.STRATEGIES))"],
                         cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "['market_shift', 'market_shift_sell', 'prev_day_zone30', 'prev_day_zone30_buy_center', 'range_thirds', 'range_thirds_research']"


def test_with_nothing_installed_everything_that_needs_a_strategy_says_so(tmp_path, no_strategies):
    with TestClient(make(tmp_path)) as c:
        d = c.get("/api/strategies").json()
        assert d["strategies"] == [] and "No strategy is installed yet" in d["message"]
        r = c.post("/api/runs", json={"query": "BAJFINANCE", "prev_high": 100, "prev_low": 95})
        assert r.status_code == 400 and "No strategy is installed yet" in r.json()["detail"]
        r = c.post("/api/backtest", json={"instrument_id": 1, "interval": "1m"})
        assert r.status_code == 400 and "No strategy is installed yet" in r.json()["detail"]
        assert c.post("/api/strategies/anything/preview", json={"high": 2, "low": 1}).status_code == 404
        # the pages that do not depend on a strategy still work
        for path in ("/", "/ticks", "/backtest", "/market-data", "/settings", "/health"):
            assert c.get(path).status_code == 200
        assert c.get("/api/status").status_code == 200 and c.get("/api/state").json()["runs"] == []
    with pytest.raises(fw.NoStrategyError):
        fw.make_engine(100, 95, fw.Params(strategy="x"))


def test_a_registered_strategy_appears_with_its_form_and_preview(tmp_path):
    with TestClient(make(tmp_path)) as c:
        d = c.get("/api/strategies").json()
        [s] = d["strategies"]
        assert s["name"] == "demo" and [f["key"] for f in s["fields"]] == ["pullback_pct", "target_pct", "stop_pct"] and s["has_preview"]
        p = c.post("/api/strategies/demo/preview", json={"high": 100, "low": 95, "options": {"pullback_pct": 2.0}}).json()
        assert p["cards"][0]["rows"][0][1] == "98.00"
        assert c.post("/api/strategies/demo/preview", json={"high": 90, "low": 95}).status_code == 400
        assert c.post("/api/strategies/nope/preview", json={"high": 100, "low": 95}).status_code == 404
        # explicit strategy, the single installed strategy as the default, and unknown names
        assert c.post("/api/runs", json={"query": "BAJFINANCE", "prev_high": 100, "prev_low": 95, "params": jp()}).status_code == 200
        assert c.post("/api/runs", json={"query": "BAJFINANCE", "prev_high": 100, "prev_low": 95, "params": {}}).status_code == 200
        r = c.post("/api/runs", json={"query": "BAJFINANCE", "prev_high": 100, "prev_low": 95, "params": {"strategy": "nope"}})
        assert r.status_code == 400 and "unknown strategy 'nope'" in r.json()["detail"]
        run = c.get("/api/state").json()["runs"][0]
        assert run["strategy"] == "demo" and [x["name"] for x in run["levels"]] == ["prev High", "buy trigger", "sell trigger", "prev Low"]


def test_two_installed_strategies_need_an_explicit_choice(tmp_path):
    other = fw.StrategySpec(name="other", label="Other", engine=fw.StrategyEngine)
    fw.register(other)
    try:
        with TestClient(make(tmp_path)) as c:
            r = c.post("/api/runs", json={"query": "BAJFINANCE", "prev_high": 100, "prev_low": 95, "params": {}})
            assert r.status_code == 400 and "unknown strategy ''" in r.json()["detail"]
            assert c.post("/api/runs", json={"query": "BAJFINANCE", "prev_high": 100, "prev_low": 95, "params": jp()}).status_code == 200
    finally:
        fw.unregister("other")


def test_the_base_engine_requires_a_strategy_to_implement_evaluate():
    eng = fw.StrategyEngine(100, 95, fw.Params(strategy="demo"))
    with pytest.raises(NotImplementedError):
        eng.on_tick(datetime(2026, 10, 13, 10, 0), 99.0)


def test_runs_made_with_retired_strategies_are_labelled_in_the_history(tmp_path):
    from datetime import date
    from dryrun.db import Instrument, Run
    from sqlalchemy import select
    with TestClient(make(tmp_path)) as c:
        app = c.app
        with app.state.factory() as s:
            iid = s.scalar(select(Instrument.id).limit(1))
            s.add(Run(instrument_id=iid, status="stopped", trading_date=date(2026, 10, 12), params={"pullback_pct": 1.0, "target_pct": 2.0, "stop_pct": 1.0}))
            s.add(Run(instrument_id=iid, status="stopped", trading_date=date(2026, 10, 13), params={"strategy": "thirds", "entry_mode": "approach", "entry_frac": 0.1667, "stop_frac": 0.3333}))
            s.add(Run(instrument_id=iid, status="stopped", trading_date=date(2026, 10, 14), params={"strategy": "demo", "options": {"target_pct": 3.0}}))
            s.commit()
        hist = {h["date"]: h["settings"] for h in c.get("/api/state").json()["history"]}
        assert hist["2026-10-12"] == {"strategy": "pullback (retired)", "options": {"pullback_pct": 1.0, "target_pct": 2.0, "stop_pct": 1.0}}
        assert hist["2026-10-13"]["strategy"] == "thirds (retired)" and hist["2026-10-13"]["options"]["entry_frac"] == 0.1667
        assert hist["2026-10-14"] == {"strategy": "demo", "options": {"target_pct": 3.0}}
