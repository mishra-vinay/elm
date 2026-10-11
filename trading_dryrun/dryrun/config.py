"""Settings, read from environment variables (see .env.example). Nothing secret is stored in code."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def _get(name: str, default: str = "") -> str:
    """Environment value with any trailing ' # comment' removed (systemd and some shells keep it)."""
    v = os.environ.get(name)
    return default if v is None else re.split(r"\s+#", v, maxsplit=1)[0].strip()


def _bool(name: str, default: bool) -> bool:
    v = _get(name, "")
    return default if v == "" else v.lower() in ("1", "true", "yes", "on")


def _hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


# Regular sessions per Dhan exchange segment: (open, last entry allowed, square-off, close).
# Square-off is a few minutes before the close so the simulated exit has a real price.
SESSIONS = {
    "IDX_I": (time(9, 15), time(15, 0), time(15, 15), time(15, 30)),
    "NSE_EQ": (time(9, 15), time(15, 0), time(15, 15), time(15, 30)),
    "BSE_EQ": (time(9, 15), time(15, 0), time(15, 15), time(15, 30)),
    "NSE_FNO": (time(9, 15), time(15, 0), time(15, 15), time(15, 30)),
    "BSE_FNO": (time(9, 15), time(15, 0), time(15, 15), time(15, 30)),
    "MCX_COMM": (time(9, 0), time(23, 0), time(23, 15), time(23, 30)),
}


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: _get("DATABASE_URL", "sqlite:///data/dryrun.db"))
    feed_mode: str = field(default_factory=lambda: _get("FEED_MODE", "dhan").lower())   # dhan | sim
    dhan_client_id: str = field(default_factory=lambda: _get("DHAN_CLIENT_ID", ""))
    dhan_access_token: str = field(default_factory=lambda: _get("DHAN_ACCESS_TOKEN", ""))
    feed_packet: str = field(default_factory=lambda: _get("FEED_PACKET", "quote").lower())  # ticker|quote|full
    host: str = field(default_factory=lambda: _get("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_get("PORT", "8000")))
    dashboard_password: str = field(default_factory=lambda: _get("DASHBOARD_PASSWORD", ""))
    secret_key: str = field(default_factory=lambda: _get("SECRET_KEY", ""))
    auth_mode: str = field(default_factory=lambda: _get("AUTH_MODE", "none").lower())  # none|password|email_otp
    brevo_api_key: str = field(default_factory=lambda: _get("BREVO_API_KEY", ""))
    brevo_sender: str = field(default_factory=lambda: _get("BREVO_SENDER_EMAIL", ""))
    allowed_emails: tuple = field(default_factory=lambda: tuple(
        e.strip().lower() for e in _get("ALLOWED_EMAILS", "").split(",") if e.strip()))
    store_ticks: bool = field(default_factory=lambda: _bool("STORE_TICKS", True))
    tick_retention_days: int = field(default_factory=lambda: int(_get("TICK_RETENTION_DAYS", "30")))
    # Simulated feed has no market hours; real feeds are gated by SESSIONS.
    ignore_market_hours: bool = field(default_factory=lambda: _bool("IGNORE_MARKET_HOURS", False))
    sim_ticks_per_second: float = field(default_factory=lambda: float(_get("SIM_TICKS_PER_SECOND", "5")))
    stale_tick_seconds: float = field(default_factory=lambda: float(_get("STALE_TICK_SECONDS", "15")))

    def __post_init__(self):
        if self.feed_mode == "sim":
            self.ignore_market_hours = True
        if self.auth_mode == "none" and self.dashboard_password:
            self.auth_mode = "password"

    @property
    def is_loopback(self) -> bool:
        return self.host in ("127.0.0.1", "localhost", "::1")

    def validate(self) -> None:
        """Refuse unsafe configurations instead of silently running them."""
        if not self.is_loopback and self.auth_mode == "none":
            raise RuntimeError(
                "HOST is not loopback but no login is configured. Set DASHBOARD_PASSWORD (or AUTH_MODE=email_otp), "
                "or keep HOST=127.0.0.1 and use an SSH tunnel.")
        if self.feed_mode == "dhan" and not (self.dhan_client_id and self.dhan_access_token):
            raise RuntimeError("FEED_MODE=dhan needs DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN (or use FEED_MODE=sim).")
        if self.auth_mode == "password" and not self.dashboard_password:
            raise RuntimeError("AUTH_MODE=password needs DASHBOARD_PASSWORD.")
        if self.auth_mode == "email_otp" and not (self.brevo_api_key and self.brevo_sender and self.allowed_emails):
            raise RuntimeError("AUTH_MODE=email_otp needs BREVO_API_KEY, BREVO_SENDER_EMAIL and ALLOWED_EMAILS.")


def session_for(segment: str):
    return SESSIONS.get(segment, SESSIONS["NSE_EQ"])
