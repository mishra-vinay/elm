"""Range thirds RESEARCH version (all options): geometry on the worked example (Low 100, High 105), both entry modes, both stop placements, validation,
the engine against the fast simulator, the preview, and a full dry run through the API."""
import random
import time
from collections import defaultdict
from datetime import datetime, time as dtime, timedelta

import pytest
from fastapi.testclient import TestClient

from dryrun.app import create_app
from dryrun.config import Settings
from dryrun.replay import candle_path, replay_range
from dryrun.sim_feed import ReplayFeed
from dryrun.strategies.range_thirds_research import CLASSIC, SPEC, RangeThirdsEngine
from dryrun.strategies.range_thirds_backtest import sim_day
from dryrun.strategy import Params, make_engine

T0 = datetime(2026, 10, 13, 10, 0)
H, L = 105.0, 100.0          # D = 5: trigger D/6 = 0.833, stop and target distance D/3 = 1.667

pytestmark = pytest.mark.usefixtures("only_shipped")


def P(**o):
    return Params(strategy="range_thirds_research", options={**CLASSIC, **o}, slippage_pct=0, cost_pct=0)


def run(prices, **o):
    o.setdefault("stop_side", "inside")        # most scenarios use the protective stop; the as-written stop has its own tests
    e = make_engine(H, L, P(**o))
    ev = []
    for i, x in enumerate(prices):
        ev += e.on_tick(T0 + timedelta(seconds=i), x)
    return e, ev


def test_factory_and_levels():
    e = make_engine(H, L, P(stop_side="inside"))
    assert isinstance(e, RangeThirdsEngine)
    assert e.sell_entry_level == pytest.approx(100 + 5 / 6) and e.buy_entry_level == pytest.approx(105 - 5 / 6)
    assert e.sell_stop_level == pytest.approx(100 + 5 / 3) and e.buy_stop_level == pytest.approx(105 - 5 / 3)
    d = make_engine(H, L, P())                                                     # default = the stop on the losing side
    assert d.sell_stop_level == pytest.approx(100 + 5 / 3) and d.buy_stop_level == pytest.approx(105 - 5 / 3)
    w = make_engine(H, L, P(stop_side="outside"))                                  # the stop beyond the level
    assert w.sell_stop_level == pytest.approx(100 - 5 / 3) and w.buy_stop_level == pytest.approx(105 + 5 / 3)
    assert [x["name"] for x in w.levels()] == ["prev High", "buy trigger", "sell trigger", "prev Low", "buy stop", "sell stop"]


def test_sell_approach_to_target():
    e, ev = run([102, 101.0, 100.8, 100.0, 99.1])
    [t] = e.closed
    assert t["side"] == "SELL" and t["exit_reason"] == "target"
    assert t["entry_price"] == pytest.approx(100.8)
    assert t["target"] == pytest.approx(100.8 - 5 / 3) and t["stop"] == pytest.approx(100 + 5 / 3)
    assert t["net_pnl"] == pytest.approx(5 / 3)


def test_sell_approach_to_stop_is_above_the_entry():
    e, _ = run([102, 100.8, 101.0, 101.7])
    [t] = e.closed
    assert t["exit_reason"] == "stop" and t["exit_price"] == pytest.approx(101.7) and t["net_pnl"] < 0 and t["stop"] > t["entry_price"]


def test_buy_approach_to_target_and_stop():
    e, _ = run([103, 104.17, 105.0, 105.9])
    [t] = e.closed
    assert t["side"] == "BUY" and t["exit_reason"] == "target" and t["target"] == pytest.approx(104.17 + 5 / 3)
    e2, _ = run([103, 104.17, 103.3])
    [t2] = e2.closed
    assert t2["exit_reason"] == "stop" and t2["stop"] == pytest.approx(105 - 5 / 3) and t2["stop"] < t2["entry_price"]


