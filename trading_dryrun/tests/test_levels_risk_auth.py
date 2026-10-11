from datetime import date, datetime, time, timedelta

import pytest

from dryrun import instruments as instr
from dryrun import levels
from dryrun.auth import Auth, OtpService
from dryrun.config import Settings
from dryrun.db import Tick, init_db, purge_old_ticks
from dryrun.history import store_candles
from dryrun.risk import RiskContext, RiskEngine
from sqlalchemy import select


@pytest.fixture()
def factory(tmp_path):
    _e, f = init_db(f"sqlite:///{tmp_path}/t.db")
    instr.seed_defaults(f)
    return f


def inst(f, sym="BAJFINANCE"):
    return instr.search(f, sym, 1)[0]


def test_levels_prefer_manual_then_daily_then_intraday(factory):
    i = inst(factory)
    today = date(2026, 10, 12)
    assert levels.from_db(factory, i, today) is None                       # nothing stored -> ask the user
    # intraday candles for 9 Oct (regular session) -> derived
    rows = [{"ts": datetime(2026, 10, 9, 9, 15) + timedelta(minutes=k), "open": 960, "high": 960 + k, "low": 950 - k % 3, "close": 960}
            for k in range(30)]
    rows.append({"ts": datetime(2026, 10, 9, 18, 0), "open": 1, "high": 5000, "low": 1, "close": 1})      # after-hours junk ignored
    store_candles(factory, i.id, "1m", rows, "api")
    lv = levels.from_db(factory, i, today)
    assert lv.source == "derived" and lv.high == 989 and lv.low == 948 and lv.from_date == date(2026, 10, 9)
    # a daily candle wins over derived
    store_candles(factory, i.id, "D", [{"ts": datetime(2026, 10, 9), "open": 955, "high": 990.5, "low": 945.5, "close": 960}], "api")
    lv = levels.from_db(factory, i, today)
    assert (lv.source, lv.high, lv.low) == ("candles", 990.5, 945.5)
    # today's own daily candle must not be used as "previous day"
    store_candles(factory, i.id, "D", [{"ts": datetime(2026, 10, 12), "open": 1, "high": 2000, "low": 1, "close": 1}], "api")
    assert levels.from_db(factory, i, today).high == 990.5
    # manual beats everything
    levels.set_manual(factory, i.id, today, 1000, 940)
    assert (levels.from_db(factory, i, today).source) == ("manual")
    with pytest.raises(ValueError):
        levels.set_manual(factory, i.id, today, 900, 950)


def test_store_candles_is_idempotent(factory):
    i = inst(factory)
    rows = [{"ts": datetime(2026, 10, 9, 9, 15), "open": 1, "high": 2, "low": 1, "close": 2}]
    assert store_candles(factory, i.id, "1m", rows, "api") == 1
    assert store_candles(factory, i.id, "1m", rows, "api") == 0


def test_instrument_search_modes(factory):
    assert instr.search(factory, "bajaj")[0].symbol == "BAJFINANCE"
    assert instr.search(factory, "IDX_I:13")[0].symbol == "NIFTY"
    assert instr.search(factory, "317")[0].segment == "NSE_EQ"
    assert instr.search(factory, "bajaj fin")[0].symbol == "BAJFINANCE"       # words match in any order
    assert instr.search(factory, "zzzz") == []


def test_parse_master_filters_expired_and_options():
    csv_text = ("EXCH_ID,SEGMENT,SECURITY_ID,ISIN,INSTRUMENT,UNDERLYING_SECURITY_ID,UNDERLYING_SYMBOL,SYMBOL_NAME,DISPLAY_NAME,INSTRUMENT_TYPE,SERIES,LOT_SIZE,SM_EXPIRY_DATE,STRIKE_PRICE,OPTION_TYPE,TICK_SIZE,EXPIRY_FLAG\n"
                "NSE,I,13,NA,INDEX,,NIFTY,NIFTY,Nifty 50,INDEX,,1.0,0001-01-01,-0.01,XX,0.05,\n"
                "NSE,D,1,NA,FUTIDX,,NIFTY,NIFTY-Oct2026-FUT,NIFTY OCT FUT,FUTIDX,,65.0,2026-10-27,-0.01,XX,10.0,M\n"
                "NSE,D,2,NA,FUTIDX,,NIFTY,NIFTY-Aug2024-FUT,NIFTY AUG FUT,FUTIDX,,25.0,2024-08-29,-0.01,XX,10.0,M\n"
                "NSE,D,3,NA,OPTIDX,,NIFTY,NIFTY-Oct2026-24000-CE,x,OP,,65.0,2026-10-27,24000,CE,5.0,M\n")
    rows = instr.parse_master(csv_text, today=date(2026, 10, 11))
    assert sorted((r["security_id"], r["segment"]) for r in rows) == [("1", "NSE_FNO"), ("13", "IDX_I")]


def test_tick_retention(factory):
    i = inst(factory)
    with factory() as s:
        s.add_all([Tick(instrument_id=i.id, ts=datetime.now() - timedelta(days=40), ltp=1),
                   Tick(instrument_id=i.id, ts=datetime.now(), ltp=2)])
        s.commit()
    assert purge_old_ticks(factory, 30) == 1
    with factory() as s:
        assert len(s.scalars(select(Tick)).all()) == 1


def risk(prev_msg, now, logs, ignore=False, last_entry=time(15, 0)):
    return RiskEngine(RiskContext(last_tick_at=lambda: prev_msg, now=lambda: now, last_entry_time=last_entry,
                                  session_open=time(9, 15), stale_seconds=15, ignore_hours=ignore,
                                  log=lambda c, ok, d: logs.append((c, ok))))


