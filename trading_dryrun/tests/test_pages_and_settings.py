import base64
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from demo_strategy import jp
from dryrun.app import create_app, save_env_value, token_info
from dryrun.config import Settings
from dryrun.sim_feed import ReplayFeed

START = datetime(2026, 10, 9, 10, 0, 0)
BUY_PATH = [99.0, 99.5, 100.4, 101.0, 100.2, 99.0, 99.8, 100.5, 101.2, 101.5]


def jwt(exp_in_seconds):
    b64 = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")      # noqa: E731
    exp = int((datetime.now(timezone.utc) + timedelta(seconds=exp_in_seconds)).timestamp())
    return f"{b64({'alg': 'HS512'})}.{b64({'dhanClientId': '1114191751', 'exp': exp})}.sig"


def wait_for(fn, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("timed out")


def sim_app(tmp_path, prices, pause=0.0, **kw):
    st = Settings(database_url=f"sqlite:///{tmp_path}/p.db", feed_mode="sim", **kw)
    return create_app(st, feed_factory=lambda t, n, s: ReplayFeed(t, prices, START, on_note=n, on_status=s, pause=pause), load_master=False)


def dhan_app(tmp_path, token):
    st = Settings(database_url=f"sqlite:///{tmp_path}/d.db", feed_mode="dhan", dhan_client_id="1114191751", dhan_access_token=token)
    return create_app(st, feed_factory=lambda t, n, s: ReplayFeed(t, [], START, on_note=n, on_status=s), load_master=False)


def test_three_pages_are_served_with_navigation_and_shared_assets(tmp_path):
    with TestClient(sim_app(tmp_path, [1.0])) as c:
        for path, marker in (("/", "Start a dry run on live market data"), ("/runs", "Finished runs"),
                             ("/market-data", "Stored candles"), ("/settings", "Dhan access token")):
            r = c.get(path)
            assert r.status_code == 200 and marker in r.text, path
            assert '<div id="activity">' in r.text or 'id="activity"' in r.text           # activity log on every page
        assert c.get("/static/common.js").status_code == 200 and c.get("/static/app.css").status_code == 200
        assert "/market-data" in c.get("/static/common.js").text and "/settings" in c.get("/static/common.js").text
        s = c.get("/api/status").json()
        assert s["mode"] == "sim" and s["active_runs"] == 0 and "feed" in s


def test_several_runs_on_the_same_instrument_with_different_settings(tmp_path):
    app = sim_app(tmp_path, BUY_PATH, pause=0.25)             # slow enough that both runs are registered before the prices play
    with TestClient(app) as c:
        a = c.post("/api/runs", json={"query": "BAJFINANCE", "prev_high": 100, "prev_low": 95, "label": "wide target",
                                       "params": jp(target_pct=2.0, slippage_pct=0, cost_pct=0)})
        b = c.post("/api/runs", json={"query": "BAJFINANCE", "prev_high": 100, "prev_low": 95, "label": "quick target",
                                       "params": jp(target_pct=0.5, stop_pct=0.5, slippage_pct=0, cost_pct=0)})
        assert a.status_code == 200 and b.status_code == 200 and a.json()["id"] != b.json()["id"]
        runs = c.get("/api/state").json()["runs"]
        assert {r["label"] for r in runs} == {"wide target", "quick target"}
        assert c.get("/api/status").json()["active_runs"] == 2
        wait_for(lambda: all(r["summary"]["trades"] == 1 for r in c.get("/api/state").json()["runs"]))
        by_label = {r["label"]: r for r in c.get("/api/state").json()["runs"]}
        assert by_label["wide target"]["params"]["options"]["target_pct"] == 2.0 and by_label["quick target"]["params"]["options"]["target_pct"] == 0.5
        assert c.post("/api/runs/stop-all").json() == {"stopped": 2}
        hist = c.get("/api/state").json()["history"]
        assert {h["label"] for h in hist} == {"wide target", "quick target"}
        assert all(h["ran_ok"] and h["trades"] == 1 and h["settings"]["strategy"] == "demo" and h["settings"]["options"]["target_pct"] in (2.0, 0.5) for h in hist)
        text = "\n".join(r["message"] for r in c.get("/api/activity").json())
        assert "#1 wide target]" in text and "#2 quick target]" in text


def test_active_run_cap(tmp_path):
    from dryrun import runner
    with TestClient(sim_app(tmp_path, [1.0])) as c:
        old = runner.MAX_ACTIVE_RUNS
        runner.MAX_ACTIVE_RUNS = 2
        try:
            for _ in range(2):
                assert c.post("/api/runs", json={"query": "NIFTY 50", "prev_high": 100, "prev_low": 95}).status_code == 200
            r = c.post("/api/runs", json={"query": "NIFTY 50", "prev_high": 100, "prev_low": 95})
            assert r.status_code == 400 and "At most 2 runs" in r.json()["detail"]
        finally:
            runner.MAX_ACTIVE_RUNS = old


def test_settings_show_token_expiry_but_never_the_token(tmp_path):
    tok = jwt(5 * 3600)
    with TestClient(dhan_app(tmp_path, tok)) as c:
        r = c.get("/api/settings")
        assert tok not in r.text and "1114191751" not in r.text                  # neither the token nor the full client id
        s = r.json()
        assert s["client_id"] == "******1751" and s["token"]["status"] == "valid" and 4.9 * 3600 < s["token"]["seconds_left"] <= 5 * 3600
        assert s["mode"] == "dhan" and s["sessions"]["NSE_EQ"]["square_off"] == "15:15" and s["sessions"]["MCX_COMM"]["square_off"] == "23:20"
    assert token_info(jwt(-60))["status"] == "expired"
    assert token_info(jwt(3600))["status"] == "expiring soon"
    assert token_info("not-a-jwt") == {"set": True, "expires_at": None, "seconds_left": None, "status": "unknown"}
    assert token_info("") == {"set": False}


def test_dhan_connection_test_returns_only_safe_fields(tmp_path):
    with TestClient(dhan_app(tmp_path, jwt(3600))) as c:
        class FakeRest:
            def profile(self):
                return {"dhanClientId": "1114191751", "tokenValidity": "11/10/2026 15:00", "dataPlan": "Active",
                        "dataValidity": "2026-11-10 00:00:00", "someSecret": "do-not-return", "mobileNo": "9999999999"}
        c.app.state.manager.rest = FakeRest()
        r = c.post("/api/settings/test")
        assert r.status_code == 200 and r.json() == {"tokenValidity": "11/10/2026 15:00", "dataPlan": "Active", "dataValidity": "2026-11-10 00:00:00"}
        c.app.state.manager.rest = type("Bad", (), {"profile": lambda self: (_ for _ in ()).throw(RuntimeError("profile HTTP 401: invalid token"))})()
        r = c.post("/api/settings/test")
        assert r.status_code == 502 and "401" in r.json()["detail"]
        c.app.state.manager.writer.flush()          # the activity row is written by the background writer thread
        assert any("connection test failed" in x["message"] for x in c.get("/api/activity", params={"source": "errors"}).json())


def test_token_update_with_save_rewrites_only_that_env_line(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nFEED_MODE=dhan\nDHAN_CLIENT_ID=1114191751\nDHAN_ACCESS_TOKEN=old.old.old\nPORT=8000\n")
    monkeypatch.setenv("DRYRUN_ENV_FILE", str(env))
    new = jwt(24 * 3600)
    with TestClient(dhan_app(tmp_path, jwt(10))) as c:
        r = c.post("/api/feed/token", json={"token": new, "save": True})
        assert r.status_code == 200 and r.json()["saved_to_env"] is True and r.json()["token"]["status"] == "valid"
        assert new not in json.dumps(r.json())
        assert env.read_text() == f"# comment\nFEED_MODE=dhan\nDHAN_CLIENT_ID=1114191751\nDHAN_ACCESS_TOKEN={new}\nPORT=8000\n"
        assert oct(env.stat().st_mode & 0o777) == "0o600"
        assert c.get("/api/settings").json()["token"]["status"] == "valid"
        # without save the file is untouched, and a missing file is reported rather than crashing
        other = jwt(2 * 24 * 3600)
        assert c.post("/api/feed/token", json={"token": other}).json()["saved_to_env"] is None
        assert other not in env.read_text()
        monkeypatch.setenv("DRYRUN_ENV_FILE", str(tmp_path / "missing.env"))
        assert c.post("/api/feed/token", json={"token": other, "save": True}).json()["saved_to_env"] is False
        assert c.post("/api/feed/token", json={"token": "short"}).status_code == 400


def test_save_env_value_appends_when_key_is_missing(tmp_path):
    p = tmp_path / "e.env"
    p.write_text("A=1\n")
    save_env_value("DHAN_ACCESS_TOKEN", "xyz", str(p))
    assert p.read_text() == "A=1\nDHAN_ACCESS_TOKEN=xyz\n"


def test_big_responses_are_gzipped_and_small_ones_are_not(tmp_path):
    with TestClient(sim_app(tmp_path, [1.0])) as c:
        js = c.get("/static/common.js", headers={"Accept-Encoding": "gzip"})
        assert js.headers.get("content-encoding") == "gzip" and "renderHeader" in js.text           # decoded transparently by the client
        small = c.get("/health", headers={"Accept-Encoding": "gzip"})
        assert small.headers.get("content-encoding") is None


def test_data_summary_is_cached_briefly_and_dropped_after_a_delete(tmp_path):
    from datetime import datetime as dt
    from dryrun.history import store_candles
    from dryrun import instruments as instr
    app = sim_app(tmp_path, [1.0])
    with TestClient(app) as c:
        f = app.state.factory
        i = instr.search(f, "BAJFINANCE", 1)[0]
        row = {"ts": dt(2026, 10, 9, 9, 15), "open": 1, "high": 1, "low": 1, "close": 1}
        store_candles(f, i.id, "1m", [row], "api")
        assert c.get("/api/data/summary").json()["candles"][0]["count"] == 1
        store_candles(f, i.id, "1m", [{**row, "ts": dt(2026, 10, 9, 9, 16)}], "api")
        assert c.get("/api/data/summary").json()["candles"][0]["count"] == 1          # served from the 20-second cache
        c.delete("/api/candles", params={"instrument_id": i.id, "interval": "D"})      # any delete drops the cache
        assert c.get("/api/data/summary").json()["candles"][0]["count"] == 2


def test_status_snapshot_matches_what_the_header_needs(tmp_path):
    with TestClient(sim_app(tmp_path, [1.0])) as c:
        s = c.get("/api/status").json()
        assert set(s) == {"now", "mode", "writer_errors", "active_runs", "feed"}
        assert set(s["feed"]) == {"connected", "fatal", "disconnects", "subscribed", "last_message_age"}


def test_instrument_list_download_is_skipped_when_fresh(tmp_path):
    from dryrun import instruments as instr
    from dryrun.db import ApiLog, Instrument, init_db
    _e, f = init_db(f"sqlite:///{tmp_path}/r.db")
    instr.seed_defaults(f)
    assert instr.refreshed_recently(f) is False                                      # starter list only
    with f() as s:
        s.add_all([Instrument(segment="NSE_EQ", security_id=str(10000 + k), instrument="EQUITY", symbol=f"S{k}") for k in range(1100)])
        s.commit()
    assert instr.refreshed_recently(f) is False                                      # many rows but never loaded through the app
    with f() as s:
        s.add(ApiLog(source="data", level="info", message="Instrument list loaded (25000 new instruments)"))
        s.commit()
    assert instr.refreshed_recently(f) is True
    assert instr.refreshed_recently(f, hours=0) is False


def test_backtest_page_is_slim_and_its_script_is_a_separate_file(tmp_path):
    with TestClient(sim_app(tmp_path, [1.0])) as c:
        page = c.get("/backtest").text
        assert 'id="activity"' not in page and "Activity log" not in page                          # no activity log on this page
        assert "Skip sessions" not in page and "minbars" not in page                                # the completeness filter is automatic, not a control
        assert '<script src="/static/backtest.js' in page and "function runBt" not in page          # the page's code lives in its own file
        js = c.get("/static/backtest.js")
        assert js.status_code == 200 and "function runBt" in js.text and "min_bars_pct" not in js.text
        assert c.get("/runs").text.count('id="activity"') == 1                                      # the other pages keep theirs
        assert '<details class="more">' in page and 'id="stratBox"' in page and 'id="s_fields"' not in page                     # no strategy settings on the page


@pytest.mark.usefixtures("only_shipped")
def test_strategies_describe_themselves_and_have_no_settings_form(tmp_path):
    with TestClient(sim_app(tmp_path, [1.0])) as c:
        listed = {s["name"]: s for s in c.get("/api/strategies").json()["strategies"]}
    assert set(listed) == {"range_thirds", "prev_day_zone30", "prev_day_zone30_buy_center", "market_shift", "market_shift_sell"}                       # the research version is hidden
    assert "yesterday" in listed["range_thirds"]["blurb"] and "opening price" in listed["prev_day_zone30"]["blurb"]
    assert all(v["fields"] == [] and v["blurb"] for v in listed.values()) and listed["prev_day_zone30_buy_center"]["label"] == "30% opening buy at center"