def test_approach_needs_price_to_start_on_the_far_side():
    e, ev = run([100.5, 100.6, 100.7])             # SELL trigger 100.83: the day opened already inside the zone
    assert not [x for x in ev if x.side == "SELL"] and e.closed == [] and e.position is None


def test_after_break_mode_waits_for_the_level_to_break():
    e, ev = run([102, 100.9, 100.0], entry_mode="after_break")
    assert e.position is None and not [x for x in ev if x.kind == "entry"]
    e, ev = run([99.5, 99.0, 100.5, 100.9, 100.0, 99.2], entry_mode="after_break")
    [t] = e.closed
    assert t["side"] == "SELL" and t["entry_price"] == pytest.approx(100.9) and t["exit_reason"] == "target"
    assert any(x.kind == "break" and x.side == "SELL" for x in ev)


def test_stop_beyond_the_level_sits_beyond_the_target_and_never_limits_a_loss():
    e, _ = run([102, 100.8, 101.5, 103.0, 104.5, 104.9], stop_side="outside")      # SELL entered at 100.8, price rises against it
    assert e.position is not None and e.closed == [] and e.position.stop == pytest.approx(100 - 5 / 3)
    e.square_off(T0 + timedelta(minutes=5), reason="squareoff")
    assert e.closed[0]["exit_reason"] == "squareoff" and e.closed[0]["net_pnl"] < -3        # only the square-off limits the loss
    e2, _ = run([102, 100.8, 100.0, 99.1], stop_side="outside")
    assert e2.closed[0]["exit_reason"] == "target"
    e3, _ = run([103, 104.17, 105.9], stop_side="outside")
    assert e3.closed[0]["exit_reason"] == "target" and e3.closed[0]["stop"] == pytest.approx(105 + 5 / 3)


def test_outside_stop_does_fire_when_it_is_nearer_than_the_target():
    e, _ = run([102, 100.8, 99.0, 98.2], stop_side="outside", target_frac=1.0)
    [t] = e.closed
    assert t["exit_reason"] == "stop" and t["exit_price"] == pytest.approx(98.2) and t["net_pnl"] > 2.5


def test_defaults_fill_in_missing_options_and_validation():
    P().validate()
    P(stop_frac=0.1, entry_frac=0.2, stop_side="outside").validate()          # fine with the stop beyond the level
    for bad in (dict(stop_frac=0.1, entry_frac=0.2), dict(entry_mode="x"), dict(target_frac=0), dict(entry_frac=-0.1),
                dict(stop_side="sideways"), dict(stop_frac=0), dict(gap_filter="maybe")):
        with pytest.raises(ValueError):
            P(**bad).validate()
    assert make_engine(H, L, Params(strategy="range_thirds_research")).o["entry_frac"] == pytest.approx(1 / 6)       # options left empty


def test_preview_on_the_example():
    p = SPEC.preview(105, 100, {**CLASSIC, "stop_side": "inside"})
    assert "reward : risk = <b>2.00 : 1</b>" in p["summary"]
    sell, buy = p["cards"]
    assert "100.83" in sell["rows"][1][1] and "101.67" in sell["rows"][2][1] and "99.17" in sell["rows"][3][1]
    assert "104.17" in buy["rows"][1][1] and "103.33" in buy["rows"][2][1] and "105.83" in buy["rows"][3][1]
    assert "reward : risk = <b>2.00 : 1</b>" in SPEC.preview(105, 100, CLASSIC)["summary"]          # the default is the losing-side stop
    w = SPEC.preview(8998, 8617, {**CLASSIC, "stop_side": "outside"})
    assert "8,490.00" in w["cards"][0]["rows"][2][1] and "beyond the target" in w["cards"][0]["rows"][2][1]
    assert "can never limit a loss" in " ".join(w["notes"])
    assert "can never limit a loss" not in " ".join(SPEC.preview(105, 100, {**CLASSIC, "stop_side": "inside"})["notes"])
    with pytest.raises(ValueError):
        SPEC.preview(105, 100, {**CLASSIC, "entry_frac": 0.5, "stop_frac": 0.2, "stop_side": "inside"})


