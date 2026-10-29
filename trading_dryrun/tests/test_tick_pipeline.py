"""Tick pipeline hardening: jump confirmation, duplicate / out-of-order skipping, per-instrument stale check,
orphaned trades after a restart, keep-alive settings, the Ticks page API and stored-tick management."""
import time
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from demo_strategy import jp
from dryrun.app import create_app
from dryrun.config import Settings
from dryrun.db import RiskLog, Run, Tick, Trade
from dryrun.dhan_feed import DhanFeed, FeedTick
from dryrun.sim_feed import ReplayFeed

T0 = datetime(2026, 10, 12, 10, 0, 0)


def build(tmp_path, name="t.db", clock=None, **kw):
    st = Settings(database_url=f"sqlite:///{tmp_path}/{name}", feed_mode="sim", **kw)
    return create_app(st, feed_factory=lambda t, n, s: ReplayFeed(t, [], datetime.now(), on_note=n, on_status=s), load_master=False, clock=clock)


def start_run(c, query, high=100, low=90, **params):
    r = c.post("/api/runs", json={"query": query, "prev_high": high, "prev_low": low,
                                   "params": jp(**{"slippage_pct": 0, "cost_pct": 0, **params})})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def tick(m, sid, px, sec, ltt=None, vol=None, seg="NSE_EQ"):
    m._on_tick(FeedTick(seg, sid, px, ltt, vol, None, None, T0 + timedelta(seconds=sec)))


def test_large_real_move_is_confirmed_by_the_next_tick(tmp_path):
    app = build(tmp_path)
    with TestClient(app) as c:
        m = app.state.manager
        rid = start_run(c, "BAJFINANCE")
        run = m.runs[rid]
        key = (run.inst.segment, run.inst.security_id)
        for i, px in enumerate([98, 98.5, 99]):
            tick(m, key[1], px, i)
        tick(m, key[1], 123.75, 3)                 # +25%: held, not yet used
        st = m.states[key]
        assert st.last_price == 99 and st.pending is not None
        tick(m, key[1], 124.0, 4)                  # the next tick agrees: both are accepted
        assert st.last_price == 124.0 and st.jump_confirmed == 1 and st.bad_ticks == 0
        tick(m, key[1], 125.0, 5)                  # and the feed keeps flowing afterwards
        assert st.last_price == 125.0 and st.accepted == 6


def test_bad_print_is_dropped_when_the_next_tick_contradicts_it(tmp_path):
    app = build(tmp_path)
    with TestClient(app) as c:
        m = app.state.manager
        rid = start_run(c, "BAJFINANCE")
        run = m.runs[rid]
        key = (run.inst.segment, run.inst.security_id)
        for i, px in enumerate([98, 98.5, 99]):
            tick(m, key[1], px, i)
        tick(m, key[1], 150.0, 3)                  # spike
        tick(m, key[1], 99.2, 4)                   # back to normal
        st = m.states[key]
        assert st.bad_ticks == 1 and st.last_price == 99.2 and st.pending is None
        assert 150.0 not in [p for _t, p in run.recent]
        assert run.engine.armed == {"BUY": False, "SELL": False}      # the spike did not arm a break


def test_identical_repeat_of_a_jump_does_not_confirm_it(tmp_path):
    app = build(tmp_path)
    with TestClient(app) as c:
        m = app.state.manager
        rid = start_run(c, "BAJFINANCE")
        run = m.runs[rid]
        key = (run.inst.segment, run.inst.security_id)
        tick(m, key[1], 99.0, 0, ltt=1000, vol=10)
        tick(m, key[1], 150.0, 1, ltt=1001, vol=11)         # spike held
        tick(m, key[1], 150.0, 1, ltt=1001, vol=11)         # the feed repeats the same packet: still unconfirmed
        st = m.states[key]
        assert st.pending is not None and st.last_price == 99.0 and st.dup_skipped == 1
        tick(m, key[1], 99.1, 2, ltt=1002, vol=12)
        assert st.bad_ticks == 1 and st.last_price == 99.1


