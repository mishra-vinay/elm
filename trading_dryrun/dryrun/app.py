"""FastAPI app: dashboard + JSON API. Start with:  python -m dryrun  (see README)."""
from __future__ import annotations

import csv
import io
import logging
import threading
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from . import instruments as instr
from .auth import COOKIE, Auth, OtpService
from .config import Settings
from .db import Candle, Instrument, Run, Signal, Tick, Trade, init_db
from .dhan_feed import DhanFeed
from .dhan_rest import DhanRest
from .history import backfill
from .runner import LevelsNeeded, RunManager, StartError
from .sim_feed import SimFeed
from .strategy import Params

log = logging.getLogger("dryrun.app")
STATIC = Path(__file__).parent / "static"


class ParamsIn(BaseModel):
    pullback_pct: float = 1.0
    target_pct: float = 2.0
    stop_pct: float | None = 1.0
    break_buffer_pct: float = 0.0
    qty: float = 1.0
    slippage_pct: float = 0.02
    cost_pct: float = 0.10
    allow_both_sides: bool = False
    max_trades_per_side: int = Field(1, ge=1, le=10)
    max_trades_per_day: int = Field(2, ge=1, le=50)
    daily_loss_limit_pct: float | None = 2.0


class StartIn(BaseModel):
    instrument_id: int | None = None
    query: str | None = None
    params: ParamsIn = ParamsIn()
    prev_high: float | None = None
    prev_low: float | None = None


class WatchIn(BaseModel):
    instrument_id: int
    on: bool = True


class BackfillIn(BaseModel):
    instrument_id: int
    intraday_days: int = Field(365, ge=0, le=1825)
    interval: int = 1


class PasswordIn(BaseModel):
    password: str


class EmailIn(BaseModel):
    email: str
    code: str | None = None


class TokenIn(BaseModel):
    token: str