def _random_days(seed=3, n=25):
    rnd = random.Random(seed)
    candles, price, day = [], 100.0, datetime(2026, 1, 5, 9, 15)
    for _ in range(n):
        for k in range(375):
            o = price
            c = o * (1 + rnd.gauss(0, 0.0008))
            hi, lo = max(o, c) * (1 + abs(rnd.gauss(0, 0.0003))), min(o, c) * (1 - abs(rnd.gauss(0, 0.0003)))
            candles.append({"ts": day + timedelta(minutes=k), "open": o, "high": hi, "low": lo, "close": c})
            price = c
        day += timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
    return candles


@pytest.mark.parametrize("gap", ["on", "off"])
@pytest.mark.parametrize("side", ["inside", "outside"])
@pytest.mark.parametrize("mode", ["approach", "after_break"])
@pytest.mark.parametrize("fr", [(1 / 6, 1 / 3, 1 / 3), (0.25, 0.5, 0.5), (0.0, 0.25, 1 / 6), (0.1, 0.2, 0.9)])
def test_fast_simulator_matches_the_engine(mode, fr, side, gap):
    candles = _random_days()
    ef, sf, tf = fr
    params = Params(strategy="range_thirds_research", options={**CLASSIC, "entry_frac": ef, "stop_frac": sf, "target_frac": tf, "entry_mode": mode, "stop_side": side,
                                                      "gap_filter": gap})
    eng = [t["net_pct"] for r in replay_range(candles, params, "NSE_EQ")["per_day"].values() for t in r["trade_list"]]
    by = defaultdict(list)
    for c in candles:
        by[c["ts"].date()].append(c)
    ds, fast = sorted(by), []
    for prev, cur in zip(ds, ds[1:]):
        hi, lo = max(c["high"] for c in by[prev]), min(c["low"] for c in by[prev])
        pts = []
        for c in by[cur]:
            if c["ts"].time() >= dtime(15, 15):
                break
            pts.append(candle_path(c["open"], c["high"], c["low"], c["close"]))
        fast += sim_day(hi, lo, pts, ef, sf, tf, mode, stop_side=side, gap_filter=gap == "on")
    assert len(eng) == len(fast) and len(eng) > 3
    assert all(abs(a - b) < 1e-9 for a, b in zip(eng, fast))


def test_full_dry_run_through_the_api(tmp_path):
    prices = [102, 101.5, 101.0, 100.8, 100.3, 99.8, 99.3, 99.1]          # armed, trigger 100.83, then the target at 99.13
    st = Settings(database_url=f"sqlite:///{tmp_path}/t.db", feed_mode="sim")
    app = create_app(st, feed_factory=lambda t, n, s: ReplayFeed(t, prices, T0, on_note=n, on_status=s), load_master=False)
    with TestClient(app) as c:
        assert "range_thirds_research" not in [s["name"] for s in c.get("/api/strategies").json()["strategies"]]          # hidden from the dropdowns
        r = c.post("/api/runs", json={"query": "BAJFINANCE", "prev_high": 105, "prev_low": 100, "params": {"strategy": "range_thirds_research", "slippage_pct": 0, "cost_pct": 0, "options": CLASSIC}})
        assert r.status_code == 200, r.text
        rid = r.json()["id"]
        for _ in range(100):
            runs = c.get("/api/state").json()["runs"]
            if runs and runs[0]["summary"]["trades"] == 1:
                break
            time.sleep(0.05)
        run_ = c.get("/api/state").json()["runs"][0]
        lv = {x["name"]: x["value"] for x in run_["levels"]}
        assert run_["strategy"] == "range_thirds_research" and lv["buy trigger"] == pytest.approx(105 - 5 / 6) and lv["sell stop"] == pytest.approx(100 + 5 / 3)
        assert run_["summary"]["trades"] == 1 and run_["summary"]["wins"] == 1 and "SELL armed" in run_["flags"]
        assert c.post(f"/api/runs/{rid}/stop").status_code == 200
        d = c.get(f"/api/runs/{rid}").json()
        assert [s["kind"] for s in d["signals"]][:2] == ["armed", "armed"]
        assert d["trades"][0]["side"] == "SELL" and d["trades"][0]["exit_reason"] == "target"
        bad = c.post("/api/runs", json={"query": "BAJFINANCE", "prev_high": 105, "prev_low": 100,
                                         "params": {"options": {**CLASSIC, "entry_frac": 0.5, "stop_frac": 0.2, "stop_side": "inside"}}})
        assert bad.status_code == 400
        assert c.post("/api/runs", json={"query": "BAJFINANCE", "prev_high": 105, "prev_low": 100, "params": {"options": {**CLASSIC, "stop_side": "sideways"}}}).status_code == 400
        p = c.post("/api/strategies/range_thirds_research/preview", json={"high": 8998, "low": 8617, "options": CLASSIC}).json()
        assert "8,680.50" in p["cards"][0]["rows"][1][1]


