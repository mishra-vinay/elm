"""Range thirds as installed: the 0.8 rule with its numbers in RULES (no settings form). Geometry, entry window, one trade a day, day-open filter, replay."""
from contextlib import contextmanager
from datetime import datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient

from dryrun.app import create_app
from dryrun.config import Settings
from dryrun.replay import replay_range
from dryrun.sim_feed import ReplayFeed
from dryrun.strategies import range_thirds as rt
from dryrun.strategies.range_thirds import RULES, SPEC, RangeThirdsEngine, geometry
from dryrun.strategy import Params, make_engine

H, L = 105.0, 100.0          # D = 5; third 1.6667; the 0.8 = 0.84 (0.8% of 105) for a BUY and 0.80 (0.8% of 100) for a SELL
D0 = datetime(2026, 10, 13)
pytestmark = pytest.mark.usefixtures("only_shipped")


def P():
    return Params(strategy="range_thirds", slippage_pct=0, cost_pct=0)


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


def play(ticks, session_open=time(9, 0), day_open=None, **r):
    with rules(**r):
        e = make_engine(H, L, P())
        assert isinstance(e, RangeThirdsEngine)
        e.session_open, e.day_open = session_open, day_open
        ev = []
        for ts, px in ticks:
            ev += e.on_tick(ts, px)
    return e, ev


def test_geometry_follows_the_written_rule():
    g = geometry(H, L, RULES)
    assert g.third == pytest.approx(5 / 3) and g.dev == {"BUY": pytest.approx(0.84), "SELL": pytest.approx(0.80)}
    assert g.entry["BUY"] == pytest.approx(105 - (5 / 3 - 0.84)) and g.entry["SELL"] == pytest.approx(100 + (5 / 3 - 0.80))
    assert g.entry["BUY"] - g.stop["BUY"] == pytest.approx(0.84) and g.stop["SELL"] - g.entry["SELL"] == pytest.approx(0.80)        # the stop is exactly the 0.8 from the entry
    assert g.target["BUY"] == pytest.approx(105 + 5 / 3) and g.target["SELL"] == pytest.approx(100 - 5 / 3)                      # = previous High + third / previous Low - third
    assert g.valid == {"BUY": True, "SELL": True}
    wide = geometry(105, 95, RULES)
    assert wide.target["BUY"] == pytest.approx(105 + 10 / 3) and wide.entry["BUY"] - wide.stop["BUY"] == pytest.approx(0.84)
    tiny = geometry(100.5, 100.0, RULES)                                                                                          # D/3 + (D/3 - 0.8) is negative: skipped
    assert tiny.valid == {"BUY": False, "SELL": False}


def test_buy_trade_with_fixed_stop_and_target():
    e, ev = play([(at(9, 0), 102.0), (at(9, 5), 103.5), (at(9, 6), 104.2), (at(9, 7), 105.0), (at(9, 8), 106.0), (at(9, 9), 106.7)])
    [t] = e.closed
    assert t["side"] == "BUY" and t["entry_price"] == pytest.approx(104.2) and t["stop"] == pytest.approx(105 - 5 / 3) and t["target"] == pytest.approx(105 + 5 / 3)
    assert t["exit_reason"] == "target" and t["exit_price"] == pytest.approx(105 + 5 / 3)


def test_sell_trade_and_a_stop_loss():
    e, _ = play([(at(9, 0), 103.0), (at(9, 5), 102.0), (at(9, 6), 100.8), (at(9, 7), 100.0), (at(9, 8), 98.2)])
    [t] = e.closed
    assert t["side"] == "SELL" and t["stop"] == pytest.approx(100 + 5 / 3) and t["target"] == pytest.approx(100 - 5 / 3) and t["exit_reason"] == "target"
    e, _ = play([(at(9, 0), 102.0), (at(9, 5), 103.5), (at(9, 6), 104.2), (at(9, 7), 103.3)])            # the stop 103.33 is 0.84 below the 104.17 entry price
    [t] = e.closed
    assert t["exit_reason"] == "stop" and t["exit_price"] == pytest.approx(103.3) and t["net_pnl"] < 0


