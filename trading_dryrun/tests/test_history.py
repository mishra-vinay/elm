from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from dryrun import instruments as instr
from dryrun import levels
from dryrun.app import create_app
from dryrun.config import Settings
from dryrun.db import Candle, init_db
from dryrun.history import backfill, store_candles
from dryrun.sim_feed import ReplayFeed

START = datetime(2026, 10, 9, 10, 0, 0)


class FakeRest:
    """Records the ranges asked for and returns one candle per requested weekday (daily) or per minute of the first day."""
    def __init__(self):
        self.daily_calls, self.intraday_calls = [], []

    def daily(self, seg, sid, instr_type, start, end, expiry_code=None):
        self.daily_calls.append((start, end))
        days = [start + timedelta(days=k) for k in range((end - start).days) if (start + timedelta(days=k)).weekday() < 5]
        return [{"ts": datetime.combine(d, datetime.min.time()), "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1} for d in days]

    def intraday(self, seg, sid, instr_type, interval, start, end, expiry_code=None):
        self.intraday_calls.append((interval, start, end))
        return [{"ts": datetime.combine(start, datetime.min.time()).replace(hour=9, minute=15) + timedelta(minutes=k),
                 "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1} for k in range(5)]


@pytest.fixture()
def factory(tmp_path):
    _e, f = init_db(f"sqlite:///{tmp_path}/h.db")
    instr.seed_defaults(f)
    return f


def test_second_download_only_asks_for_what_is_missing(factory):
    i = instr.search(factory, "BAJFINANCE", 1)[0]
    r1 = FakeRest()
    first = backfill(factory, r1, i, daily_from=date(2026, 9, 1), intraday_days=30)
    assert first["daily"] > 0 and r1.daily_calls[0][0] == date(2026, 9, 1)
    r2 = FakeRest()
    second = backfill(factory, r2, i, daily_from=date(2026, 9, 1), intraday_days=30)
    assert r2.daily_calls[0][0] > date(2026, 9, 1)                       # incremental: starts near the last stored day
    assert r2.intraday_calls[0][1] >= r1.intraday_calls[0][1]
    assert second["daily"] <= 5                                           # only the re-fetched tail, nothing duplicated
    r3 = FakeRest()
    backfill(factory, r3, i, daily_from=date(2026, 9, 1), intraday_days=30, incremental=False)
    assert r3.daily_calls[0][0] == date(2026, 9, 1)                       # full re-download on request
    with factory() as s:
        ts = [c.ts for c in s.scalars(select(Candle).where(Candle.interval == "D"))]
    assert len(ts) == len(set(ts))                                        # never two rows for the same day


def test_official_candles_replace_live_and_sim_candles_but_not_each_other(factory):
    i = instr.search(factory, "BAJFINANCE", 1)[0]
    t = datetime(2026, 10, 9, 9, 15)
    store_candles(factory, i.id, "1m", [{"ts": t, "open": 1, "high": 2, "low": 1, "close": 2}], "live")
    assert store_candles(factory, i.id, "1m", [{"ts": t, "open": 10, "high": 12, "low": 9, "close": 11, "volume": 5}], "api") == 1
    assert store_candles(factory, i.id, "1m", [{"ts": t, "open": 99, "high": 99, "low": 99, "close": 99}], "api") == 0
    assert store_candles(factory, i.id, "1m", [{"ts": t, "open": 77, "high": 77, "low": 77, "close": 77}], "live") == 0
    with factory() as s:
        c = s.scalar(select(Candle))
        assert (c.open, c.close, c.source, c.volume) == (10, 11, "api", 5)


def test_stale_daily_candle_is_refreshed_from_dhan(factory):
    i = instr.search(factory, "BAJFINANCE", 1)[0]
    store_candles(factory, i.id, "D", [{"ts": datetime(2026, 10, 2), "open": 1, "high": 500, "low": 400, "close": 1}], "api")   # a week old
    lv = levels.resolve(factory, i, date(2026, 10, 12), rest=None)
    assert lv.high == 500 and lv.from_date == date(2026, 10, 2)            # no Dhan access: falls back to what is stored

    class R(FakeRest):
        def daily(self, *a, **k):
            return [{"ts": datetime(2026, 10, 9), "open": 1, "high": 977.0, "low": 951.0, "close": 1}]
    lv = levels.resolve(factory, i, date(2026, 10, 12), rest=R())
    assert (lv.high, lv.low, lv.from_date) == (977.0, 951.0, date(2026, 10, 9))
    # Friday's candle is current for a Monday run: no Dhan call is needed
    class Boom(FakeRest):
        def daily(self, *a, **k):
            raise AssertionError("should not be called")
    assert levels.resolve(factory, i, date(2026, 10, 12), rest=Boom()).high == 977.0


def make_app(tmp_path, **kw):
    st = Settings(database_url=f"sqlite:///{tmp_path}/a.db", feed_mode="sim", **kw)
    return create_app(st, feed_factory=lambda t, n, s: ReplayFeed(t, [], START, on_note=n, on_status=s), load_master=False)


def test_delete_candles_endpoint(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as c:
        f = app.state.factory
        i = instr.search(f, "BAJFINANCE", 1)[0]
        store_candles(f, i.id, "1m", [{"ts": datetime(2026, 10, 9, 9, 15), "open": 1, "high": 1, "low": 1, "close": 1}], "api")
        store_candles(f, i.id, "D", [{"ts": datetime(2026, 10, 9), "open": 1, "high": 1, "low": 1, "close": 1}], "api")
        summ = c.get("/api/data/summary").json()
        assert {x["interval"] for x in summ["candles"]} == {"1m", "D"} and summ["candles"][0]["days"] == 1
        assert c.delete("/api/candles", params={"instrument_id": i.id, "interval": "1m"}).json() == {"deleted": 1}
        assert {x["interval"] for x in c.get("/api/data/summary").json()["candles"]} == {"D"}
        assert c.delete("/api/candles", params={"instrument_id": i.id, "interval": "bogus"}).status_code == 400
        assert c.delete("/api/candles", params={"instrument_id": i.id}).json() == {"deleted": 1}
        assert c.get("/api/data/summary").json()["candles"] == []


def test_watch_list_persists_and_live_collection_resubscribes_after_restart(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as c:
        i = instr.search(app.state.factory, "BAJFINANCE", 1)[0]
        assert c.post("/api/watch", json={"instrument_id": i.id, "on": True}).status_code == 200
        c.get("/api/state")
        app.state.manager.writer.flush()
        w = c.get("/api/data/summary").json()["watchlist"]
        assert w and w[0]["collect_live"] is True
    app2 = make_app(tmp_path)
    with TestClient(app2) as c:
        s = c.get("/api/state").json()
        assert any("Bajaj Finance" in x["instrument"] for x in s["watch"])      # collecting again without any click
        assert s["feed"]["subscribed"] == 1


def test_update_history_now_uses_stored_intervals_and_flags(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app):
        f = app.state.factory
        i = instr.search(f, "BAJFINANCE", 1)[0]
        m = app.state.manager
        m.rest = FakeRest()
        m.set_watch_flags(i.id, daily_update=True)
        store_candles(f, i.id, "5m", [{"ts": datetime(2026, 10, 9, 9, 15), "open": 1, "high": 1, "low": 1, "close": 1}], "api")
        m.writer.flush()
        res = m.update_history_now()
        assert res["updated"] == 1 and not res["failed"]
        assert m.rest.intraday_calls and m.rest.intraday_calls[0][0] == 5     # refreshed the interval that was stored