# ---- day-open filter: no SELL when the day opens below the previous Low, no BUY when it opens above the previous High (approach mode)
RALLY_THEN_FALL = [98.0, 99.0, 101.0, 102.0, 100.8, 100.0, 99.1]          # opens below the Low (100), rallies above the SELL trigger, falls back to it


def test_no_sell_when_the_day_opens_below_the_previous_low():
    e, ev = run(RALLY_THEN_FALL)
    assert e.closed == [] and e.position is None and e.day_open == 98.0
    [note] = [x for x in ev if x.kind == "note"]
    assert note.side == "SELL" and "opened at 98.00, below the previous Low 100.00" in note.reason and e.describe_event(note) == note.reason
    assert "SELL is off today" in e.status_text(100.0)
    assert e.blocked("SELL") and not e.blocked("BUY")
    off, _ = run(RALLY_THEN_FALL, gap_filter="off")                                 # the switch restores the old behaviour
    assert off.closed[0]["side"] == "SELL" and off.closed[0]["exit_reason"] == "target"
    inside, _ = run([102.0] + RALLY_THEN_FALL[1:])                                  # the same moves after an open inside the range do trade
    assert inside.closed[0]["side"] == "SELL"


def test_no_buy_when_the_day_opens_above_the_previous_high():
    path = [107.0, 106.0, 104.0, 104.3, 106.0]                                       # opens above the High (105), falls under the BUY trigger, rises to it
    e, ev = run(path)
    assert e.closed == [] and e.day_open == 107.0 and e.blocked("BUY") and not e.blocked("SELL")
    assert [x.side for x in ev if x.kind == "note"] == ["BUY"]
    off, _ = run(path, gap_filter="off")
    assert off.closed[0]["side"] == "BUY" and off.closed[0]["exit_reason"] == "target"


def test_the_filter_only_applies_to_approach_mode_and_a_preset_day_open_wins():
    e, _ = run([99.5, 99.0, 100.5, 100.9, 100.0, 99.2], entry_mode="after_break")   # after break is meant for a day that already broke the level
    assert e.closed[0]["side"] == "SELL" and not e.blocked("SELL")
    e = make_engine(H, L, P(stop_side="inside"))
    e.day_open = 101.0                                                                # the runner sets the real opening price for a run started mid-session
    for i, x in enumerate(RALLY_THEN_FALL):
        e.on_tick(T0 + timedelta(seconds=i), x)
    assert e.day_open == 101.0 and e.closed and e.closed[0]["side"] == "SELL"