def test_risk_rejects_stale_feed_and_late_entries():
    logs = []
    now = datetime(2026, 10, 9, 11, 0, 0)
    assert risk(now - timedelta(seconds=2), now, logs).approve("BUY", now, 100, {})[0] is True
    ok, why = risk(now - timedelta(seconds=60), now, logs).approve("BUY", now, 100, {})
    assert not ok and "stale" in why
    late = datetime(2026, 10, 9, 15, 10)
    assert not risk(late - timedelta(seconds=1), late, logs).approve("BUY", late, 100, {})[0]
    assert risk(late - timedelta(seconds=1), late, logs, ignore=True).approve("BUY", late, 100, {})[0]
    assert ("stale_feed", False) in logs and ("session_window", False) in logs


def test_settings_refuse_public_bind_without_login():
    with pytest.raises(RuntimeError):
        Settings(feed_mode="sim", host="0.0.0.0", auth_mode="none").validate()
    Settings(feed_mode="sim", host="0.0.0.0", dashboard_password="x").validate()
    with pytest.raises(RuntimeError):
        Settings(feed_mode="dhan", dhan_client_id="", dhan_access_token="").validate()


def test_auth_tokens_expire_and_tamper():
    a = Auth("password", "pw", "k")
    tok = a.issue()
    assert a.verify(tok) and not a.verify(tok[:-2] + "xx") and not a.verify(None)
    assert Auth("none").verify(None)


def test_password_rate_limit():
    a = Auth("password", "pw", "k")
    for _ in range(5):
        assert not a.check_password("1.2.3.4", "bad")
    with pytest.raises(PermissionError):
        a.check_password("1.2.3.4", "pw")


class FakeHttp:
    def __init__(self):
        self.calls = []

    def post(self, url, **kw):
        self.calls.append((url, kw))

        class R:
            def raise_for_status(self):
                pass
        return R()


def test_otp_flow_via_brevo():
    http = FakeHttp()
    svc = OtpService("KEY", "from@example.com", ("me@example.com",), http=http)
    assert svc.request("someone@else.com") is False and not http.calls        # unknown address: nothing sent
    assert svc.request("ME@example.com") is True
    url, kw = http.calls[0]
    assert url == "https://api.brevo.com/v3/smtp/email" and kw["headers"]["api-key"] == "KEY"
    code = kw["json"]["htmlContent"].split("<b>")[1].split("</b>")[0]
    assert not svc.verify("me@example.com", "000000" if code != "000000" else "111111")
    assert svc.verify("me@example.com", code)
    assert not svc.verify("me@example.com", code)                              # single use


def test_otp_locks_after_too_many_tries():
    http = FakeHttp()
    svc = OtpService("KEY", "f@x.com", ("me@example.com",), http=http, max_tries=3)
    svc.request("me@example.com")
    code = http.calls[0][1]["json"]["htmlContent"].split("<b>")[1].split("</b>")[0]
    wrong = "123456" if code != "123456" else "654321"
    for _ in range(3):
        svc.verify("me@example.com", wrong)
    assert not svc.verify("me@example.com", code)


def test_env_values_with_trailing_comments_are_cleaned(monkeypatch):
    monkeypatch.setenv("FEED_MODE", "sim            # simulated")
    monkeypatch.setenv("TICK_RETENTION_DAYS", "30   # days")
    monkeypatch.setenv("STORE_TICKS", "false  # off")
    s = Settings()
    assert s.feed_mode == "sim" and s.tick_retention_days == 30 and s.store_ticks is False


def test_example_env_file_loads_and_starts(tmp_path, monkeypatch):
    """The shipped .env.example must parse (a trailing-comment bug once stopped the first deployment)."""
    import os
    from pathlib import Path
    from dryrun.__main__ import _load_env
    for k in list(os.environ):
        if k in ("FEED_MODE", "PORT", "HOST", "STORE_TICKS", "TICK_RETENTION_DAYS", "STALE_TICK_SECONDS", "DATABASE_URL"):
            monkeypatch.delenv(k)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text((Path(__file__).resolve().parents[1] / ".env.example").read_text())
    _load_env()
    s = Settings()
    s.validate()
    assert s.feed_mode == "sim" and s.port == 8000 and s.tick_retention_days == 30
    for k in ("FEED_MODE", "PORT", "HOST", "STORE_TICKS", "TICK_RETENTION_DAYS", "STALE_TICK_SECONDS", "DATABASE_URL", "FEED_PACKET",
              "DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN", "DASHBOARD_PASSWORD", "SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_simulated_candles_never_become_levels_and_dhan_daily_beats_derived(factory):
    i = inst(factory)
    today = date(2026, 10, 12)
    sim = [{"ts": datetime(2026, 10, 11, 13, 55) + timedelta(minutes=k), "open": 100, "high": 105, "low": 95, "close": 100} for k in range(3)]
    store_candles(factory, i.id, "1m", sim, "sim")
    assert levels.from_db(factory, i, today) is None                       # fake prices are ignored
    live = [{"ts": datetime(2026, 10, 9, 10, 0) + timedelta(minutes=k), "open": 960, "high": 970, "low": 950, "close": 960} for k in range(3)]
    store_candles(factory, i.id, "1m", live, "live")
    assert levels.from_db(factory, i, today).source == "derived"

    class FakeRest:
        def daily(self, *a, **k):
            return [{"ts": datetime(2026, 10, 9), "open": 958, "high": 975.0, "low": 948.0, "close": 965}]
    lv = levels.resolve(factory, i, today, rest=FakeRest())
    assert (lv.source, lv.high, lv.low) == ("candles", 975.0, 948.0)       # Dhan's daily candle, not our derived 970/950
