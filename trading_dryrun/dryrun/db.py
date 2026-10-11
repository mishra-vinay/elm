"""Database models (SQLAlchemy 2). SQLite by default; set DATABASE_URL for PostgreSQL.

Times are stored as naive Asia/Kolkata wall-clock datetimes (the only market this system trades).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import (JSON, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text,
                        UniqueConstraint, create_engine, event)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from .config import IST


def now_ist() -> datetime:
    """Current Asia/Kolkata wall-clock time as a naive datetime (the convention used for every stored time)."""
    return datetime.now(IST).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Instrument(Base):
    __tablename__ = "instruments"
    id: Mapped[int] = mapped_column(primary_key=True)
    segment: Mapped[str] = mapped_column(String(16))          # Dhan exchangeSegment enum: NSE_EQ, IDX_I, MCX_COMM ...
    security_id: Mapped[str] = mapped_column(String(16))
    instrument: Mapped[str] = mapped_column(String(12))       # INDEX, EQUITY, FUTIDX, FUTSTK, FUTCOM ...
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    lot_size: Mapped[float] = mapped_column(Float, default=1.0)
    tick_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    isin: Mapped[str | None] = mapped_column(String(16), nullable=True)
    __table_args__ = (UniqueConstraint("segment", "security_id"),)

    @property
    def label(self) -> str:
        return f"{self.name or self.symbol} [{self.segment}:{self.security_id}]"


class Candle(Base):
    """Historical + live-aggregated candles. interval: 'D' daily, '1m', '5m', '15m', '60m'."""
    __tablename__ = "candles"
    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    interval: Mapped[str] = mapped_column(String(4))
    ts: Mapped[datetime] = mapped_column(DateTime)             # bar start (daily: 00:00 of the date)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(12), default="api")   # api | live
    __table_args__ = (UniqueConstraint("instrument_id", "interval", "ts"),)


class Tick(Base):
    __tablename__ = "ticks"
    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    ts: Mapped[datetime] = mapped_column(DateTime)             # local receive time (IST)
    ltp: Mapped[float] = mapped_column(Float)
    ltt: Mapped[int | None] = mapped_column(Integer, nullable=True)       # exchange last-trade-time, raw
    volume: Mapped[int | None] = mapped_column(Integer, nullable=True)    # cumulative day volume (quote packets)
    day_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    day_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    __table_args__ = (Index("ix_ticks_inst_ts", "instrument_id", "ts"),)


class DailyLevel(Base):
    """Previous-day High/Low used to trade `for_date`. source: manual | candles | derived | api"""
    __tablename__ = "daily_levels"
    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    for_date: Mapped[date] = mapped_column(Date)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(12))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist)
    __table_args__ = (UniqueConstraint("instrument_id", "for_date"),)


class Watch(Base):
    """Instruments you asked the system to look after: record live ticks and/or keep history updated after the close."""
    __tablename__ = "watchlist"
    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), unique=True)
    collect_live: Mapped[bool] = mapped_column(Boolean, default=False)
    daily_update: Mapped[bool] = mapped_column(Boolean, default=False)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    instrument: Mapped[Instrument] = relationship()
    mode: Mapped[str] = mapped_column(String(8), default="dryrun")
    status: Mapped[str] = mapped_column(String(12), default="starting")  # waiting|running|finished|stopped|interrupted|error
    trading_date: Mapped[date] = mapped_column(Date)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    prev_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    prev_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    levels_source: Mapped[str | None] = mapped_column(String(12), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now_ist)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ticks_received: Mapped[int] = mapped_column(Integer, default=0)
    first_tick_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_tick_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    feed_disconnects: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    ran_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)       # "did the run work", set at the end
    summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)         # performance, set at the end
    signals: Mapped[list["Signal"]] = relationship(back_populates="run", cascade="all, delete-orphan")
    trades: Mapped[list["Trade"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class Signal(Base):
    """Every break and every entry signal, traded or not, with the reason."""
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    run: Mapped[Run] = relationship(back_populates="signals")
    ts: Mapped[datetime] = mapped_column(DateTime)
    kind: Mapped[str] = mapped_column(String(8))              # break | entry
    side: Mapped[str] = mapped_column(String(4))              # BUY | SELL
    price: Mapped[float] = mapped_column(Float)
    level: Mapped[float] = mapped_column(Float)
    action: Mapped[str] = mapped_column(String(8), default="info")   # info | taken | skipped
    reason: Mapped[str | None] = mapped_column(String(160), nullable=True)


class Trade(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    run: Mapped[Run] = relationship(back_populates="trades")
    side: Mapped[str] = mapped_column(String(4))
    qty: Mapped[float] = mapped_column(Float, default=1)
    signal_price: Mapped[float] = mapped_column(Float)
    entry_ts: Mapped[datetime] = mapped_column(DateTime)
    entry_price: Mapped[float] = mapped_column(Float)         # includes simulated slippage
    target: Mapped[float] = mapped_column(Float)
    stop: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_ts: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)   # target|stop|squareoff|manual
    gross_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_pct: Mapped[float | None] = mapped_column(Float, nullable=True)


class ApiLog(Base):
    __tablename__ = "api_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=now_ist)
    source: Mapped[str] = mapped_column(String(16))           # feed | rest | app
    level: Mapped[str] = mapped_column(String(8))
    message: Mapped[str] = mapped_column(Text)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)


class RiskLog(Base):
    __tablename__ = "risk_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=now_ist)
    check: Mapped[str] = mapped_column(String(32))
    approved: Mapped[bool] = mapped_column(Boolean)
    detail: Mapped[str | None] = mapped_column(String(200), nullable=True)


def make_engine(url: str):
    if url.startswith("sqlite:///") and url != "sqlite:///:memory:":
        Path(url.replace("sqlite:///", "", 1)).parent.mkdir(parents=True, exist_ok=True)
    kwargs = {"connect_args": {"check_same_thread": False, "timeout": 30}} if url.startswith("sqlite") else {"pool_pre_ping": True}
    if url == "sqlite:///:memory:":
        from sqlalchemy.pool import StaticPool
        kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _):  # WAL lets the API read while the writer thread inserts ticks
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()
    return engine


def init_db(url: str):
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(engine, expire_on_commit=False)


def purge_old_ticks(factory: sessionmaker, keep_days: int) -> int:
    from sqlalchemy import delete
    cutoff = now_ist() - timedelta(days=keep_days)
    with factory() as s:
        res = s.execute(delete(Tick).where(Tick.ts < cutoff))
        s.execute(delete(ApiLog).where(ApiLog.ts < now_ist() - timedelta(days=max(keep_days, 60))))
        s.commit()
        return res.rowcount or 0


def jdump(obj) -> str:
    return json.dumps(obj, default=str)