def test_replay_and_fast_simulator_skip_the_gap_day():
    day = lambda d, rows: [{"ts": datetime(2026, 10, d, 10, k), "open": o, "high": h, "low": lo, "close": c} for k, (o, h, lo, c) in enumerate(rows)]      # noqa: E731
    candles = day(12, [(102, 105, 100, 103)]) + day(13, [(98, 102, 98, 101), (101, 101, 99, 99.2)])
    base = {"stop_side": "inside"}
    on = replay_range(candles, Params(strategy="range_thirds_research", options={**CLASSIC, **base}, slippage_pct=0, cost_pct=0))
    off = replay_range(candles, Params(strategy="range_thirds_research", options={**CLASSIC, **base, "gap_filter": "off"}, slippage_pct=0, cost_pct=0))
    assert on["trades"] == 0 and off["trades"] == 1
    pts = [candle_path(98, 102, 98, 101), candle_path(101, 101, 99, 99.2)]
    assert sim_day(105, 100, pts, 1 / 6, 1 / 3, 1 / 3, "approach", slip=0, cost=0) == []
    assert len(sim_day(105, 100, pts, 1 / 6, 1 / 3, 1 / 3, "approach", slip=0, cost=0, gap_filter=False)) == 1


def test_preview_and_form_show_the_filter():
    assert "Day-open filter" in " ".join(SPEC.preview(105, 100, {})["notes"])
    assert "Day-open filter" not in " ".join(SPEC.preview(105, 100, {"gap_filter": "off"})["notes"])
    assert "Day-open filter" not in " ".join(SPEC.preview(105, 100, {"entry_mode": "after_break"})["notes"])
    f = next(x for x in SPEC.fields if x["key"] == "gap_filter")
    assert f["default"] == "on" and [c[0] for c in f["choices"]] == ["on", "off"]


def _app_at(tmp_path, clock):
    st = Settings(database_url=f"sqlite:///{tmp_path}/g.db", feed_mode="sim")
    return create_app(st, feed_factory=lambda t, n, s: ReplayFeed(t, [], T0, on_note=n, on_status=s), load_master=False, clock=clock)


def test_a_run_started_mid_session_reads_the_real_day_open(tmp_path):
    from dryrun import instruments as instr
    from dryrun.history import store_candles
    app = _app_at(tmp_path, lambda: datetime(2026, 10, 13, 11, 0))                  # Tuesday 11:00 IST, the NSE session opened at 09:15
    with TestClient(app) as c:
        f = app.state.factory
        i = instr.search(f, "BAJFINANCE", 1)[0]
        store_candles(f, i.id, "1m", [{"ts": datetime(2026, 10, 13, 9, 15), "open": 98.0, "high": 99.0, "low": 97.5, "close": 98.5},
                                      {"ts": datetime(2026, 10, 13, 9, 16), "open": 98.5, "high": 99.0, "low": 98.0, "close": 98.7}], "api")
        r = c.post("/api/runs", json={"instrument_id": i.id, "prev_high": 105, "prev_low": 100, "params": {"strategy": "range_thirds_research"}})
        assert r.status_code == 200, r.text
        assert app.state.manager.runs[r.json()["id"]].engine.day_open == 98.0
        # candles that only start late in the morning are not the opening candle: the first price seen is used instead
        j = instr.search(f, "RELIANCE", 1)[0]
        store_candles(f, j.id, "1m", [{"ts": datetime(2026, 10, 13, 10, 30), "open": 50.0, "high": 51.0, "low": 49.0, "close": 50.5}], "api")
        r = c.post("/api/runs", json={"instrument_id": j.id, "prev_high": 105, "prev_low": 100, "params": {"strategy": "range_thirds_research"}})
        assert r.status_code == 200 and app.state.manager.runs[r.json()["id"]].engine.day_open is None
    (tmp_path / "x").mkdir()
    app = _app_at(tmp_path / "x", lambda: datetime(2026, 10, 13, 9, 15, 20))
    with TestClient(app) as c:                                                       # started within a minute of the open: no lookup, the first price is the open
        i = instr.search(app.state.factory, "BAJFINANCE", 1)[0]
        r = c.post("/api/runs", json={"instrument_id": i.id, "prev_high": 105, "prev_low": 100, "params": {"strategy": "range_thirds_research"}})
        assert r.status_code == 200 and app.state.manager.runs[r.json()["id"]].engine.day_open is None


