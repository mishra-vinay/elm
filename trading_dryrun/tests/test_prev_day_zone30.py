"""Previous-day 30% opening zones: the zone of the day's open picks the side once a day; entry after the first candle; one trade a day."""
from contextlib import contextmanager
from datetime import datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient

from dryrun.app import create_app
from dryrun.config import Settings
from dryrun.replay import replay_range
from dryrun.sim_feed import ReplayFeed
from dryrun.strategies.prev_day_zone30 import RULES, SPEC, ZoneEngine
from dryrun.strategy import Params, make_engine

H, L = 105.0, 100.0           # D = 5; the 30% zones: BUY [103.5, 105], SELL [100, 101.5]
D0 = datetime(2026, 10, 13)
pytestmark = pytest.mark.usefixtures("only_shipped")


def P():
    return Params(strategy="prev_day_zone30", slippage_pct=0, cost_pct=0)


@contextmanager
def rules(**r):
    """The strategy's numbers live in RULES (no settings form): a test changes them here and puts them back."""
    old = dict(RULES)
    RULES.update(r)
    try:
        yield
    finally:
        RULES.clear()
        RULES.update(old)


def at(h, m, s=0):
    return D0.replace(hour=h, minute=m, second=s)


def play(ticks, session_open=time(9, 0), day_open=None, **r):
    with rules(**r):
        e = make_engine(H, L, P())
        assert isinstance(e, ZoneEngine)
        e.session_open = session_open
        e.day_open = day_open
        ev = []
        for ts, px in ticks:
            ev += e.on_tick(ts, px)
    return e, ev


def notes(ev):
    return [x.reason for x in ev if x.kind == "note"]


def test_open_in_the_upper_zone_buys_after_the_first_candle_with_stop_pdl_and_target_1_3_times_the_new_stop_distance():
    e, ev = play([(at(9, 0), 104.0), (at(9, 2), 104.2), (at(9, 4, 59), 104.3), (at(9, 5), 104.4), (at(9, 7), 104.7), (at(9, 8), 109.7)])
    [t] = e.closed
    assert t["side"] == "BUY" and t["entry_ts"] == at(9, 5) and t["entry_price"] == pytest.approx(104.4)      # the first price at/after 09:05, not the open of 104.0
    assert t["stop"] == pytest.approx(102.5) and t["target"] == pytest.approx(104.4 + 1.3 * 1.9) and t["exit_reason"] == "target"     # stop = the centre 102.5 (1.9 below), target 1.3 x that above
    assert t["qty"] == 5263                                                                                      # Rs 10,000 / 1.9 per unit (no costs in this test), whole units (no lot size known)
    assert e.day_open == 104.0 and "inside the BUY zone 103.50 to 105.00" in notes(ev)[0] and "BUY is planned" in notes(ev)[0]
    assert not [x for x in ev if x.kind == "entry" and x.ts < at(9, 5)]                                         # nothing in the first candle


def test_open_in_the_lower_zone_sells_with_stop_pdh_and_target_1_3_times_the_new_stop_distance():
    e, ev = play([(at(9, 0), 101.0), (at(9, 5), 100.9), (at(9, 6), 100.4), (at(9, 7), 95.9)])
    [t] = e.closed
    assert t["side"] == "SELL" and t["entry_price"] == pytest.approx(100.9) and t["stop"] == pytest.approx(102.5) and t["target"] == pytest.approx(100.9 - 1.3 * 1.6) and t["exit_reason"] == "target" and t["qty"] == 6250
    assert "inside the SELL zone 100.00 to 101.50" in notes(ev)[0]


@pytest.mark.parametrize("open_px,where", [(102.0, "between the two zones"), (106.0, "above the previous High"), (99.0, "below the previous Low")])
def test_an_open_outside_both_zones_means_no_trade_all_day(open_px, where):
    e, ev = play([(at(9, 0), open_px), (at(9, 5), 104.0), (at(9, 30), 101.0), (at(10, 0), 104.4)])         # later prices in the zones do not matter
    assert e.closed == [] and e.position is None and e.side is None
    assert where in notes(ev)[0] and "no trade today" in notes(ev)[0]


def test_one_trade_a_day_even_after_the_stop_is_hit():
    e, ev = play([(at(9, 0), 104.0), (at(9, 5), 104.4), (at(9, 10), 100.0), (at(9, 15), 104.0), (at(9, 20), 104.4), (at(9, 25), 103.8), (at(9, 30), 104.5)])
    [t] = e.closed
    assert t["exit_reason"] == "stop" and len(ev) and sum(1 for x in ev if x.kind == "entry") == 1
    assert e.position is None


def test_the_decision_uses_the_real_open_even_if_the_first_price_seen_is_later():
    e, ev = play([(at(9, 6), 104.0), (at(9, 7), 104.2), (at(9, 8), 108.0)], day_open=104.0)               # a run started after the open: the runner supplies the open
    assert e.side == "BUY" and e.day_open == 104.0 and e.closed and e.closed[0]["entry_price"] == pytest.approx(104.0)


def test_a_run_that_starts_too_late_or_after_the_cutoff_skips_the_day():
    e, ev = play([(at(11, 0), 104.0), (at(11, 5), 104.4)], day_open=104.0)
    assert e.closed == [] and e.position is None and any("too late" in n for n in notes(ev))
    e, ev = play([(at(17, 56), 104.0), (at(18, 1), 104.4), (at(18, 2), 104.6)], session_open=time(17, 56))     # entry moment 18:01 is after the 18:00 cut-off
    assert e.closed == [] and e.position is None and any("no new entries from 18:00" in n for n in notes(ev))
    e, ev = play([(at(17, 50), 104.0), (at(17, 55), 104.4)], session_open=time(17, 50))                       # 17:55 is before the cut-off
    assert e.position is not None
    e, ev = play([(at(21, 0), 104.0), (at(21, 5), 104.4)], session_open=time(21, 0), last_entry=None)
    assert e.position is not None