def create_app(settings: Settings | None = None, feed_factory=None, load_master: bool = True) -> FastAPI:
    st = settings or Settings()
    st.validate()
    engine, factory = init_db(st.database_url)
    instr.seed_defaults(factory)
    rest = DhanRest(st.dhan_client_id, st.dhan_access_token) if st.dhan_client_id and st.dhan_access_token else None
    auth = Auth(st.auth_mode, st.dashboard_password, st.secret_key)
    otp = OtpService(st.brevo_api_key, st.brevo_sender, st.allowed_emails) if st.auth_mode == "email_otp" else None
    jobs: dict[int, dict] = {}

    def make_feed(on_tick, on_note, on_status):
        if feed_factory is not None:
            return feed_factory(on_tick, on_note, on_status)
        if st.feed_mode == "sim":
            return SimFeed(on_tick, on_note, on_status, ticks_per_second=st.sim_ticks_per_second)
        return DhanFeed(st.dhan_client_id, st.dhan_access_token, on_tick, on_note, on_status, packet=st.feed_packet)

    manager = RunManager(st, factory, make_feed, rest)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await manager.start()
        if load_master and st.feed_mode == "dhan":
            threading.Thread(target=lambda: _safe_refresh(factory), daemon=True).start()
        yield
        await manager.stop()

    app = FastAPI(title="Dry-run trading", lifespan=lifespan)
    app.state.manager, app.state.factory, app.state.settings, app.state.auth = manager, factory, st, auth

    def _safe_refresh(f):
        try:
            n = instr.refresh_master(f)
            log.info("instrument master loaded, %s new rows", n)
        except Exception as e:      # noqa: BLE001
            log.warning("instrument master download failed: %s", e)

    def need_auth(request: Request):
        if auth.enabled and not auth.verify(request.cookies.get(COOKIE)):
            raise HTTPException(401, "login required")

    def inst_dict(i: Instrument) -> dict:
        return {"id": i.id, "segment": i.segment, "security_id": i.security_id, "instrument": i.instrument,
                "symbol": i.symbol, "name": i.name, "lot_size": i.lot_size, "expiry": i.expiry, "label": i.label}

    # ------------------------------------------------------------------ open routes
    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/auth/status")
    def auth_status(request: Request):
        return {"mode": auth.mode, "authenticated": (not auth.enabled) or auth.verify(request.cookies.get(COOKIE))}

    def _set_cookie(resp: Response, request: Request):
        https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        resp.set_cookie(COOKIE, auth.issue(), httponly=True, samesite="strict", max_age=12 * 3600, secure=https)

    @app.post("/api/auth/login")
    def login(body: PasswordIn, request: Request, response: Response):
        if auth.mode != "password":
            raise HTTPException(400, "password login is not enabled")
        try:
            ok = auth.check_password(request.client.host if request.client else "?", body.password)
        except PermissionError as e:
            raise HTTPException(429, str(e))
        if not ok:
            raise HTTPException(401, "wrong password")
        _set_cookie(response, request)
        return {"ok": True}

    @app.post("/api/auth/otp/request")
    def otp_request(body: EmailIn):
        if otp is None:
            raise HTTPException(400, "email OTP is not enabled")
        try:
            otp.request(body.email)
        except Exception as e:      # noqa: BLE001
            log.warning("OTP e-mail failed: %s", e)
            raise HTTPException(502, "could not send the code")
        return {"ok": True}        # same answer for unknown addresses

    @app.post("/api/auth/otp/verify")
    def otp_verify(body: EmailIn, request: Request, response: Response):
        if otp is None or not body.code or not otp.verify(body.email, body.code):
            raise HTTPException(401, "invalid or expired code")
        _set_cookie(response, request)
        return {"ok": True}

    # ------------------------------------------------------------------ protected routes
    @app.get("/api/state", dependencies=[Depends(need_auth)])
    def state():
        snap = manager.snapshot()
        with factory() as s:
            recent = s.scalars(select(Run).order_by(Run.id.desc()).limit(30)).all()
            snap["history"] = [run_row(r) for r in recent if r.id not in manager.runs]
        snap["rest_configured"] = rest is not None
        snap["version"] = "0.1.0"
        return snap

    def run_row(r: Run) -> dict:
        sm = r.summary or {}
        return {"id": r.id, "date": r.trading_date, "instrument": r.instrument.label if r.instrument else r.instrument_id,
                "status": r.status, "ran_ok": r.ran_ok, "ticks": r.ticks_received, "trades": sm.get("trades"),
                "win_rate": sm.get("win_rate"), "net_pnl": sm.get("net_pnl"), "net_pct_sum": sm.get("net_pct_sum"),
                "coverage": sm.get("data_coverage"), "error": r.error, "high": r.prev_high, "low": r.prev_low,
                "levels_source": r.levels_source}

    @app.get("/api/instruments/search", dependencies=[Depends(need_auth)])
    def search(q: str = ""):
        return [inst_dict(i) for i in instr.search(factory, q)]

    @app.post("/api/instruments/refresh", dependencies=[Depends(need_auth)])
    def refresh():
        threading.Thread(target=lambda: _safe_refresh(factory), daemon=True).start()
        return {"started": True}

    @app.post("/api/runs", dependencies=[Depends(need_auth)])
    async def start_run(body: StartIn):
        inst = None
        with factory() as s:
            if body.instrument_id:
                inst = s.get(Instrument, body.instrument_id)
        if inst is None and body.query:
            hits = instr.search(factory, body.query, 2)
            if len(hits) == 1:
                inst = hits[0]
            elif len(hits) > 1:
                raise HTTPException(400, f"'{body.query}' matches several instruments; pick one from the list.")
        if inst is None:
            raise HTTPException(404, "instrument not found")
        try:
            params = Params.from_dict(body.params.model_dump())
        except ValueError as e:
            raise HTTPException(400, str(e))
        manual = None
        if body.prev_high is not None or body.prev_low is not None:
            if body.prev_high is None or body.prev_low is None or not (body.prev_high > body.prev_low > 0):
                raise HTTPException(400, "enter both previous-day High and Low, with High greater than Low")
            manual = (body.prev_high, body.prev_low)
        try:
            return await manager.start_run(inst, params, manual)
        except LevelsNeeded as e:
            return JSONResponse(status_code=409, content={"code": "levels_needed", "detail": str(e), "instrument": inst_dict(inst)})
        except StartError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/runs/{run_id}/stop", dependencies=[Depends(need_auth)])
    def stop_run(run_id: int):
        if not manager.stop_run(run_id):
            raise HTTPException(404, "run is not active")
        manager.writer.flush()          # the saved result is readable as soon as this call returns
        return {"ok": True}

    @app.get("/api/runs/{run_id}", dependencies=[Depends(need_auth)])
    def run_detail(run_id: int):
        with factory() as s:
            r = s.get(Run, run_id)
            if r is None:
                raise HTTPException(404, "no such run")
            sigs = s.scalars(select(Signal).where(Signal.run_id == run_id).order_by(Signal.id)).all()
            trs = s.scalars(select(Trade).where(Trade.run_id == run_id).order_by(Trade.id)).all()
            return {**run_row(r), "params": r.params, "summary": r.summary, "started_at": r.started_at, "ended_at": r.ended_at,
                    "signals": [{"ts": x.ts, "kind": x.kind, "side": x.side, "price": x.price, "level": x.level,
                                 "action": x.action, "reason": x.reason} for x in sigs],
                    "trades": [{c.name: getattr(t, c.name) for c in Trade.__table__.columns} for t in trs]}

    @app.get("/api/export/trades.csv", dependencies=[Depends(need_auth)])
    def export_trades():
        out = io.StringIO()
        with factory() as s:
            cols = [c.name for c in Trade.__table__.columns]
            w = csv.writer(out)
            w.writerow(["run_date", "instrument"] + cols)
            for t, r in s.execute(select(Trade, Run).join(Run, Run.id == Trade.run_id).order_by(Trade.id)):
                w.writerow([r.trading_date, r.instrument.symbol] + [getattr(t, c) for c in cols])
        return PlainTextResponse(out.getvalue(), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=dryrun_trades.csv"})

    @app.post("/api/watch", dependencies=[Depends(need_auth)])
    async def watch(body: WatchIn):
        with factory() as s:
            inst = s.get(Instrument, body.instrument_id)
        if inst is None:
            raise HTTPException(404, "instrument not found")
        await manager.watch(inst, body.on)
        return {"ok": True}

    @app.post("/api/backfill", dependencies=[Depends(need_auth)])
    def start_backfill(body: BackfillIn):
        if rest is None:
            raise HTTPException(400, "Dhan credentials are not configured; historical download needs the Data API.")
        with factory() as s:
            inst = s.get(Instrument, body.instrument_id)
        if inst is None:
            raise HTTPException(404, "instrument not found")
        if body.interval not in (1, 5, 15, 25, 60):
            raise HTTPException(400, "interval must be 1, 5, 15, 25 or 60")
        job = {"instrument": inst.label, "status": "running", "message": "starting", "result": None}
        jobs[inst.id] = job

        def work():
            try:
                job["result"] = backfill(factory, rest, inst, intraday_days=body.intraday_days, intervals=(body.interval,),
                                         progress=lambda m: job.update(message=m))
                job.update(status="done", message="finished")
            except Exception as e:      # noqa: BLE001
                job.update(status="error", message=str(e)[:300])

        threading.Thread(target=work, daemon=True).start()
        return job

    @app.get("/api/data/summary", dependencies=[Depends(need_auth)])
    def data_summary():
        with factory() as s:
            rows = s.execute(select(Instrument.id, Instrument.symbol, Instrument.segment, Candle.interval, func.count(),
                                    func.min(Candle.ts), func.max(Candle.ts))
                             .join(Candle, Candle.instrument_id == Instrument.id).group_by(Instrument.id, Candle.interval)).all()
            ticks = s.scalar(select(func.count()).select_from(Tick)) or 0
        with factory() as s2:
            n_inst = s2.scalar(select(func.count()).select_from(Instrument)) or 0
        return {"instruments": n_inst, "candles": [{"symbol": r[1], "segment": r[2], "interval": r[3], "count": r[4], "from": r[5], "to": r[6]} for r in rows],
                "ticks": ticks, "jobs": list(jobs.values())}

    @app.post("/api/feed/token", dependencies=[Depends(need_auth)])
    async def set_token(body: TokenIn):
        """Dhan access tokens last 24 h. Paste the new one here instead of restarting; it is kept in memory only."""
        if st.feed_mode != "dhan":
            raise HTTPException(400, "the simulated feed has no token")
        tok = body.token.strip()
        if len(tok) < 20:
            raise HTTPException(400, "that does not look like an access token")
        st.dhan_access_token = tok
        if rest is not None:
            rest.token = tok
        manager.feed.update_token(tok)
        await manager.feed.reconnect()
        return {"ok": True}

    return app