# ---- candle replay fills an order at the level the price crossed, not at the candle's extreme
def _replay(day2, **o):
    day = lambda d, rows: [{"ts": datetime(2026, 10, d, 10, k), "open": a, "high": b, "low": c, "close": e} for k, (a, b, c, e) in enumerate(rows)]      # noqa: E731
    candles = day(12, [(102, 105, 100, 103)]) + day(13, day2)                           # day 1 gives H 105, L 100 (D 5): BUY trigger 104.167, stop 103.333, target 105.833
    r = replay_range(candles, Params(strategy="range_thirds_research", options={**CLASSIC, "stop_side": "inside", **o}, slippage_pct=0, cost_pct=0), "NSE_EQ")
    return r["per_day"][datetime(2026, 10, 13).date()]["trade_list"]


def test_a_candle_that_jumps_through_the_trigger_fills_at_the_trigger():
    [t] = _replay([(102, 106, 101.9, 105.8)])                                          # up bar: 102 -> 101.9 -> 106: crosses 104.167 and then the target 105.833
    assert t["side"] == "BUY" and t["entry_price"] == pytest.approx(105 - 5 / 6) and t["exit_reason"] == "target"
    assert t["exit_price"] == pytest.approx(105 - 5 / 6 + 5 / 3) and t["net_pnl"] == pytest.approx(5 / 3)


def test_a_candle_that_plunges_through_the_stop_exits_at_the_stop_with_slippage_only():
    day = lambda d, rows: [{"ts": datetime(2026, 10, d, 10, k), "open": a, "high": b, "low": c, "close": e} for k, (a, b, c, e) in enumerate(rows)]      # noqa: E731
    candles = day(12, [(102, 105, 100, 103)]) + day(13, [(102, 104.5, 101.9, 104.3), (104.3, 104.4, 99.0, 99.5)])
    r = replay_range(candles, Params(strategy="range_thirds_research", options={**CLASSIC, "stop_side": "inside"}, slippage_pct=0.1, cost_pct=0), "NSE_EQ")
    t = r["per_day"][datetime(2026, 10, 13).date()]["trade_list"][0]                    # (the fall to 99 then also takes the SELL, a second trade)
    assert t["side"] == "BUY" and t["exit_reason"] == "stop" and t["exit_price"] == pytest.approx((105 - 5 / 3) * (1 - 0.001))        # the stop level 103.333, not the candle's low of 99


def test_a_gap_between_candles_is_still_a_gap():
    [t] = _replay([(103.0, 103.2, 102.9, 103.0), (106.0, 106.2, 105.9, 106.1)])         # the second candle opens above the trigger: filled at its open, not at 104.167
    assert t["side"] == "BUY" and t["entry_price"] == pytest.approx(106.0)


# ---- the current rule ("dev"): entry = D/3 - the 0.8, stop = entry - 0.8, target = entry + D/3 + (D/3 - 0.8); window, one trade a day -------------------
def PD(**o):
    """Current-rule params with no entry window unless a test asks for it."""
    return Params(strategy="range_thirds_research", options={"skip_minutes": 0, "last_entry": "none", **o}, slippage_pct=0, cost_pct=0)


def play(ticks, session_open=dtime(9, 0), day_open=None, **o):
    e = make_engine(H, L, PD(**o))
    e.session_open, e.day_open = session_open, day_open
    ev = []
    for ts, px in ticks:
        ev += e.on_tick(ts, px)
    return e, ev


def at(h, m, sec=0):
    return datetime(2026, 10, 13, h, m, sec)