def test_stop_is_the_centre_of_yesterdays_range_and_the_target_follows_the_x_stop_setting():
    e, _ = play([(at(9, 0), 104.0), (at(9, 5), 104.4)], target_x_stop=2.0)
    assert e.position.target == pytest.approx(104.4 + 2.0 * 1.9) and e.position.stop == pytest.approx(102.5)
    e, _ = play([(at(9, 0), 101.0), (at(9, 5), 100.9)])                                                      # SELL: the stop is the same centre, above the entry
    assert e.position.target == pytest.approx(100.9 - 1.3 * 1.6) and e.position.stop == pytest.approx(102.5)
    e, _ = play([(at(9, 0), 104.0), (at(9, 5), 104.4)], max_loss_rs=None)                                    # no maximum loss: the run's fixed quantity
    assert e.position.qty == 1.0
    e, ev = play([(at(9, 0), 104.0), (at(9, 5), 102.0)])                                                    # BUY planned, but by the entry moment price is already below the centre: no stop distance
    assert e.position is None and e.closed == [] and any("already crossed the centre 102.50" in n for n in notes(ev))
    e, ev = play([(at(9, 0), 101.0), (at(9, 5), 103.0)])                                                    # SELL planned, but price is already above the centre
    assert e.position is None and any("already crossed the centre" in n for n in notes(ev))


def test_levels_status_preview_and_no_settings_form():
    e = make_engine(H, L, P())
    assert [x["name"] for x in e.levels()] == ["prev High", "BUY zone from", "SELL zone to", "centre (stop)", "prev Low"]
    assert [round(x["value"], 2) for x in e.levels()] == [105.0, 103.5, 101.5, 102.5, 100.0]
    assert "Waiting" in e.status_text(None)
    p = SPEC.preview(105, 100, {})
    assert "103.50" in p["summary"] and "101.50" in p["summary"] and "target = 1.3 × stop distance" in p["summary"]
    assert "103.50" in p["cards"][0]["rows"][0][1] and "102.50" in p["cards"][0]["rows"][1][1] and "middle of the previous High and Low" in p["cards"][0]["rows"][1][1] and "entry + 1.3 × stop distance" in p["cards"][0]["rows"][2][1]
    assert "stop 102.50, target 106.5" in p["cards"][0]["rows"][3][1]                                       # an entry in the middle of the BUY zone (104.25): stop 1.75 below, target 1.3 x 1.75 above
    assert "at most Rs 10,000" in " ".join(p["notes"])
    with rules(target_x_stop=2.0):
        assert "target = 2 × stop distance" in SPEC.preview(105, 100, {})["summary"]
    assert "30% opening zones" in SPEC.describe(105, 100, {})
    assert SPEC.fields == [] and SPEC.blurb and "opening price" in SPEC.blurb
    assert RULES["zone_pct"] == 30.0 and RULES["skip_minutes"] == 5 and RULES["last_entry"] == time(18, 0) and RULES["target_x_stop"] == 1.3  and RULES["max_loss_rs"] == 10000


def test_replay_enters_at_the_open_of_the_second_candle_with_the_stored_session_open():
    day = lambda d, rows: [{"ts": datetime(2026, 10, d, 9, 15) + timedelta(minutes=5 * k), "open": a, "high": b, "low": c, "close": e} for k, (a, b, c, e) in enumerate(rows)]      # noqa: E731
    candles = day(12, [(102, 105, 100, 103)]) + day(13, [(104.0, 104.3, 103.9, 104.2), (104.4, 110.0, 104.3, 109.9)])
    r = replay_range(candles, P(), "NSE_EQ")
    [t] = r["per_day"][datetime(2026, 10, 13).date()]["trade_list"]
    assert t["side"] == "BUY" and t["entry_ts"].time() == time(9, 20) and t["entry_price"] == pytest.approx(104.4) and t["exit_reason"] == "target" and t["exit_price"] == pytest.approx(104.4 + 1.3 * 1.9)
    assert r["per_day"][datetime(2026, 10, 13).date()]["levels"][1]["value"] == pytest.approx(103.5)


def test_the_strategy_works_through_the_api(tmp_path):
    st = Settings(database_url=f"sqlite:///{tmp_path}/z.db", feed_mode="sim")
    app = create_app(st, feed_factory=lambda t, n, s: ReplayFeed(t, [], D0, on_note=n, on_status=s), load_master=False)
    with TestClient(app) as c:
        names = [s["name"] for s in c.get("/api/strategies").json()["strategies"]]
        assert sorted(names) == ["market_shift", "market_shift_sell", "prev_day_zone30", "prev_day_zone30_buy_center", "range_thirds"]
        assert all(x["fields"] == [] for x in c.get("/api/strategies").json()["strategies"])
        p = c.post("/api/strategies/prev_day_zone30/preview", json={"high": 105, "low": 100, "options": {}}).json()
        assert "103.50" in p["summary"]
        r = c.post("/api/runs", json={"query": "BAJFINANCE", "prev_high": 105, "prev_low": 100, "params": {"strategy": "prev_day_zone30"}})
        assert r.status_code == 200, r.text
        run = c.get("/api/state").json()["runs"][0]
        assert run["strategy"] == "prev_day_zone30" and [x["name"] for x in run["levels"]] == ["prev High", "BUY zone from", "SELL zone to", "centre (stop)", "prev Low"]
        assert c.post("/api/runs", json={"query": "BAJFINANCE", "prev_high": 105, "prev_low": 100, "params": {}}).status_code == 400          # two strategies: one must be named
