"""Optional dashboard login.

AUTH_MODE=none        no login (the app then refuses to listen on anything but loopback)
AUTH_MODE=password    one shared password (DASHBOARD_PASSWORD)
AUTH_MODE=email_otp   6-digit code e-mailed through Brevo to an address in ALLOWED_EMAILS

Sessions are HMAC-signed cookies (stdlib only). This is a single-user tool: it is meant to sit behind an SSH tunnel
or an HTTPS reverse proxy, not to be a public login system.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

import requests

COOKIE = "dryrun_session"
SESSION_SECONDS = 12 * 3600


class Auth:
    def __init__(self, mode: str, password: str = "", secret_key: str = ""):
        self.mode = mode
        self.password = password
        self.key = (secret_key or secrets.token_hex(32)).encode()
        self._fails: dict[str, list[float]] = {}

    @property
    def enabled(self) -> bool:
        return self.mode != "none"

    # -- tokens ---------------------------------------------------------------------------
    def issue(self, subject: str = "user") -> str:
        exp = int(time.time()) + SESSION_SECONDS
        body = f"{subject}|{exp}".encode()
        sig = hmac.new(self.key, body, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(body).decode() + "." + base64.urlsafe_b64encode(sig).decode()

    def verify(self, token: str | None) -> bool:
        if not self.enabled:
            return True
        if not token or "." not in token:
            return False
        try:
            b, s = token.split(".", 1)
            body, sig = base64.urlsafe_b64decode(b), base64.urlsafe_b64decode(s)
            if not hmac.compare_digest(hmac.new(self.key, body, hashlib.sha256).digest(), sig):
                return False
            return int(body.decode().split("|")[1]) > time.time()
        except Exception:      # noqa: BLE001
            return False

    # -- password -------------------------------------------------------------------------
    def check_password(self, client: str, attempt: str) -> bool:
        now = time.time()
        recent = [t for t in self._fails.get(client, []) if now - t < 300]
        if len(recent) >= 5:
            raise PermissionError("too many attempts; wait 5 minutes")
        ok = bool(self.password) and hmac.compare_digest(attempt.encode(), self.password.encode())
        if not ok:
            recent.append(now)
        self._fails[client] = recent
        return ok


class OtpService:
    """Generates and verifies one-time codes; sending goes through Brevo's transactional e-mail API."""

    def __init__(self, api_key: str, sender: str, allowed: tuple[str, ...], http=requests, ttl: int = 300, max_tries: int = 5):
        self.api_key, self.sender, self.allowed, self.http, self.ttl, self.max_tries = api_key, sender, set(allowed), http, ttl, max_tries
        self._codes: dict[str, tuple[str, float, int]] = {}

    def request(self, email: str) -> bool:
        email = email.strip().lower()
        if email not in self.allowed:
            return False                       # the caller answers identically either way (no account probing)
        code = f"{secrets.randbelow(10 ** 6):06d}"
        self._codes[email] = (hashlib.sha256(code.encode()).hexdigest(), time.time() + self.ttl, 0)
        r = self.http.post("https://api.brevo.com/v3/smtp/email", timeout=15,
                           headers={"api-key": self.api_key, "content-type": "application/json", "accept": "application/json"},
                           json={"sender": {"email": self.sender, "name": "Dry-run dashboard"}, "to": [{"email": email}],
                                 "subject": "Your login code",
                                 "htmlContent": f"<p>Your code is <b>{code}</b>. It expires in {self.ttl // 60} minutes.</p>"})
        r.raise_for_status()
        return True

    def verify(self, email: str, code: str) -> bool:
        email = email.strip().lower()
        item = self._codes.get(email)
        if not item:
            return False
        digest, exp, tries = item
        if time.time() > exp or tries >= self.max_tries:
            self._codes.pop(email, None)
            return False
        if hmac.compare_digest(hashlib.sha256(code.strip().encode()).hexdigest(), digest):
            self._codes.pop(email, None)
            return True
        self._codes[email] = (digest, exp, tries + 1)
        return False
