import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import dryrun.strategies                        # noqa: E402  (registers the shipped strategies)
from dryrun import strategy as _framework      # noqa: E402

SHIPPED = dict(_framework.STRATEGIES)          # what the app installs by itself
assert "range_thirds" in SHIPPED and dryrun.strategies.__all__

import demo_strategy                            # noqa: E402  (registers the test-only "demo" strategy)

assert demo_strategy.DEMO.name == "demo" and "demo" not in SHIPPED


@pytest.fixture(autouse=True)
def _only_the_demo_strategy():
    """The framework tests run with the single test-only strategy installed (so a request that names none gets it).
    Tests of a shipped strategy ask for `only_shipped` or `shipped_and_demo`."""
    for name in SHIPPED:
        _framework.STRATEGIES.pop(name, None)
    yield
    _framework.STRATEGIES.update(SHIPPED)
    _framework.STRATEGIES["demo"] = demo_strategy.DEMO


@pytest.fixture
def only_shipped():
    _framework.STRATEGIES.clear()
    _framework.STRATEGIES.update(SHIPPED)
    yield


@pytest.fixture
def no_strategies():
    """A bare framework: nothing installed."""
    _framework.STRATEGIES.clear()
    yield
