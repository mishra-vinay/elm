import base64
import json
import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

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
                                       "params": {"target_pct": 2.0, "slippage_pct": 0, "cost_pct": 0}})
        b = c.post("/api/runs", json={"query": "BAJFINANCE", "prev_high": 100, "prev_low": 95, "label": "quick target",
                                       "params": {"target_pct": 0.5, "stop_pct": 0.5, "slippage_pct": 0, "cost_pct": 0}})
        assert a.status_code == 200 and b.status_code == 200 and a.json()["id"] != b.json()["id"]
        runs = c.get("/api/state").json()["runs"]
        assert {r["label"] for r in runs} == {"wide target", "quick target"}
        assert c.get("/api/status").json()["active_runs"] == 2
        wait_for(lambda: all(r["summary"]["trades"] == 1 for r in c.get("/api/state").json()["runs"]))
        by_label = {r["label"]: r for r in c.get("/api/state").json()["runs"]}
        assert by_label["wide target"]["params"]["target_pct"] == 2.0 and by_label["quick target"]["params"]["target_pct"] == 0.5
        assert c.post("/api/runs/stop-all").json() == {"stopped": 2}
        hist = c.get("/api/state").json()["history"]
        assert {h["label"] for h in hist} == {"wide target", "quick target"}
        assert all(h["ran_ok"] and h["trades"] == 1 and h["settings"]["pullback_pct"] == 1.0 for h in hist)
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
        assert s["mode"] == "dhan" and s["sessions"]["NSE_EQ"]["square_off"] == "15:15" and s["sessions"]["MCX_COMM"]["square_off"] == "23:15"
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
