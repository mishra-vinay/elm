"""30% opening buy at center: only a day that opens in the top 30% zone is traded, as a BUY resting at the centre of yesterday's range; stop PDL; target 1.4 x the stop distance."""
from contextlib import contextmanager
from datetime import datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient

from dryrun.app import create_app
from dryrun.config import Settings
from dryrun.replay import replay_range
from dryrun.sim_feed import ReplayFeed
from dryrun.strategies.prev_day_zone30_buy_center import RULES, SPEC, CenterEngine
from dryrun.strategy import Params, make_engine

H, L = 105.0, 100.0          # D = 5; top zone [103.5, 105]; centre 102.5; stop 100; target = entry + 1.4 x (entry - 100)
D0 = datetime(2026, 10, 13)
pytestmark = pytest.mark.usefixtures("only_shipped")


def P(**kw):
    return Params(strategy="prev_day_zone30_buy_center", slippage_pct=kw.get("slip", 0), cost_pct=kw.get("cost", 0))


@contextmanager
def rules(**r):
    old = dict(RULES)
    RULES.update(r)
    try:
        yield
    finally:
        RULES.clear()
        RULES.update(old)


def at(h, m, s=0):
    return D0.replace(hour=h, minute=m, second=s)


def play(ticks, session_open=time(9, 0), day_open=None, symbol=None, params=None, **r):
    with rules(**r):
        e = make_engine(H, L, params or P())
        assert isinstance(e, CenterEngine)
        e.session_open, e.day_open, e.symbol = session_open, day_open, symbol
        ev = []
        for ts, px in ticks:
            ev += e.on_tick(ts, px)
    return e, ev


def notes(ev):
    return [x.reason for x in ev if x.kind == "note"]


def test_open_in_the_top_zone_buys_when_price_comes_down_to_the_centre():
    e, ev = play([(at(9, 0), 104.0), (at(9, 5), 103.8), (at(9, 10), 102.9), (at(9, 12), 102.5), (at(9, 30), 106.1)])
    [t] = e.closed
    assert t["side"] == "BUY" and t["entry_ts"] == at(9, 12) and t["entry_price"] == pytest.approx(102.5)
    assert t["stop"] == 100.0 and t["target"] == pytest.approx(102.5 + 1.4 * 2.5) and t["exit_reason"] == "target"          # risk 2.5, reward 3.5
    assert t["qty"] == 4000                                                                                                  # Rs 10,000 / 2.5 per unit, no costs in this test
    assert "inside the top zone 103.50 to 105.00" in notes(ev)[0] and "centre 102.50" in notes(ev)[0]


@pytest.mark.parametrize("open_px,where", [(101.0, "bottom zone"), (102.8, "between the zones"), (106.0, "above the previous High"), (99.0, "below the previous Low")])
def test_any_other_open_means_no_trade_all_day(open_px, where):
    e, ev = play([(at(9, 0), open_px), (at(9, 5), 102.6), (at(9, 10), 102.0), (at(9, 30), 101.0)])
    assert e.closed == [] and e.position is None and where in notes(ev)[0] and "no trade today" in notes(ev)[0]


def test_nothing_is_bought_while_price_stays_above_the_centre_and_the_cutoff_cancels_the_order():
    e, ev = play([(at(9, 0), 104.0), (at(9, 5), 103.0), (at(12, 0), 102.7), (at(17, 59), 102.6), (at(18, 1), 102.4), (at(18, 5), 101.0)])
    assert e.position is None and e.closed == [] and any("never came down to the centre 102.50 before 18:00" in n for n in notes(ev))
    e, _ = play([(at(9, 0), 104.0), (at(9, 5), 103.0), (at(17, 59), 102.5)])                                                # the same touch a minute before the cut-off buys
    assert e.position is not None


def test_the_first_candle_never_fills_and_a_price_already_at_the_centre_fills_at_once():
    e, _ = play([(at(9, 0), 104.0), (at(9, 2), 102.4), (at(9, 5), 102.4), (at(9, 6), 102.9)])                               # touched the centre in the first 5 minutes: no fill then
    assert e.position is not None and e.position.entry_ts == at(9, 5) and e.position.entry_price == pytest.approx(102.4)    # the order starts at 09:05 and fills at the market
    assert e.position.target == pytest.approx(102.4 + 1.4 * 2.4)
    e, ev = play([(at(9, 0), 104.0), (at(9, 5), 99.8), (at(9, 6), 102.0)])                                                  # already below the Low when the order starts: no stop distance
    assert e.position is None and e.closed == [] and any("at or below yesterday's Low" in n for n in notes(ev))


def test_one_trade_a_day_even_after_the_stop_is_hit():
    e, _ = play([(at(9, 0), 104.0), (at(9, 5), 103.0), (at(9, 10), 102.5), (at(9, 20), 100.0), (at(9, 30), 103.0), (at(9, 40), 102.5), (at(9, 50), 106.0)])
    [t] = e.closed
    assert t["exit_reason"] == "stop" and e.position is None


def test_a_run_started_too_late_skips_the_day_but_a_run_started_in_time_does_not():
    e, ev = play([(at(11, 0), 102.0), (at(11, 5), 102.0)], day_open=104.0)
    assert e.position is None and any("too late" in n for n in notes(ev))
    e, _ = play([(at(9, 6), 103.0), (at(9, 8), 102.5)], day_open=104.0)                                                      # the real open is supplied by the runner
    assert e.position is not None and e.day_open == 104.0


