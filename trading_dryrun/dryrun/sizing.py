"""Position sizing from a maximum loss. A strategy is given the most it may lose on one trade (in rupees) and works out the quantity itself:

    units = floor(max loss / (loss per unit if the stop is hit) / lot size) * lot size

The loss per unit is the stop distance plus the exit slippage and the round-trip cost, so a stop-out costs at most the maximum loss. If even ONE lot would lose
more than that, the trade is not taken. Quantity is in the instrument's trading units (barrels for crude oil, mmBtu for natural gas, kg for silver, grams for
gold petal), the same units the price is quoted in, so rupees = price points x units.
"""
from __future__ import annotations

import math

# MCX contract sizes for the instruments in use (units per lot). Other instruments are sized in single units. Check against your broker's contract specification.
LOT_SIZES = {"CRUDEOILM": 10, "CRUDEOIL": 100, "NATGASMINI": 250, "NATURALGAS": 1250, "SILVERMIC": 1, "SILVERM": 5, "GOLDPETAL": 1}


def lot_size(symbol: str | None) -> int:
    return LOT_SIZES.get((symbol or "").upper(), 1)


def loss_per_unit(entry: float, stop: float, slippage_pct: float, cost_pct: float) -> float:
    """Rupees lost per unit if the stop is hit: the stop distance, slippage on the stop exit, and the round-trip cost."""
    return abs(entry - stop) + stop * slippage_pct / 100 + entry * cost_pct / 100


def units_for_max_loss(max_loss: float, entry: float, stop: float, slippage_pct: float, cost_pct: float, lot: int = 1) -> int:
    """Whole lots' worth of units whose stop-out loss stays within max_loss; 0 when even one lot is too much."""
    per_unit = loss_per_unit(entry, stop, slippage_pct, cost_pct)
    if per_unit <= 0:
        return 0
    return int(math.floor(max_loss / per_unit / lot + 1e-9)) * lot