def test_current_rule_geometry_in_percent_and_in_points():
    e = make_engine(H, L, PD())                                    # D = 5, third 1.6667, dev 0.8% of 105 = 0.84 (BUY) and 0.8% of 100 = 0.80 (SELL)
    assert e.buy_entry_level == pytest.approx(105 - (5 / 3 - 0.84)) and e.buy_stop_level == pytest.approx(105 - 5 / 3) and e.buy_target_level == pytest.approx(105 + 5 / 3)
    assert e.sell_entry_level == pytest.approx(100 + (5 / 3 - 0.8)) and e.sell_stop_level == pytest.approx(100 + 5 / 3) and e.sell_target_level == pytest.approx(100 - 5 / 3)
    assert e.buy_entry_level - e.buy_stop_level == pytest.approx(0.84) and e.sell_stop_level - e.sell_entry_level == pytest.approx(0.8)      # the stop is exactly the 0.8 from the entry
    assert [x["name"] for x in e.levels()] == ["prev High", "buy trigger", "sell trigger", "prev Low", "buy stop", "sell stop", "buy target", "sell target"]
    pts = make_engine(H, L, PD(dev_unit="points"))
    assert pts.buy_entry_level == pytest.approx(105 - (5 / 3 - 0.8)) and pts.buy_stop_level == pytest.approx(105 - 5 / 3)
    c = make_engine(105, 95, PD())                                 # a wider range: D = 10
    assert c.buy_target_level == pytest.approx(105 + 10 / 3) and c.buy_entry_level - c.buy_stop_level == pytest.approx(0.84)


def test_current_rule_buy_trade_uses_fixed_stop_and_target():
    e, ev = play([(at(9, 0), 102.0), (at(9, 5), 103.5), (at(9, 6), 104.2), (at(9, 7), 105.0), (at(9, 8), 106.0), (at(9, 9), 106.7)])
    [t] = e.closed
    assert t["side"] == "BUY" and t["entry_price"] == pytest.approx(104.2) and t["stop"] == pytest.approx(105 - 5 / 3) and t["target"] == pytest.approx(105 + 5 / 3)
    assert t["exit_reason"] == "target" and t["exit_price"] == pytest.approx(105 + 5 / 3)


def test_current_rule_sell_trade_and_stop_loss():
    e, _ = play([(at(9, 0), 103.0), (at(9, 5), 102.0), (at(9, 6), 100.8), (at(9, 7), 100.0), (at(9, 8), 98.2)])
    [t] = e.closed
    assert t["side"] == "SELL" and t["stop"] == pytest.approx(100 + 5 / 3) and t["target"] == pytest.approx(100 - 5 / 3) and t["exit_reason"] == "target"
    e, _ = play([(at(9, 0), 102.0), (at(9, 5), 103.5), (at(9, 6), 104.2), (at(9, 7), 103.3)])          # the stop 103.33 is 0.84 below the 104.17 entry level
    [t] = e.closed
    assert t["exit_reason"] == "stop" and t["exit_price"] == pytest.approx(103.3) and t["net_pnl"] < 0


def test_one_trade_a_day_and_the_other_side_is_cancelled_once_one_enters():
    prices = [(at(9, 0), 102.0), (at(9, 5), 103.5), (at(9, 6), 104.2), (at(9, 7), 103.3), (at(9, 8), 102.0), (at(9, 9), 100.8), (at(9, 10), 100.0), (at(9, 11), 99.0), (at(9, 12), 98.2)]
    e, ev = play(prices)
    assert len(e.closed) == 1 and e.closed[0]["side"] == "BUY" and e.position is None                  # the stop was hit; the SELL at 100.8 was not allowed
    assert e.armed == {"BUY": False, "SELL": False} and "Today's trade has been entered" in e.status_text(None)
    two, _ = play(prices, trades_per_day=2)                                                              # with two trades a day the SELL goes ahead
    assert [t["side"] for t in two.closed] == ["BUY", "SELL"]


