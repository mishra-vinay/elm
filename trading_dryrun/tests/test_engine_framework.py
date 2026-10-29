from datetime import datetime, timedelta

import pytest

from demo_strategy import P, DemoEngine

T0 = datetime(2026, 10, 9, 10, 0, 0)


def run(prices, high=100.0, low=95.0, **kw):
    kw.setdefault("slippage_pct", 0.0)
    kw.setdefault("cost_pct", 0.0)
    eng = DemoEngine(high, low, P(**kw))
    evs = []
    for i, p in enumerate(prices):
        evs += eng.on_tick(T0 + timedelta(seconds=i), p)
    return eng, evs


def kinds(evs):
    return [(e.kind, e.side) for e in evs]


def test_buy_after_break_and_pullback_then_target():
    eng, evs = run([99.5, 100.4, 101.0, 99.0, 100.0, 101.2])
    assert kinds(evs) == [("break", "BUY"), ("entry_signal", "BUY"), ("entry", "BUY"), ("exit", "BUY")]
    entry = [e for e in evs if e.kind == "entry"][0]
    assert entry.price == 99.0 and entry.trade["target"] == pytest.approx(99.0 * 1.02)
    ex = evs[-1]
    assert ex.reason == "target" and ex.price == pytest.approx(99.0 * 1.02)       # limit fill at the target
    assert eng.summary()["wins"] == 1


def test_sell_after_break_below_low_and_pullback():
    eng, evs = run([94.5, 94.0, 95.5, 96.0, 95.0, 93.0], pullback_pct=1.0, target_pct=2.0)
    # break below 95 at 94.0, pullback to 95*1.01 = 95.95 triggers at 96.0
    assert kinds(evs)[:3] == [("break", "SELL"), ("entry_signal", "SELL"), ("entry", "SELL")]
    assert eng.position is None and eng.closed[0]["exit_reason"] == "target"
    assert eng.closed[0]["entry_price"] == 96.0 and eng.closed[0]["exit_price"] == pytest.approx(96.0 * 0.98)


def test_no_signal_without_break():
    _, evs = run([97, 98, 99.9, 99.0, 98.0, 96.0])
    assert evs == []


def test_pullback_needed_after_break_not_before():
    # price falls to 99 BEFORE any break: no signal; only after a break does the pullback count
    _, evs = run([99.0, 98.0, 99.5])
    assert evs == []
    _, evs = run([100.5, 101, 100.5, 100.2])           # broke but never pulled back to 99.0
    assert kinds(evs) == [("break", "BUY")]


def test_gap_open_above_high_arms_immediately():
    _, evs = run([101.0, 99.0])
    assert kinds(evs) == [("break", "BUY"), ("entry_signal", "BUY"), ("entry", "BUY")]


def test_stop_loss_market_exit_with_slippage():
    eng, evs = run([100.5, 99.0, 98.0], stop_pct=1.0, slippage_pct=0.1)
    ex = [e for e in evs if e.kind == "exit"][0]
    assert ex.reason == "stop"
    assert ex.price == pytest.approx(98.0 * (1 - 0.001))      # market order: tick price less slippage
    assert eng.closed[0]["net_pnl"] < 0


def test_no_stop_when_stop_is_none():
    eng, evs = run([100.5, 99.0, 90.0], stop_pct=None)
    assert eng.position is not None and not [e for e in evs if e.kind == "exit"]


def test_square_off_closes_open_position_at_last_price():
    eng, _ = run([100.5, 99.0, 99.4])
    evs = eng.square_off(T0 + timedelta(hours=5))
    assert evs[0].reason == "squareoff" and evs[0].price == pytest.approx(99.4)
    assert eng.on_tick(T0 + timedelta(hours=6), 120) == []        # nothing after the session ends


def test_one_position_at_a_time_second_signal_logged_as_skipped():
    # BUY opens at 99.0; later price falls below the Low and pulls back -> SELL signal while BUY is still open
    eng, evs = run([100.5, 99.0, 98.0, 94.0, 96.0], stop_pct=None, allow_both_sides=False)
    skipped = [e for e in evs if e.kind == "entry_skipped"]
    assert skipped and "already open" in skipped[0].reason and skipped[0].side == "SELL"


def test_both_sides_allowed_when_configured():
    eng, evs = run([100.5, 99.0, 98.0, 94.0, 96.0], stop_pct=None, allow_both_sides=True, max_trades_per_day=5)
    assert [e.side for e in evs if e.kind == "entry"] == ["BUY", "SELL"]


def test_max_trades_per_day_blocks_entries():
    eng, evs = run([100.5, 99.0, 101.5, 100.5, 99.0], max_trades_per_side=2, max_trades_per_day=1)
    assert len([e for e in evs if e.kind == "entry"]) == 1
    assert [e.reason for e in evs if e.kind == "entry_skipped"] == ["max trades per day reached"]


def test_reentry_needs_fresh_break_and_respects_per_side_cap():
    eng, evs = run([100.5, 99.0, 101.5, 99.5, 100.5, 99.0, 101.5], max_trades_per_side=2, max_trades_per_day=5)
    assert len([e for e in evs if e.kind == "entry"]) == 2
    # a third break would exceed max_trades_per_side=2
    eng.on_tick(T0 + timedelta(minutes=1), 100.8)
    assert not eng.armed["BUY"]


def test_daily_loss_limit_blocks_after_losses():
    eng, evs = run([100.5, 99.0, 98.0, 100.5, 99.0], stop_pct=1.0, max_trades_per_side=3, max_trades_per_day=5,
                   daily_loss_limit_pct=0.5)
    assert any(e.reason == "daily loss limit reached" for e in evs if e.kind == "entry_skipped")


def test_risk_callback_can_veto():
    eng = DemoEngine(100, 95, P(slippage_pct=0, cost_pct=0), approve=lambda *a: (False, "risk: test"))
    evs = []
    for i, p in enumerate([100.5, 99.0]):
        evs += eng.on_tick(T0 + timedelta(seconds=i), p)
    assert kinds(evs)[-1] == ("entry_skipped", "BUY") and evs[-1].reason == "risk: test"
    assert eng.position is None


def test_costs_and_quantity_in_pnl():
    eng, _ = run([100.5, 99.0, 101.5], qty=10, cost_pct=0.1)
    t = eng.closed[0]
    assert t["gross_pnl"] == pytest.approx((99.0 * 1.02 - 99.0) * 10)
    assert t["cost"] == pytest.approx(99.0 * 10 * 0.001)
    assert t["net_pnl"] == pytest.approx(t["gross_pnl"] - t["cost"])


def test_invalid_inputs():
    with pytest.raises(ValueError):
        DemoEngine(95, 100, P())
    with pytest.raises(ValueError):
        P(target_pct=0).validate()
    with pytest.raises(ValueError):
        P(stop_pct=-1).validate()


def test_zero_pullback_means_retest_of_the_level():
    _, evs = run([100.5, 100.0], pullback_pct=0.0)
    assert kinds(evs) == [("break", "BUY"), ("entry_signal", "BUY"), ("entry", "BUY")]