def test_quantity_comes_from_the_maximum_loss_in_whole_lots_and_a_too_big_lot_skips_the_day():
    big = Params(strategy="prev_day_zone30_buy_center", slippage_pct=0.02, cost_pct=0.10)
    eng = make_engine(9600.0, 9200.0, big)                                                                                    # D = 400, centre 9400, stop 9200: 200 away
    eng.session_open, eng.symbol = time(9, 0), "CRUDEOILM"
    for ts, px in [(at(9, 0), 9550.0), (at(9, 5), 9500.0), (at(9, 10), 9400.0), (at(9, 20), 9200.0)]:
        eng.on_tick(ts, px)
    [t] = eng.closed
    assert t["qty"] % 10 == 0 and t["qty"] == 40 and -10000 <= t["net_pnl"] < -8500                                           # 40 barrels, the stop-out loses close to Rs 10,000
    eng2 = make_engine(9600.0, 9200.0, big)
    eng2.session_open, eng2.symbol = time(9, 0), "CRUDEOIL"                                                                   # one lot is 100 barrels: about Rs 20,000 at this stop
    ev = []
    for ts, px in [(at(9, 0), 9550.0), (at(9, 5), 9500.0), (at(9, 10), 9400.0)]:
        ev += eng2.on_tick(ts, px)
    assert eng2.position is None and any("even one lot (100 units)" in n for n in notes(ev))
    e3, _ = play([(at(9, 0), 104.0), (at(9, 5), 102.5)], max_loss_rs=None, params=Params(strategy="prev_day_zone30_buy_center", qty=7, slippage_pct=0, cost_pct=0))
    assert e3.position.qty == 7                                                                                               # no maximum loss: the run's fixed quantity


def test_spec_levels_status_preview_and_no_form():
    e = make_engine(H, L, P())
    assert [(x["name"], round(x["value"], 2)) for x in e.levels()] == [("prev High", 105.0), ("top zone from", 103.5), ("buy at centre", 102.5), ("prev Low (stop)", 100.0)]
    assert "Waiting" in e.status_text(None)
    p = SPEC.preview(105, 100, {})
    assert "centre = <b>102.50</b>" in p["summary"] and "102.50" in p["cards"][0]["rows"][1][1] and "100.00" in p["cards"][0]["rows"][2][1] and "106.00" in p["cards"][0]["rows"][3][1]
    assert "at most Rs 10,000" in " ".join(p["notes"]) and "only as a BUY" in " ".join(p["notes"])
    assert "buy at the centre 102.50, stop 100.00, target 106.00" in SPEC.describe(105, 100, {})
    assert SPEC.fields == [] and SPEC.label == "30% opening buy at center" and SPEC.blurb and not SPEC.hidden
    assert RULES["zone_pct"] == 30.0 and RULES["target_x_stop"] == 1.4 and RULES["max_loss_rs"] == 10000 and RULES["last_entry"] == time(18, 0)


def test_replay_fills_at_the_centre_and_exits_at_the_target():
    day = lambda d, rows: [{"ts": datetime(2026, 10, d, 9, 15) + timedelta(minutes=5 * k), "open": a, "high": b, "low": c, "close": e} for k, (a, b, c, e) in enumerate(rows)]      # noqa: E731
    candles = day(12, [(102, 105, 100, 103)]) + day(13, [(104.0, 104.3, 103.9, 104.2), (104.2, 104.3, 102.4, 102.6), (102.6, 106.3, 102.5, 106.2)])
    r = replay_range(candles, P(), "NSE_EQ")
    [t] = r["per_day"][datetime(2026, 10, 13).date()]["trade_list"]
    assert t["entry_price"] == pytest.approx(102.5) and t["entry_ts"].time() >= time(9, 20) and t["stop"] == 100.0
    assert t["exit_reason"] == "target" and t["exit_price"] == pytest.approx(106.0)
    assert [x["name"] for x in r["per_day"][datetime(2026, 10, 13).date()]["levels"]][2] == "buy at centre"


def test_the_strategy_is_listed_and_runs_through_the_api(tmp_path):
    st = Settings(database_url=f"sqlite:///{tmp_path}/c.db", feed_mode="sim")
    app = create_app(st, feed_factory=lambda t, n, s: ReplayFeed(t, [], D0, on_note=n, on_status=s), load_master=False)
    with TestClient(app) as c:
        listed = {s["name"]: s for s in c.get("/api/strategies").json()["strategies"]}
        assert listed["prev_day_zone30_buy_center"]["label"] == "30% opening buy at center" and listed["prev_day_zone30_buy_center"]["fields"] == []
        p = c.post("/api/strategies/prev_day_zone30_buy_center/preview", json={"high": 105, "low": 100, "options": {}}).json()
        assert "102.50" in p["summary"]
        r = c.post("/api/runs", json={"query": "BAJFINANCE", "prev_high": 105, "prev_low": 100, "params": {"strategy": "prev_day_zone30_buy_center"}})
        assert r.status_code == 200, r.text
        run = c.get("/api/state").json()["runs"][0]
        assert run["strategy"] == "prev_day_zone30_buy_center" and [x["name"] for x in run["levels"]][2] == "buy at centre"
