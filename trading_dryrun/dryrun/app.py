"""FastAPI app: dashboard + JSON API. Start with:  python -m dryrun  (see README)."""
from __future__ import annotations

import base64
import importlib
import csv
import io
import json
import logging
import os
import threading
from datetime import date, datetime, timedelta, timezone
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import delete, distinct, func, select
from sqlalchemy.orm import joinedload

from . import instruments as instr
from . import levels as levels_mod
from .auth import COOKIE, Auth, OtpService
from .config import IST, SESSIONS, Settings
from .db import ApiLog, Candle, Instrument, Run, Signal, Tick, Trade, Watch, delete_ticks, init_db, set_setting
from .dhan_feed import DhanFeed
from .depth_service import BUCKETS, DepthService
from .backtests import BacktestError, BacktestJobs, day_view
from .dhan_rest import DhanRest
from .history import backfill
from .runner import LevelsNeeded, RunManager, StartError
from .sim_feed import SimFeed
from .strategy import STRATEGIES, NoStrategyError, Params, get_strategy

importlib.import_module(f"{__package__}.strategies")      # importing the package registers the installed strategies (none yet)
log = logging.getLogger("dryrun.app")
STATIC = Path(__file__).parent / "static"


class ParamsIn(BaseModel):
    strategy: str = ""
    options: dict = {}
    qty: float = 1.0
    slippage_pct: float = 0.02
    cost_pct: float = 0.10
    allow_both_sides: bool = False
    max_trades_per_side: int = Field(1, ge=1, le=10)
    max_trades_per_day: int = Field(2, ge=1, le=50)
    daily_loss_limit_pct: float | None = 2.0


class PreviewIn(BaseModel):
    high: float
    low: float
    options: dict = {}


class StartIn(BaseModel):
    instrument_id: int | None = None
    query: str | None = None
    label: str | None = Field(None, max_length=40)
    params: ParamsIn = ParamsIn()
    prev_high: float | None = None
    prev_low: float | None = None


class RetentionIn(BaseModel):
    retention_days: int = Field(ge=1, le=365)


class BacktestIn(BaseModel):
    instrument_id: int
    interval: str
    date_from: date | None = None
    date_to: date | None = None
    min_bars_pct: float = Field(80.0, ge=0, le=100)
    capital: float = Field(100_000.0, gt=0)          # starting capital the rupee result is shown against
    params: ParamsIn = ParamsIn()


class WatchIn(BaseModel):
    instrument_id: int
    on: bool = True


class BackfillIn(BaseModel):
    instrument_id: int
    intraday_days: int = Field(365, ge=0, le=1825)
    interval: int = 1
    keep_updated: bool = True          # also refresh this instrument every weekday after the close


class PasswordIn(BaseModel):
    password: str


class EmailIn(BaseModel):
    email: str
    code: str | None = None


class TokenIn(BaseModel):
    token: str
    save: bool = False        # also write it to the server's .env so it survives a restart


def token_info(token: str) -> dict:
    """Expiry of a Dhan access token, read from the token itself (a JWT). The token value is never returned."""
    if not token:
        return {"set": False}
    info = {"set": True, "expires_at": None, "seconds_left": None, "status": "unknown"}
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        if exp:
            left = int(exp - datetime.now(timezone.utc).timestamp())
            info.update(expires_at=datetime.fromtimestamp(exp, IST).strftime("%Y-%m-%d %H:%M:%S"), seconds_left=left,
                        status="expired" if left <= 0 else "expiring soon" if left < 3 * 3600 else "valid")
    except Exception:      # noqa: BLE001  (not a JWT or unreadable: say so rather than guess)
        pass
    return info