def test_one_trade_a_day_and_the_other_side_is_cancelled():
    prices = [(at(9, 0), 102.0), (at(9, 5), 103.5), (at(9, 6), 104.2), (at(9, 7), 103.3), (at(9, 8), 102.0), (at(9, 9), 100.8), (at(9, 10), 100.0), (at(9, 11), 99.0), (at(9, 12), 98.2)]
    e, _ = play(prices)
    assert len(e.closed) == 1 and e.closed[0]["side"] == "BUY" and e.position is None               # stopped out; the SELL at 100.8 was not allowed
    assert e.armed == {"BUY": False, "SELL": False} and "Today's trade has been entered" in e.status_text(None)
    two, _ = play(prices, trades_per_day=2)                                                          # the number is in RULES: with 2 the SELL goes ahead
    assert [t["side"] for t in two.closed] == ["BUY", "SELL"]


def test_no_entry_in_the_first_candle_and_none_from_the_cutoff():
    e, _ = play([(at(9, 0), 102.0), (at(9, 2), 104.3), (at(9, 5), 103.0), (at(9, 6), 104.2)])        # 104.3 in the first 5 minutes would have hit the entry; nothing is armed yet
    assert e.position is not None and e.position.entry_ts == at(9, 6) and e.position.entry_price == pytest.approx(104.2)
    e, _ = play([(at(9, 0), 102.0), (at(9, 2), 104.3), (at(9, 3), 102.5), (at(9, 4), 104.4)])
    assert e.position is None and e.closed == []                                                      # everything happened inside the first candle
    e, _ = play([(at(17, 0), 102.0), (at(17, 58), 103.0), (at(18, 1), 104.3)], session_open=time(17, 0))              # armed at 17:58, cancelled at 18:00
    assert e.position is None and e.armed == {"BUY": False, "SELL": False}
    e, _ = play([(at(17, 0), 102.0), (at(17, 58), 103.0), (at(17, 59), 104.3)], session_open=time(17, 0))              # the same move a minute earlier trades
    assert e.position is not None
    e, _ = play([(at(21, 0), 102.0), (at(21, 30), 103.0), (at(21, 31), 104.3)], session_open=time(21, 0), last_entry=None)   # no cut-off configured
    assert e.position is not None


def test_day_open_filter_no_trade_unless_the_day_opens_inside_yesterdays_range():
    sell_path = [(at(9, 0), 98.0), (at(9, 5), 99.0), (at(9, 6), 101.0), (at(9, 7), 102.0), (at(9, 8), 100.8), (at(9, 9), 100.0), (at(9, 10), 98.2)]   # opens below the Low, rallies, falls back
    e, ev = play(sell_path)
    assert e.closed == [] and e.position is None and e.blocked("SELL") and e.blocked("BUY")                          # both sides are off
    assert [x.reason for x in ev if x.kind == "note"] == ["No trade today: the day opened at 98.00, below the previous Low 100.00, outside yesterday's range"]
    assert "No trade today" in e.status_text(None)
    e, _ = play(sell_path, day_open_filter=False)                                                                   # the number in RULES switches the filter off
    assert e.closed[0]["side"] == "SELL"
    buy_path = [(at(9, 0), 107.0), (at(9, 5), 104.0), (at(9, 6), 104.3), (at(9, 7), 106.0), (at(9, 8), 106.8)]      # opens above the High, falls into the range
    e, ev = play(buy_path)
    assert e.closed == [] and e.position is None and e.blocked("BUY") and e.blocked("SELL")
    assert "above the previous High 105.00" in [x.reason for x in ev if x.kind == "note"][0]
    # the same prices after an open inside the range do trade; the edges of the range count as inside
    e, _ = play([(at(9, 0), 104.0)] + buy_path[1:])
    assert e.closed and e.closed[0]["side"] == "BUY"
    for edge in (100.0, 105.0):
        e, _ = play([(at(9, 0), edge), (at(9, 5), 102.0)])
        assert not e.blocked("BUY") and not e.blocked("SELL")
    # a SELL on a day that opens ABOVE the High was possible before (price falls through the SELL entry): now it is not
    e, _ = play([(at(9, 0), 106.0), (at(9, 5), 103.0), (at(9, 6), 101.0), (at(9, 7), 100.8), (at(9, 8), 98.0)])
    assert e.closed == [] and e.position is None
    e, _ = play([(at(9, 6), 102.0), (at(9, 7), 103.5), (at(9, 8), 104.2), (at(9, 9), 106.7)], day_open=104.0)         # the runner supplies the real open for a run started later
    assert e.closed and e.day_open == 104.0