def test_no_entry_in_the_first_candle_and_none_from_the_cutoff():
    ticks = [(at(9, 0), 102.0), (at(9, 2), 104.3), (at(9, 5), 103.0), (at(9, 6), 104.2)]
    e, _ = play(ticks, skip_minutes=5)                                       # 104.3 in the first 5 minutes would have hit the trigger; nothing is armed until 09:05
    pos = e.position
    assert pos is not None and pos.entry_ts == at(9, 6) and pos.entry_price == pytest.approx(104.2)
    e, _ = play([(at(9, 0), 102.0), (at(9, 2), 104.3), (at(9, 3), 102.5), (at(9, 4), 104.4)], skip_minutes=5)
    assert e.position is None and e.closed == []                             # everything happened inside the first candle
    e, _ = play([(at(17, 0), 102.0), (at(17, 58), 103.0), (at(18, 1), 104.3)], session_open=dtime(17, 0), last_entry="18:00")        # armed at 17:58, cancelled at 18:00
    assert e.position is None and e.armed == {"BUY": False, "SELL": False}
    e, _ = play([(at(17, 0), 102.0), (at(17, 58), 103.0), (at(17, 59), 104.3)], session_open=dtime(17, 0), last_entry="18:00")
    assert e.position is not None                                            # the same move one minute earlier trades


def test_a_side_is_skipped_when_the_range_is_too_small_for_the_dev():
    e = make_engine(100.5, 100.0, PD())                                      # D = 0.5: D/3 + (D/3 - 0.84) is negative
    assert not e.side_valid("BUY") and not e.side_valid("SELL")
    for i, px in enumerate([100.2, 100.1, 100.4, 100.45]):
        e.on_tick(at(9, i), px)
    assert e.position is None and e.closed == []
    assert "too small" in " ".join(SPEC.preview(100.5, 100, {})["notes"])


def test_current_rule_validation_preview_and_description():
    for bad in (dict(dev_unit="x"), dict(dev_value=0), dict(third_frac=0), dict(trades_per_day=0), dict(last_entry="7pm"), dict(skip_minutes=-1), dict(rule="x")):
        with pytest.raises(ValueError):
            PD(**bad).validate()
    p = SPEC.preview(105, 100, {})
    assert "103.33" in p["cards"][0]["rows"][2][1] and "106.67" in p["cards"][0]["rows"][3][1] and "104.17" in p["cards"][0]["rows"][1][1]      # BUY card: entry, stop, target
    assert "100.87" in p["cards"][1]["rows"][1][1] and "101.67" in p["cards"][1]["rows"][2][1] and "98.33" in p["cards"][1]["rows"][3][1]
    assert "Entry window" in " ".join(p["notes"]) and "18:00" in " ".join(p["notes"])
    assert "BUY entry 104.17, stop 103.33, target 106.67" in SPEC.describe(105, 100, {}) and "classic" in SPEC.describe(105, 100, {"rule": "classic"})
    assert [f["key"] for f in SPEC.fields][:2] == ["entry_mode", "rule"] and SPEC.fields[1]["default"] == "dev"


def test_replay_fills_the_current_rule_at_its_level_after_the_first_candle():
    day = lambda d, rows: [{"ts": datetime(2026, 10, d, 9, 15) + timedelta(minutes=5 * k), "open": a, "high": b, "low": c, "close": e} for k, (a, b, c, e) in enumerate(rows)]      # noqa: E731
    candles = day(12, [(102, 105, 100, 103)]) + day(13, [(102.0, 102.5, 101.9, 102.3), (102.4, 104.5, 102.3, 104.4)])
    r = replay_range(candles, Params(strategy="range_thirds_research", options={}, slippage_pct=0, cost_pct=0), "NSE_EQ")
    [t] = r["per_day"][datetime(2026, 10, 13).date()]["trade_list"]
    assert t["side"] == "BUY" and t["entry_price"] == pytest.approx(105 - (5 / 3 - 0.84)) and t["entry_ts"].time() >= dtime(9, 20)      # at the trigger, in the second candle
    assert t["stop"] == pytest.approx(105 - 5 / 3) and t["target"] == pytest.approx(105 + 5 / 3)