def save_env_value(key: str, value: str, path: str | None = None) -> None:
    """Replace (or append) one KEY=value line in the .env file, keeping every other line as it is."""
    from pathlib import Path
    p = Path(path or os.environ.get("DRYRUN_ENV_FILE", ".env"))
    if not p.exists():
        raise FileNotFoundError(f"{p.resolve()} does not exist")
    lines, done = p.read_text().splitlines(), False
    for n, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[n], done = f"{key}={value}", True
    if not done:
        lines.append(f"{key}={value}")
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def create_app(settings: Settings | None = None, feed_factory=None, load_master: bool = True, clock=None) -> FastAPI:
    st = settings or Settings()
    st.validate()
    engine, factory = init_db(st.database_url)
    instr.seed_defaults(factory)
    rest = DhanRest(st.dhan_client_id, st.dhan_access_token) if st.dhan_client_id and st.dhan_access_token else None
    auth = Auth(st.auth_mode, st.dashboard_password, st.secret_key)
    otp = OtpService(st.brevo_api_key, st.brevo_sender, st.allowed_emails) if st.auth_mode == "email_otp" else None
    jobs: dict[int, dict] = {}
    backtests = BacktestJobs(factory)
    summary_cache: dict = {"at": 0.0, "data": None}

    def make_feed(on_tick, on_note, on_status):
        if feed_factory is not None:
            return feed_factory(on_tick, on_note, on_status)
        if st.feed_mode == "sim":
            return SimFeed(on_tick, on_note, on_status, ticks_per_second=st.sim_ticks_per_second)
        return DhanFeed(st.dhan_client_id, st.dhan_access_token, on_tick, on_note, on_status, packet=st.feed_packet)

    manager = RunManager(st, factory, make_feed, rest, **({"clock": clock} if clock else {}))

    def _depth_default():
        with factory() as s:
            i = s.scalar(select(Instrument).where(Instrument.symbol == "NATGASMINI", Instrument.instrument == "FUTCOM",
                                                  Instrument.expiry >= date.today()).order_by(Instrument.expiry).limit(1))
            return (i.segment, i.security_id, i.name or i.symbol) if i else None

    _url = st.database_url
    depth_path = str(Path(_url.split("sqlite:///", 1)[1]).parent / "depth.db") if _url.startswith("sqlite:///") else "data/depth.db"
    depth = DepthService(st, depth_path, _depth_default) if (st.feed_mode == "dhan" and feed_factory is None) else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await manager.start()
        if depth:
            await depth.start()
        if load_master and st.feed_mode == "dhan":
            threading.Thread(target=lambda: _safe_refresh(factory), daemon=True).start()
        yield
        if depth:
            await depth.stop()
        await manager.stop()

    app = FastAPI(title="Dry-run trading", lifespan=lifespan)
    app.add_middleware(GZipMiddleware, minimum_size=1000)           # JSON and scripts shrink about 5x, which matters over the internet
    app.state.manager, app.state.factory, app.state.settings, app.state.auth = manager, factory, st, auth

    def _safe_refresh(f, force: bool = False):
        try:
            if not force and instr.refreshed_recently(f, hours=20):
                log.info("instrument list is fresh, skipping the download")
                return
            manager.activity("data", "info", "Downloading Dhan's instrument list")
            n = instr.refresh_master(f)
            log.info("instrument master loaded, %s new rows", n)
            manager.activity("data", "info", f"Instrument list loaded ({n} new instruments)")
        except Exception as e:      # noqa: BLE001
            log.warning("instrument master download failed: %s", e)
            manager.activity("data", "error", f"Instrument list download failed: {str(e)[:200]}")

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

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    @app.get("/runs")
    def page_runs():
        return FileResponse(STATIC / "runs.html")

    @app.get("/market-data")
    def page_market():
        return FileResponse(STATIC / "market.html")

    @app.get("/depth")
    def page_depth():
        return FileResponse(STATIC / "depth.html")

    @app.get("/ticks")
    def page_ticks():
        return FileResponse(STATIC / "ticks.html")

    @app.get("/backtest")
    def page_backtest():
        return FileResponse(STATIC / "backtest.html")

    @app.get("/strategies")
    def page_strategies():
        return FileResponse(STATIC / "strategies.html")

    @app.get("/settings")
    def page_settings():
        return FileResponse(STATIC / "settings.html")

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
    def params_from(body_params: ParamsIn) -> Params:
        """Validated Params; when exactly one strategy is installed a request that names none uses it."""
        d = body_params.model_dump()
        listed = [k for k, v in STRATEGIES.items() if not v.hidden]
        if not d["strategy"] and len(listed) == 1:
            d["strategy"] = listed[0]
        return Params.from_dict(d)

    @app.get("/api/strategies", dependencies=[Depends(need_auth)])
    def list_strategies():
        """Installed strategies and their settings forms. Empty until a strategy is added in dryrun/strategies/."""
        return {"strategies": [x.public() for x in STRATEGIES.values() if not x.hidden],
                "message": None if STRATEGIES else "No strategy is installed yet. Add one in dryrun/strategies/ (see the README)."}

    @app.post("/api/strategies/{name}/preview", dependencies=[Depends(need_auth)])
    def strategy_preview(name: str, body: PreviewIn):
        """The prices a strategy would use for the given previous-day High/Low and settings (for the calculation panel)."""
        try:
            spec = get_strategy(name)
        except NoStrategyError as e:
            raise HTTPException(404, str(e))
        if spec.preview is None:
            return {"summary": None, "cards": [], "notes": []}
        if not (body.high > body.low > 0):
            raise HTTPException(400, "High must be greater than Low and both positive")
        try:
            return spec.preview(body.high, body.low, body.options) or {"summary": None, "cards": [], "notes": []}
        except ValueError as e:
            return {"summary": None, "cards": [], "notes": [], "error": str(e)}

    @app.get("/api/state", dependencies=[Depends(need_auth)])
    def state():
        snap = manager.snapshot()
        with factory() as s:
            recent = s.scalars(select(Run).options(joinedload(Run.instrument)).order_by(Run.id.desc()).limit(30)).all()   # one query, not 31
            snap["history"] = [run_row(r) for r in recent if r.id not in manager.runs]
        snap["rest_configured"] = manager.rest is not None
        snap["version"] = "0.1.0"
        return snap

    @app.get("/api/status", dependencies=[Depends(need_auth)])
    def status():
        """Small payload for the header badges on every page."""
        return manager.status_snapshot()

    @app.get("/api/settings", dependencies=[Depends(need_auth)])
    def get_settings():
        cid = st.dhan_client_id
        env_path = os.environ.get("DRYRUN_ENV_FILE", ".env")
        return {"mode": st.feed_mode, "client_id": ("*" * max(0, len(cid) - 4) + cid[-4:]) if cid else "",
                "token": token_info(st.dhan_access_token), "data_api_checked": manager.rest is not None,
                "feed_packet": st.feed_packet, "store_ticks": st.store_ticks, "tick_retention_days": manager.retention_days(),
                "stale_tick_seconds": st.stale_tick_seconds, "auth_mode": st.auth_mode,
                "database": st.database_url.split(":", 1)[0].split("+", 1)[0], "env_file_present": os.path.exists(env_path),
                "sessions": {k: {"open": f"{v[0]:%H:%M}", "last_entry": f"{v[1]:%H:%M}", "square_off": f"{v[2]:%H:%M}", "close": f"{v[3]:%H:%M}"}
                             for k, v in SESSIONS.items()},
                "version": "0.1.0"}

    @app.post("/api/settings/test", dependencies=[Depends(need_auth)])
    def test_dhan():
        """Ask Dhan for the profile: proves the token works and shows whether the Data API plan is active."""
        if manager.rest is None:
            raise HTTPException(400, "Dhan credentials are not configured")
        try:
            prof = manager.rest.profile()
        except Exception as e:      # noqa: BLE001
            manager.activity("app", "error", f"Dhan connection test failed: {str(e)[:200]}")
            raise HTTPException(502, str(e)[:300])
        keep = ("tokenValidity", "activeSegment", "dataPlan", "dataValidity", "ddpi", "mtf")
        out = {k: prof[k] for k in keep if k in prof}
        manager.activity("app", "info", f"Dhan connection test OK (data plan: {out.get('dataPlan', 'not reported')})")
        return out

    LEGACY = {"pullback": ("pullback_pct", "target_pct", "stop_pct"), "thirds": ("entry_mode", "entry_frac", "stop_frac", "target_frac", "stop_side"), "": ("pullback_pct", "target_pct", "stop_pct")}

    def run_settings(p: dict) -> dict:
        """Strategy name and options of a stored run. Runs made before strategies became pluggable kept their settings at the top level of the
        params and used strategies that have since been retired; they are shown as such."""
        name = p.get("strategy") or ""
        if p.get("options") or name not in LEGACY:
            return {"strategy": name or "–", "options": p.get("options") or {}}
        return {"strategy": (name or "pullback") + " (retired)", "options": {k: p[k] for k in LEGACY[name] if p.get(k) is not None}}

    def run_row(r: Run) -> dict:
        sm = r.summary or {}
        return {"id": r.id, "label": (r.params or {}).get("label", ""), "settings": run_settings(r.params or {}), "date": r.trading_date, "instrument": r.instrument.label if r.instrument else r.instrument_id,
                "status": r.status, "ran_ok": r.ran_ok, "ticks": r.ticks_received, "trades": sm.get("trades"),
                "win_rate": sm.get("win_rate"), "net_pnl": sm.get("net_pnl"), "net_pct_sum": sm.get("net_pct_sum"),
                "coverage": sm.get("data_coverage"), "error": r.error, "high": r.prev_high, "low": r.prev_low,
                "levels_source": r.levels_source}

    @app.get("/api/instruments/search", dependencies=[Depends(need_auth)])
    def search(q: str = ""):
        return [inst_dict(i) for i in instr.search(factory, q)]

    @app.get("/api/levels", dependencies=[Depends(need_auth)])
    def preview_levels(instrument_id: int):
        """What previous-day High/Low a run started now would use (so the form can fill itself in)."""
        with factory() as s:
            inst = s.get(Instrument, instrument_id)
        if inst is None:
            raise HTTPException(404, "instrument not found")
        trading_date = manager.next_session_date(manager.clock(), inst.segment)
        lv = levels_mod.resolve(factory, inst, trading_date, manager.rest)
        if lv is None:
            return {"found": False, "trading_date": trading_date.isoformat(),
                    "detail": "Not found in the database" + ("" if manager.rest else " (no Dhan credentials to fetch it)")
                              + ". Download history or type the High/Low."}
        return {"found": True, "high": lv.high, "low": lv.low, "source": lv.source,
                "from_date": lv.from_date.isoformat() if lv.from_date else None, "trading_date": trading_date.isoformat()}

    @app.get("/api/activity", dependencies=[Depends(need_auth)])
    def activity(source: str | None = None, limit: int = 200):
        q = select(ApiLog).order_by(ApiLog.id.desc()).limit(min(max(limit, 1), 1000))
        if source and source != "all":
            q = q.where(ApiLog.level == "error") if source == "errors" else q.where(ApiLog.source == source)
        with factory() as s:
            return [{"id": r.id, "ts": r.ts, "source": r.source, "level": r.level, "message": r.message} for r in s.scalars(q)]

    @app.post("/api/instruments/refresh", dependencies=[Depends(need_auth)])
    def refresh():
        threading.Thread(target=lambda: _safe_refresh(factory, force=True), daemon=True).start()
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
            params = params_from(body.params)
        except ValueError as e:
            raise HTTPException(400, str(e))
        manual = None
        if body.prev_high is not None or body.prev_low is not None:
            if body.prev_high is None or body.prev_low is None or not (body.prev_high > body.prev_low > 0):
                raise HTTPException(400, "enter both previous-day High and Low, with High greater than Low")
            manual = (body.prev_high, body.prev_low)
        try:
            return await manager.start_run(inst, params, manual, (body.label or "").strip() or None)
        except LevelsNeeded as e:
            return JSONResponse(status_code=409, content={"code": "levels_needed", "detail": str(e), "instrument": inst_dict(inst)})
        except StartError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/runs/stop-all", dependencies=[Depends(need_auth)])
    def stop_all():
        n = manager.stop_all()
        manager.writer.flush()
        return {"stopped": n}

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
        summary_cache["data"] = None
        return {"ok": True}

    @app.post("/api/backfill", dependencies=[Depends(need_auth)])
    def start_backfill(body: BackfillIn):
        if manager.rest is None:
            raise HTTPException(400, "Dhan credentials are not configured; historical download needs the Data API.")
        with factory() as s:
            inst = s.get(Instrument, body.instrument_id)
        if inst is None:
            raise HTTPException(404, "instrument not found")
        if body.interval not in (1, 5, 15, 25, 60):
            raise HTTPException(400, "interval must be 1, 5, 15, 25 or 60")
        job = {"instrument": inst.label, "status": "running", "message": "starting", "result": None}
        jobs[inst.id] = job
        if body.keep_updated:
            manager.set_watch_flags(inst.id, daily_update=True)

        manager.activity("data", "info", f"History download started: {inst.label}, {body.interval}-minute, {body.intraday_days} days")

        def work():
            try:
                job["result"] = backfill(factory, manager.rest, inst, intraday_days=body.intraday_days, intervals=(body.interval,),
                                         progress=lambda m: job.update(message=m))
                job.update(status="done", message="finished")
                summary_cache["data"] = None
                manager.activity("data", "info", f"History download finished: {inst.symbol}, added {job['result']['daily']} daily and {job['result']['intraday']} intraday candles")
            except Exception as e:      # noqa: BLE001
                job.update(status="error", message=str(e)[:300])
                manager.activity("data", "error", f"History download failed for {inst.symbol}: {str(e)[:300]}")

        threading.Thread(target=work, daemon=True).start()
        return job

    @app.delete("/api/candles", dependencies=[Depends(need_auth)])
    def delete_candles(instrument_id: int, interval: str | None = None):
        """Delete stored candles for one instrument (all intervals, or just one). Runs, trades and signals are untouched."""
        if interval is not None and interval not in ("D", "1m", "5m", "15m", "25m", "60m"):
            raise HTTPException(400, "unknown interval")
        q = delete(Candle).where(Candle.instrument_id == instrument_id)
        if interval is not None:
            q = q.where(Candle.interval == interval)
        with factory() as s:
            n = s.execute(q).rowcount or 0
            s.commit()
        summary_cache["data"] = None
        return {"deleted": n}

    @app.post("/api/update-now", dependencies=[Depends(need_auth)])
    def update_now():
        if manager.rest is None:
            raise HTTPException(400, "Dhan credentials are not configured")
        manager.activity("data", "info", "Update of stored history started (manual)")
        threading.Thread(target=manager.update_history_now, daemon=True).start()
        return {"started": True}

    @app.get("/api/data/summary", dependencies=[Depends(need_auth)])
    def data_summary():
        """Counting distinct days over every stored candle is the slowest query in the app, so the result is reused for 20 s
        (and dropped at once after a download or delete)."""
        import time as _t
        if summary_cache["data"] is not None and _t.time() - summary_cache["at"] < 20:
            return {**summary_cache["data"], "jobs": list(jobs.values())}
        data = _summary()
        summary_cache.update(at=_t.time(), data=data)
        return {**data, "jobs": list(jobs.values())}

    def _summary():
        with factory() as s:
            rows = s.execute(select(Instrument.id, Instrument.symbol, Instrument.segment, Candle.interval, func.count(),
                                    func.min(Candle.ts), func.max(Candle.ts), func.count(distinct(func.date(Candle.ts))), Instrument.name)
                             .join(Candle, Candle.instrument_id == Instrument.id).group_by(Instrument.id, Candle.interval)).all()
            ticks = s.scalar(select(func.count()).select_from(Tick)) or 0
            n_inst = s.scalar(select(func.count()).select_from(Instrument)) or 0
            watch = [{"instrument": i.label, "collect_live": w.collect_live, "daily_update": w.daily_update}
                     for w, i in s.execute(select(Watch, Instrument).join(Instrument, Instrument.id == Watch.instrument_id))]
        return {"instruments": n_inst, "ticks": ticks, "watchlist": watch,
                "candles": [{"instrument_id": r[0], "symbol": r[1], "segment": r[2], "interval": r[3], "count": r[4], "from": r[5],
                             "to": r[6], "days": r[7], "per_day": round(r[4] / r[7], 1) if r[7] else None, "name": r[8]} for r in rows]}


    # ------------------------------------------------------------------ live tick monitor and stored-tick management
    ticks_today_cache: dict = {"at": 0.0, "data": {}}

    @app.get("/api/ticks", dependencies=[Depends(need_auth)])
    def live_ticks():
        """Feed health and per-instrument tick quality for the Ticks page (in-memory counters, cheap)."""
        import time as _t
        snap = manager.ticks_snapshot()
        if _t.time() - ticks_today_cache["at"] > 15:
            today = datetime.combine(manager.clock().date(), datetime.min.time())
            with factory() as s:
                rows = s.execute(select(Tick.instrument_id, func.count()).where(Tick.ts >= today).group_by(Tick.instrument_id)).all()
            ticks_today_cache.update(at=_t.time(), data={r[0]: r[1] for r in rows})
        with factory() as s:
            ids = {f"{i.segment}:{i.security_id}": i.id for i in s.scalars(select(Instrument).where(
                Instrument.id.in_(list({r.inst.id for r in manager.runs.values()} | {x.inst.id for x in manager.states.values()}))))}
        for r in snap["instruments"]:
            r["stored_today"] = ticks_today_cache["data"].get(ids.get(r["key"]), 0)
        return snap

    @app.post("/api/settings/ticks", dependencies=[Depends(need_auth)])
    def set_tick_retention(body: RetentionIn):
        set_setting(factory, "tick_retention_days", str(body.retention_days))
        manager.activity("app", "info", f"Raw tick retention set to {body.retention_days} day(s) from the dashboard")
        return {"retention_days": manager.retention_days()}

    @app.get("/api/ticks/days", dependencies=[Depends(need_auth)])
    def tick_days():
        """Stored raw ticks per day and instrument, so a day can be downloaded and then deleted."""
        with factory() as s:
            rows = s.execute(select(func.date(Tick.ts), Instrument.id, Instrument.symbol, Instrument.segment, func.count(),
                                    func.min(Tick.ts), func.max(Tick.ts))
                             .join(Instrument, Instrument.id == Tick.instrument_id)
                             .group_by(func.date(Tick.ts), Instrument.id).order_by(func.date(Tick.ts).desc(), Instrument.symbol)).all()
        return {"retention_days": manager.retention_days(), "store_ticks": st.store_ticks,
                "days": [{"date": r[0], "instrument_id": r[1], "symbol": r[2], "segment": r[3], "rows": r[4], "first": r[5], "last": r[6]} for r in rows]}

    @app.get("/api/ticks/export.csv", dependencies=[Depends(need_auth)])
    def export_ticks(day: date, instrument_id: int | None = None):
        start = datetime.combine(day, datetime.min.time())
        q = (select(Tick.ts, Instrument.symbol, Tick.ltp, Tick.ltt, Tick.volume, Tick.day_high, Tick.day_low)
             .join(Instrument, Instrument.id == Tick.instrument_id)
             .where(Tick.ts >= start, Tick.ts < start + timedelta(days=1)).order_by(Tick.instrument_id, Tick.id))
        if instrument_id is not None:
            q = q.where(Tick.instrument_id == instrument_id)

        def gen():
            yield "ts,symbol,ltp,exchange_time_raw,cumulative_volume,day_high,day_low\n"
            with factory() as s:
                for r in s.execute(q.execution_options(yield_per=5000)):
                    yield ",".join("" if v is None else str(v) for v in r) + "\n"
        name = f"ticks_{day.isoformat()}" + (f"_{instrument_id}" if instrument_id is not None else "") + ".csv"
        return StreamingResponse(gen(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.delete("/api/ticks", dependencies=[Depends(need_auth)])
    def delete_stored_ticks(day: date | None = None, instrument_id: int | None = None, older_than_days: int | None = None):
        """Delete one stored day (optionally one instrument) or everything older than N days. Candles, runs and trades are kept."""
        if day is None and older_than_days is None:
            raise HTTPException(400, "give a day or older_than_days")
        before = None
        if older_than_days is not None:
            before = manager.clock() - timedelta(days=older_than_days)
        n = delete_ticks(factory, before=before, day=day, instrument_id=instrument_id)
        summary_cache["data"] = None
        manager.activity("data", "info", f"Deleted {n:,} stored ticks" + (f" for {day}" if day else f" older than {older_than_days} day(s)"))
        return {"deleted": n}

    # ------------------------------------------------------------------ online backtests
    @app.post("/api/backtest", dependencies=[Depends(need_auth)])
    def start_backtest(body: BacktestIn):
        """Run the live engine over a stored dataset in the background; poll /api/backtest/{id}."""
        try:
            params = params_from(body.params)
        except ValueError as e:
            raise HTTPException(400, str(e))
        with factory() as s:
            inst = s.get(Instrument, body.instrument_id)
        if inst is None:
            raise HTTPException(404, "instrument not found")
        try:
            jid = backtests.start(body.instrument_id, body.interval, params, body.date_from, body.date_to, body.min_bars_pct,
                                  f"{inst.symbol} {body.interval} · {params.strategy}", body.capital)
        except BacktestError as e:
            raise HTTPException(409, str(e))
        manager.activity("app", "info", f"Backtest started: {inst.label} {body.interval}, strategy {params.strategy}")
        return {"id": jid}

    @app.get("/api/backtest/{jid}", dependencies=[Depends(need_auth)])
    def get_backtest(jid: str):
        job = backtests.get(jid)
        if job is None:
            raise HTTPException(404, "no such backtest (results are kept in memory and lost on restart)")
        return job

    @app.get("/api/backtest/{jid}/day", dependencies=[Depends(need_auth)])
    def get_backtest_day(jid: str, day: date = Query(..., alias="date")):
        """Candles, strategy lines and trades of one tested session of a finished backtest (the day chart)."""
        job = backtests.get(jid)
        if job is None:
            raise HTTPException(404, "no such backtest (results are kept in memory and lost on restart)")
        if job["result"] is None:
            raise HTTPException(409, "the backtest has not finished")
        try:
            return day_view(factory, job["result"], day)
        except BacktestError as e:
            raise HTTPException(404, str(e))


    # ------------------------------------------------------------------ market depth recorder
    def need_depth():
        if depth is None:
            raise HTTPException(400, "the depth recorder runs only with the live Dhan feed")
        return depth

    class DepthTargetIn(BaseModel):
        segment: str
        security_id: str
        label: str = ""
        on: bool = True

    @app.get("/api/depth/status", dependencies=[Depends(need_auth)])
    def depth_status():
        return need_depth().status()

    @app.get("/api/depth/latest", dependencies=[Depends(need_auth)])
    def depth_latest():
        from .depth_service import row_json
        d = need_depth()
        return {sid: row_json(r) for sid, r in list(d.latest.items())}

    @app.post("/api/depth/targets", dependencies=[Depends(need_auth)])
    def depth_set_target(body: DepthTargetIn):
        d = need_depth()
        d.set_target(body.segment.upper(), body.security_id.strip(), body.label or body.security_id, body.on)
        manager.activity("data", "info", f"Depth recorder: {body.label or body.security_id} {'on' if body.on else 'paused'}")
        return {"ok": True, "targets": d.targets()}

    @app.delete("/api/depth/targets", dependencies=[Depends(need_auth)])
    def depth_del_target(segment: str, security_id: str):
        d = need_depth()
        d.remove_target(segment.upper(), security_id)
        return {"ok": True, "targets": d.targets()}

    @app.get("/api/depth/days", dependencies=[Depends(need_auth)])
    def depth_days(security_id: str):
        return need_depth().days(security_id)

    @app.get("/api/depth/series", dependencies=[Depends(need_auth)])
    def depth_series(security_id: str, day: date, bucket: int = 30, since_ms: int = 0):
        if bucket not in BUCKETS:
            raise HTTPException(400, f"bucket must be one of {list(BUCKETS)}")
        return need_depth().series(security_id, day.isoformat(), bucket, since_ms)

    @app.get("/api/depth/export.csv", dependencies=[Depends(need_auth)])
    def depth_export(security_id: str, day: date):
        con, cur, header = need_depth().export_rows(security_id, day.isoformat())

        def gen():
            try:
                yield ",".join(header) + "\n"
                while True:
                    rows = cur.fetchmany(5000)
                    if not rows:
                        break
                    for r in rows:
                        yield ",".join("" if v is None else str(v) for v in r) + "\n"
            finally:
                con.close()
        return StreamingResponse(gen(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="depth_{security_id}_{day.isoformat()}.csv"'})

    @app.post("/api/feed/token", dependencies=[Depends(need_auth)])
    async def set_token(body: TokenIn):
        """Dhan access tokens last 24 h. Paste the new one here instead of restarting; it is kept in memory only."""
        if st.feed_mode != "dhan":
            raise HTTPException(400, "the simulated feed has no token")
        tok = body.token.strip()
        if len(tok) < 20:
            raise HTTPException(400, "that does not look like an access token")
        st.dhan_access_token = tok
        if manager.rest is not None:
            manager.rest.token = tok
        saved = None
        if body.save:
            try:
                save_env_value("DHAN_ACCESS_TOKEN", tok)
                saved = True
            except Exception as e:      # noqa: BLE001
                saved = False
                manager.activity("app", "warn", f"Token is active but could not be saved to .env: {e}")
        manager.activity("app", "info", "Dhan access token updated from the dashboard" + (" and saved to .env" if saved else "") + "; reconnecting the feed")
        manager.feed.update_token(tok)
        await manager.feed.reconnect()
        if depth:
            depth.poke()
        return {"ok": True, "saved_to_env": saved, "token": token_info(tok)}

    return app
