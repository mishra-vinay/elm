"""Quantity from a maximum loss: nobody enters a quantity, the strategy works it out for every trade."""
from datetime import datetime, time

import pytest

from dryrun.sizing import LOT_SIZES, lot_size, loss_per_unit, units_for_max_loss
from dryrun.strategies.prev_day_zone30 import RULES as ZONE_RULES
from dryrun.strategies.range_thirds import RULES as THIRDS_RULES
from dryrun.strategy import Params, make_engine

pytestmark = pytest.mark.usefixtures("only_shipped")
D0 = datetime(2026, 10, 13)


def at(h, m):
    return D0.replace(hour=h, minute=m)


def test_units_are_whole_lots_that_keep_a_stop_out_within_the_maximum_loss():
    per = loss_per_unit(9500, 9424, 0.02, 0.10)                         # 76 + 9424 x 0.02% + 9500 x 0.10% = 87.38
    assert per == pytest.approx(76 + 9424 * 0.0002 + 9500 * 0.001)
    q = units_for_max_loss(10000, 9500, 9424, 0.02, 0.10, lot=10)
    assert q == 110 and q % 10 == 0 and q * per <= 10000 < (q + 10) * per
    assert units_for_max_loss(10000, 9500, 9424, 0.02, 0.10, lot=1) == 114
    assert units_for_max_loss(10000, 9500, 7500, 0.02, 0.10, lot=10) == 0          # even one lot of 10 would lose more than 10,000 (about 2,000 a unit)
    assert units_for_max_loss(10000, 100, 100, 0, 0) == 0                            # no stop distance and no costs: nothing to size from


def test_lot_sizes_for_the_instruments_in_use_and_a_single_unit_for_the_rest():
    assert lot_size("CRUDEOILM") == 10 and lot_size("crudeoil") == 100 and lot_size("NATGASMINI") == 250 and lot_size("GOLDPETAL") == 1
    assert lot_size("BAJFINANCE") == 1 and lot_size(None) == 1 and "SILVERMIC" in LOT_SIZES


def _zone_trade(symbol, high=9600.0, low=9200.0, entry_tick=9550.0, stop_tick=None):
    e = make_engine(high, low, Params(strategy="prev_day_zone30", slippage_pct=0.02, cost_pct=0.10))
    e.session_open, e.symbol = time(9, 0), symbol
    ev = []
    for ts, px in [(at(9, 0), 9550.0), (at(9, 5), entry_tick)] + ([(at(9, 20), stop_tick)] if stop_tick else []):
        ev += e.on_tick(ts, px)
    return e, ev


def test_zone_strategy_sizes_each_trade_from_the_ten_thousand_rupee_maximum_loss():
    assert ZONE_RULES["max_loss_rs"] == 10000
    e, _ = _zone_trade("CRUDEOILM", stop_tick=9399.0)                   # BUY at about 9552, stop = the centre 9400 of yesterday's range (about 152 below); the tick falls to the stop
    [t] = e.closed
    assert t["exit_reason"] == "stop" and t["qty"] == 60 and t["qty"] % 10 == 0           # whole lots of 10 barrels
    assert -10000 <= t["net_pnl"] < -8000                                                  # the stop-out loses close to, and never more than, Rs 10,000 (a gap far through the stop could lose more)
    e2, _ = _zone_trade("CRUDEOIL", stop_tick=9399.0)                   # the big contract: one lot is 100 barrels, about Rs 16,000 at this stop: the day is skipped
    assert e2.position is None and e2.closed == []
    e3, ev = _zone_trade("CRUDEOIL")
    assert any("even one lot (100 units) would lose more than Rs 10,000" in (x.reason or "") for x in ev if x.kind == "note")


def test_range_thirds_sizes_each_trade_the_same_way():
    assert THIRDS_RULES["max_loss_rs"] == 10000
    e = make_engine(9600.0, 9000.0, Params(strategy="range_thirds", slippage_pct=0.02, cost_pct=0.10))
    e.session_open, e.symbol = time(9, 0), "CRUDEOILM"
    for ts, px in [(at(9, 0), 9450.0), (at(9, 5), 9450.0), (at(9, 6), 9480.0)]:       # D = 600, D/3 = 200: BUY entry 9476.8 (D/3 - 0.8% in from the High), stop 9400
        e.on_tick(ts, px)
    pos = e.position
    assert pos is not None and pos.side == "BUY" and pos.qty % 10 == 0 and pos.qty >= 10
    per = loss_per_unit(pos.entry_price, pos.stop, 0.02, 0.10)
    assert pos.qty * per <= 10000 < (pos.qty + 10) * per


def test_a_strategy_without_a_maximum_loss_uses_the_runs_fixed_quantity():
    old = dict(THIRDS_RULES)
    THIRDS_RULES["max_loss_rs"] = None
    try:
        e = make_engine(9600.0, 9000.0, Params(strategy="range_thirds", qty=5, slippage_pct=0, cost_pct=0))
        e.session_open = time(9, 0)
        for ts, px in [(at(9, 0), 9450.0), (at(9, 5), 9450.0), (at(9, 6), 9480.0)]:
            e.on_tick(ts, px)
        assert e.position is not None and e.position.qty == 5
    finally:
        THIRDS_RULES.clear()
        THIRDS_RULES.update(old)
