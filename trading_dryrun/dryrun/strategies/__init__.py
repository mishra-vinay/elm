"""Installed strategies.

To add one:
  1. Create `dryrun/strategies/my_strategy.py` with a `StrategyEngine` subclass (see `dryrun/strategy.py` for the hooks) and a
     `register(StrategySpec(...))` call that names it and describes its settings form (`range_thirds.py` is a complete, simple example: its numbers in a RULES dict at the top, its logic in the engine, no settings form).
  2. Import it here.
  3. Restart the service. The strategy then appears in the Dry runs and Backtest pages, with its settings form generated from `fields`.
"""
from . import range_thirds, prev_day_zone30, prev_day_zone30_buy_center, market_shift, market_shift_sell, range_thirds_research         # the first one imported is the default in the dropdowns

__all__ = ["range_thirds", "prev_day_zone30", "prev_day_zone30_buy_center", "market_shift", "market_shift_sell", "range_thirds_research"]          # importing a strategy module registers it