def test_a_side_is_skipped_when_the_range_is_too_small():
    with rules():
        e = make_engine(100.5, 100.0, P())
        e.session_open = time(9, 0)
        for i, px in enumerate([100.2, 100.1, 100.4, 100.45]):
            e.on_tick(at(9, 5 + i), px)
    assert e.position is None and e.closed == [] and not e.valid["BUY"]
    assert "too small" in " ".join(SPEC.preview(100.5, 100, {})["notes"]) or "skipped" in str(SPEC.preview(100.5, 100, {})["cards"])


def test_preview_levels_and_the_spec_have_no_settings_form():
    p = SPEC.preview(105, 100, {})
    assert "103.33" in p["cards"][0]["rows"][2][1] and "106.67" in p["cards"][0]["rows"][3][1] and "104.17" in p["cards"][0]["rows"][1][1]      # BUY card: entry, stop, target
    assert "100.87" in p["cards"][1]["rows"][1][1] and "101.67" in p["cards"][1]["rows"][2][1] and "98.33" in p["cards"][1]["rows"][3][1]
    assert "fixed in the strategy file" in " ".join(p["notes"]) and "18:00" in " ".join(p["notes"])
    assert "BUY entry 104.17, stop 103.33, target 106.67" in SPEC.describe(105, 100, {})
    assert SPEC.fields == [] and SPEC.blurb and "yesterday" in SPEC.blurb and not SPEC.hidden
    e = make_engine(H, L, P())
    assert [x["name"] for x in e.levels()] == ["prev High", "buy trigger", "sell trigger", "prev Low", "buy stop", "sell stop", "buy target", "sell target"]
    assert rt.RULES["dev_pct"] == 0.8 and rt.RULES["trades_per_day"] == 1 and rt.RULES["last_entry"] == time(18, 0) and rt.RULES["skip_minutes"] == 5


def test_replay_fills_at_the_entry_price_in_the_second_candle():
    day = lambda d, rows: [{"ts": datetime(2026, 10, d, 9, 15) + timedelta(minutes=5 * k), "open": a, "high": b, "low": c, "close": e} for k, (a, b, c, e) in enumerate(rows)]      # noqa: E731
    candles = day(12, [(102, 105, 100, 103)]) + day(13, [(102.0, 102.5, 101.9, 102.3), (102.4, 104.5, 102.3, 104.4)])
    r = replay_range(candles, P(), "NSE_EQ")
    [t] = r["per_day"][datetime(2026, 10, 13).date()]["trade_list"]
    assert t["side"] == "BUY" and t["entry_price"] == pytest.approx(105 - (5 / 3 - 0.84)) and t["entry_ts"].time() >= time(9, 20)
    assert t["stop"] == pytest.approx(105 - 5 / 3) and t["target"] == pytest.approx(105 + 5 / 3)


def test_api_runs_it_without_any_options(tmp_path):
    st = Settings(database_url=f"sqlite:///{tmp_path}/r.db", feed_mode="sim")
    prices = [102, 101.5, 101.0, 100.8, 100.3, 99.8, 99.3, 98.2]
    app = create_app(st, feed_factory=lambda t, n, s: ReplayFeed(t, prices, D0.replace(hour=10), on_note=n, on_status=s), load_master=False)
    with TestClient(app) as c:
        s = next(x for x in c.get("/api/strategies").json()["strategies"] if x["name"] == "range_thirds")
        assert s["fields"] == [] and s["blurb"]
        p = c.post("/api/strategies/range_thirds/preview", json={"high": 105, "low": 100, "options": {}}).json()
        assert "104.17" in p["cards"][0]["rows"][1][1]
        r = c.post("/api/runs", json={"query": "BAJFINANCE", "prev_high": 105, "prev_low": 100, "params": {"strategy": "range_thirds", "slippage_pct": 0, "cost_pct": 0}})
        assert r.status_code == 200, r.text
        run = c.get("/api/state").json()["runs"][0]
        lv = {x["name"]: x["value"] for x in run["levels"]}
        assert run["strategy"] == "range_thirds" and lv["sell trigger"] == pytest.approx(100 + (5 / 3 - 0.80)) and lv["buy target"] == pytest.approx(105 + 5 / 3)