def test_duplicates_and_out_of_order_packets_are_skipped(tmp_path):
    app = build(tmp_path)
    with TestClient(app) as c:
        m = app.state.manager
        rid = start_run(c, "BAJFINANCE")
        run = m.runs[rid]
        key = (run.inst.segment, run.inst.security_id)
        tick(m, key[1], 99.0, 0, ltt=1000, vol=10)
        tick(m, key[1], 99.0, 0, ltt=1000, vol=10)         # identical
        tick(m, key[1], 99.5, 0, ltt=1005, vol=12)
        tick(m, key[1], 99.2, 0, ltt=1003, vol=11)         # older exchange time: would move the price backwards
        tick(m, key[1], 99.6, 1, ltt=1005, vol=11)         # same second but lower cumulative volume: older too
        tick(m, key[1], 99.7, 1, ltt=1006, vol=13)
        st = m.states[key]
        assert (st.packets, st.accepted, st.dup_skipped, st.old_skipped) == (6, 3, 1, 2)
        assert st.last_price == 99.7 and run.ticks == 3
        m._flush_ticks()
        m.writer.flush()
        with app.state.factory() as s:
            assert [t.ltp for t in s.scalars(select(Tick).order_by(Tick.id))] == [99.0, 99.5, 99.7]      # only distinct ticks are stored


def test_a_new_day_forgets_yesterdays_price(tmp_path):
    app = build(tmp_path)
    with TestClient(app) as c:
        m = app.state.manager
        rid = start_run(c, "BAJFINANCE")
        run = m.runs[rid]
        key = (run.inst.segment, run.inst.security_id)
        tick(m, key[1], 100.0, 0)
        m._on_tick(FeedTick(key[0], key[1], 130.0, None, None, None, None, T0 + timedelta(days=1)))     # +30% gap overnight
        assert m.states[key].last_price == 130.0 and m.states[key].pending is None


def test_stale_check_is_per_instrument(tmp_path):
    app = build(tmp_path)
    with TestClient(app) as c:
        m = app.state.manager
        a = start_run(c, "BAJFINANCE", pullback_pct=1)
        b = start_run(c, "RELIANCE", pullback_pct=1)
        ra, rb = m.runs[a], m.runs[b]
        sa, sb = ra.inst.security_id, rb.inst.security_id
        tick(m, sa, 100.5, 0)                                  # A breaks the high, then goes silent
        for s in range(1, 60):
            tick(m, sb, 95.0 + s * 0.001, s)                   # B keeps ticking
        tick(m, sa, 99.0, 60)                                  # A's pullback arrives after 60 s of silence on A
        assert ra.engine.position is None, "an entry was taken on a feed that had been silent for 60 s"
        m.writer.flush()
        with app.state.factory() as s:
            assert any(r.check == "stale_feed" and not r.approved for r in s.scalars(select(RiskLog)))


def test_liveness_counts_skipped_duplicates(tmp_path):
    """On a quiet instrument the feed keeps repeating packets; those must keep the stale check satisfied."""
    app = build(tmp_path)
    with TestClient(app) as c:
        m = app.state.manager
        rid = start_run(c, "BAJFINANCE", pullback_pct=1)
        run = m.runs[rid]
        sid = run.inst.security_id
        tick(m, sid, 100.5, 0, ltt=1000, vol=1)
        for s in range(1, 60, 5):
            tick(m, sid, 100.5, s, ltt=1000, vol=1)            # duplicates every 5 s, no new price
        tick(m, sid, 99.0, 60, ltt=1060, vol=2)
        assert run.engine.position is not None


def test_restart_closes_open_trades_at_the_last_price(tmp_path):
    app = build(tmp_path, "r.db")
    with TestClient(app) as c:
        m = app.state.manager
        rid = start_run(c, "BAJFINANCE", pullback_pct=1, target_pct=5, stop_pct=None, cost_pct=0.1, slippage_pct=0.02)
        sid = m.runs[rid].inst.security_id
        for i, px in enumerate([100.5, 99.0, 99.6]):
            tick(m, sid, px, i)
        m._update_run(m.runs[rid])
        time.sleep(0.8)
        m.writer.flush()
        m.runs.clear()                                         # crash: no graceful finalize
    app2 = build(tmp_path, "r.db")
    with TestClient(app2):
        with app2.state.factory() as s:
            run = s.scalar(select(Run).where(Run.id == rid))
            [t] = s.scalars(select(Trade).where(Trade.run_id == rid)).all()
            assert run.status == "interrupted" and run.summary["open_trades_closed"] == 1
            assert t.exit_reason == "interrupted" and t.exit_ts is not None and t.exit_price is not None
            assert t.exit_price < 99.6 and abs(t.net_pnl - (t.gross_pnl - t.cost)) < 1e-9 and t.gross_pnl > 0


def test_feed_has_a_client_keepalive():
    f = DhanFeed("1", "tok", lambda t: None)
    assert f.ping_interval and f.ping_timeout and f.ping_timeout >= f.ping_interval * 0.5


def test_ticks_page_and_api(tmp_path):
    app = build(tmp_path, clock=lambda: T0)
    with TestClient(app) as c:
        assert c.get("/ticks").status_code == 200
        m = app.state.manager
        rid = start_run(c, "BAJFINANCE")
        sid = m.runs[rid].inst.security_id
        for i in range(30):
            tick(m, sid, 99.0 + i * 0.01, 0, ltt=1000 + i, vol=i)
        d = c.get("/api/ticks").json()
        [row] = d["instruments"]
        assert row["packets"] == 30 and row["accepted"] == 30 and row["kind"] == "dry run" and row["runs"] == [rid]
        assert row["status"] == "receiving" and len(row["persec"]) == 60 and sum(row["persec"]) == 30
        assert row["proc_avg_us"] is not None and row["tail"][0]["ltp"] == 99.29
        assert d["feed"]["connected"] and "retention_days" in d and d["retention_days"] == 3


def test_retention_download_and_delete(tmp_path):
    app = build(tmp_path)
    with TestClient(app) as c:
        m = app.state.manager
        rid = start_run(c, "BAJFINANCE")
        inst_id = m.runs[rid].inst.id
        old = datetime(2026, 10, 1, 10, 0, 0)
        with app.state.factory() as s:
            s.add_all([Tick(instrument_id=inst_id, ts=old + timedelta(seconds=i), ltp=100 + i, ltt=i, volume=i) for i in range(5)])
            s.commit()
        assert c.post("/api/settings/ticks", json={"retention_days": 0}).status_code == 422
        assert c.post("/api/settings/ticks", json={"retention_days": 2}).json()["retention_days"] == 2
        assert c.get("/api/settings").json()["tick_retention_days"] == 2
        days = c.get("/api/ticks/days").json()["days"]
        assert days[0]["date"] == "2026-10-01" and days[0]["rows"] == 5
        csv = c.get(f"/api/ticks/export.csv?day=2026-10-01&instrument_id={inst_id}")
        assert csv.status_code == 200 and "attachment" in csv.headers["content-disposition"]
        lines = csv.text.strip().splitlines()
        assert lines[0].startswith("ts,symbol,ltp") and len(lines) == 6 and "BAJFINANCE" in lines[1]
        assert c.delete("/api/ticks").status_code == 400                       # refuses to delete everything without a condition
        assert c.delete(f"/api/ticks?day=2026-10-01&instrument_id={inst_id}").json()["deleted"] == 5
        assert c.get("/api/ticks/days").json()["days"] == []
        with app.state.factory() as s:
            s.add(Tick(instrument_id=inst_id, ts=datetime(2020, 1, 1), ltp=1.0))
            s.commit()
        assert c.delete("/api/ticks?older_than_days=2").json()["deleted"] == 1
