"""Email access gate for the public demo (TDR_ACCESS_GATE=on).

Same flow as the gated downloads on the owner's website: the visitor gives a name, email and (optional) company,
proves the email is real by typing a 6-digit code sent to it, and a signed cookie then remembers them for 30 days.
Each confirmed visitor is logged to .data/leads.jsonl (git-ignored, owner-only) and the owner gets an email.

With the gate on, only the landing page ("How it works"), /health, the four read-only lists it shows as stats and
the /access/* endpoints are public. Everything else (the live demo, the APIs it calls, /docs, /lab) needs the
cookie. With the gate off (the default, and in tests and SPARC) nothing changes.

Mail goes through any SMTP server: SMTP_USER / SMTP_PASS, optional SMTP_HOST (default smtp.gmail.com) and
SMTP_PORT (465 = SSL, 587 = STARTTLS), MAIL_FROM. Owner notifications go to NOTIFY_EMAIL or OWNER_EMAIL;
NOTIFY_ACCESS=0 turns them off.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import smtplib
import ssl
import threading
import time
from collections.abc import Callable
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

log = logging.getLogger("taskDataReadiness.access")

COOKIE = "tdr_access"
CODE_TTL_S = 15 * 60
MAX_TRIES = 5
VERIFIED_TTL_S = 30 * 24 * 3600
SITE = "Task Data Readiness"
PUBLIC_PATHS = frozenset({"/", "/dashboard", "/table", "/health", "/favicon.ico"})
PUBLIC_GETS = frozenset({"/pipeline/roles", "/pipeline/profiles", "/pipeline/datasets", "/pipeline/agents"})
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
# Common throwaway-inbox services. Not exhaustive: the emailed code is what really proves the address.
DISPOSABLE = frozenset(
    "mailinator.com guerrillamail.com guerrillamail.net sharklasers.com grr.la 10minutemail.com 10minutemail.net "
    "tempmail.com temp-mail.org temp-mail.io tempmail.net tempmailo.com yopmail.com yopmail.fr yopmail.net "
    "trashmail.com trashmail.de getnada.com nada.email dispostable.com maildrop.cc mailnesia.com mintemail.com "
    "mohmal.com throwawaymail.com fakeinbox.com spamgourmet.com emailondeck.com moakt.com tempr.email discard.email "
    "mailcatch.com inboxkitten.com burnermail.io 33mail.com spambox.us mytemp.email tempinbox.com luxusmail.org "
    "emailfake.com fakemail.net".split()
)


class ContactIn(BaseModel):
    name: str = Field("", max_length=200)
    email: str = Field("", max_length=200)
    company: str = Field("", max_length=200)


class VerifyIn(ContactIn):
    code: str = Field("", max_length=20)


def _err(status: int, code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message, "details": []}, **extra}
    )


def clean_contact(body: ContactIn) -> tuple[dict | None, str | None]:
    """Trimmed, single-line contact fields, or an error message for the visitor."""
    name = CONTROL.sub(" ", body.name).strip()
    email = CONTROL.sub("", body.email).strip().lower()
    company = CONTROL.sub(" ", body.company).strip()
    if not 2 <= len(name) <= 80:
        return None, "Please enter your name."
    if len(email) > 160 or not EMAIL_RE.match(email):
        return None, "Please enter a valid email."
    if len(company) > 120:
        return None, "Company name is too long."
    if email.split("@")[1] in DISPOSABLE:
        return None, "Please use your work or personal email, not a temporary inbox."
    return {"name": name, "email": email, "company": company}, None


class AccessGate:
    def __init__(self, enabled: bool, data_dir: Path, env: dict[str, str] | None = None):
        env = dict(os.environ if env is None else env)
        self.enabled = enabled
        self.data_dir = data_dir
        self.smtp_host = env.get("SMTP_HOST") or "smtp.gmail.com"
        self.smtp_port = int(env.get("SMTP_PORT") or 465)
        self.smtp_user = env.get("SMTP_USER") or ""
        self.smtp_pass = env.get("SMTP_PASS") or ""
        self.mail_from = env.get("MAIL_FROM") or f'"{SITE}" <{self.smtp_user}>'
        self.notify_to = (
            "" if env.get("NOTIFY_ACCESS") == "0" else (env.get("NOTIFY_EMAIL") or env.get("OWNER_EMAIL") or "")
        )
        self._secret = (env.get("TDR_GATE_SECRET") or "").encode() or None
        self._codes: dict[str, tuple[str, float, int]] = {}
        self._sends: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self.send: Callable[[EmailMessage], None] = self._smtp_send  # tests replace this

    # ------------------------------------------------------------------ signing
    @property
    def secret(self) -> bytes:
        if self._secret is None:  # one random secret per install, kept next to the other runtime data
            path = self.data_dir / "gate_secret"
            if path.exists():
                self._secret = path.read_bytes().strip()
            else:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                self._secret = secrets.token_hex(32).encode()
                path.write_bytes(self._secret)
                path.chmod(0o600)
        return self._secret

    def sign(self, payload: dict) -> str:
        data = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
        sig = (
            base64.urlsafe_b64encode(hmac.new(self.secret, data.encode(), hashlib.sha256).digest()).decode().rstrip("=")
        )
        return f"{data}.{sig}"

    def unsign(self, token: str | None) -> dict | None:
        if not token or token.count(".") != 1:
            return None
        data, sig = token.split(".")
        good = (
            base64.urlsafe_b64encode(hmac.new(self.secret, data.encode(), hashlib.sha256).digest()).decode().rstrip("=")
        )
        if not hmac.compare_digest(good, sig):
            return None
        try:
            payload = json.loads(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)))
        except ValueError:
            return None
        return payload if isinstance(payload, dict) and payload.get("exp", 0) > time.time() else None

    def visitor(self, request: Request) -> dict | None:
        return self.unsign(request.cookies.get(COOKIE))

    def allowed(self, request: Request) -> bool:
        path = request.url.path
        if not self.enabled or path in PUBLIC_PATHS or path.startswith("/access/"):
            return True
        if request.method in {"GET", "HEAD"} and path in PUBLIC_GETS:
            return True
        return self.visitor(request) is not None

    # ------------------------------------------------------------------ codes + throttling (in memory)
    def _hash(self, email: str, code: str) -> str:
        return hmac.new(self.secret, f"{email}|{code}".encode(), hashlib.sha256).hexdigest()

    def new_code(self, email: str) -> str:
        code = f"{secrets.randbelow(1_000_000):06d}"
        with self._lock:
            self._codes[email] = (self._hash(email, code), time.time() + CODE_TTL_S, 0)
        return code

    def check_code(self, email: str, code: str) -> str:
        with self._lock:
            entry = self._codes.get(email)
            if not entry or entry[1] < time.time() or entry[2] >= MAX_TRIES:
                self._codes.pop(email, None)
                return "expired"
            digest, exp, tries = entry
            if hmac.compare_digest(digest, self._hash(email, code.strip())):
                del self._codes[email]
                return "ok"
            self._codes[email] = (digest, exp, tries + 1)
            return "wrong"

    def throttled(self, key: str, limit: int, window_s: float) -> bool:
        now = time.time()
        with self._lock:
            recent = [t for t in self._sends.get(key, []) if now - t < window_s]
            if len(recent) >= limit:
                self._sends[key] = recent
                return True
            self._sends[key] = [*recent, now]
            if len(self._sends) > 20_000:
                self._sends.clear()
            return False

    # ------------------------------------------------------------------ mail + lead log
    @property
    def mail_configured(self) -> bool:
        return bool(self.smtp_user and self.smtp_pass)

    def _smtp_send(self, msg: EmailMessage) -> None:
        context = ssl.create_default_context()
        if self.smtp_port == 465:
            with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, context=context, timeout=20) as s:
                s.login(self.smtp_user, self.smtp_pass)
                s.send_message(msg)
        else:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=20) as s:
                s.starttls(context=context)
                s.login(self.smtp_user, self.smtp_pass)
                s.send_message(msg)

    def _message(self, to: str, subject: str, text: str, html: str | None = None, reply_to: str | None = None):
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.mail_from, to, subject
        if reply_to:
            msg["Reply-To"] = reply_to
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")
        return msg

    def send_code(self, to: str, code: str) -> None:
        self.send(
            self._message(
                to,
                f"{code} is your code for the {SITE} live demo",
                f"Your confirmation code is {code}\n\nEnter it on the {SITE} page to open the live demo. "
                "It expires in 15 minutes.\n\nIf you didn't ask for this, you can ignore this email.",
                f'<p>Your confirmation code is</p><p style="font-size:28px;font-weight:bold;letter-spacing:4px">{code}'
                f"</p><p>Enter it on the {SITE} page to open the live demo. It expires in 15 minutes.</p>"
                '<p style="color:#666">If you didn\'t ask for this, you can ignore this email.</p>',
            )
        )

    def notify_owner(self, c: dict) -> None:
        if not self.notify_to:
            return
        company = c["company"]
        who = c["name"] + (f" ({company})" if company else "") + f" <{c['email']}>"
        try:
            self.send(
                self._message(
                    self.notify_to,
                    f"Demo access: {c['name']}" + (f", {company}" if company else "") + f" — {SITE}",
                    f"{who} confirmed their email and opened the {SITE} live demo.\n\n"
                    "Reply to this email to contact them.",
                    reply_to=c["email"],
                )
            )
        except Exception as exc:  # the visitor is already in; a failed notification must not block them
            log.warning("access gate: owner notification failed: %s", exc)

    def log_lead(self, c: dict) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / "leads.jsonl"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": int(time.time() * 1000), "type": "verified", **c, "project": SITE}) + "\n")


def _client_ip(request: Request) -> str:
    ip = request.client.host if request.client else "unknown"
    if ip in {"127.0.0.1", "::1"}:  # behind the local Caddy proxy: it passes the visitor's address along
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            ip = forwarded.split(",")[0].strip()
    return ip


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    return not origin or urlparse(origin).netloc == request.headers.get("host")


def _https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


def register(app: FastAPI, gate: AccessGate) -> None:
    app.state.access_gate = gate

    @app.middleware("http")
    async def access_required(request: Request, call_next):
        if not gate.allowed(request):
            return _err(401, "access_required", "Confirm your email to open the live demo.")
        return await call_next(request)

    @app.get("/access/status", tags=["access"])
    def access_status(request: Request) -> dict:
        v = gate.visitor(request) if gate.enabled else None
        return {
            "gate": gate.enabled,
            "verified": bool(v) or not gate.enabled,
            "name": v["name"] if v else None,
            "email": v["email"] if v else None,
            "available": gate.mail_configured or not gate.enabled,
        }

    @app.post("/access/request", tags=["access"])
    def access_request(body: ContactIn, request: Request):
        """Step 1: check the details and email a 6-digit code (or let a visitor already confirmed here straight in)."""
        if not gate.enabled:
            return {"verified": True}
        if not _same_origin(request):
            return _err(403, "cross_origin", "Cross-origin request blocked.")
        contact, problem = clean_contact(body)
        if problem:
            return _err(422, "invalid_contact", problem)
        v = gate.visitor(request)
        if v and v.get("email") == contact["email"]:
            return {"verified": True}
        if not gate.mail_configured:
            return _err(503, "mail_not_configured", "Access codes can't be sent right now. Please try again later.")
        if gate.throttled(f"ip:{_client_ip(request)}", 10, 3600) or gate.throttled(f"email:{contact['email']}", 3, 900):
            return _err(429, "too_many_codes", "Too many codes requested. Please wait a few minutes and try again.")
        try:
            gate.send_code(contact["email"], gate.new_code(contact["email"]))
        except Exception as exc:
            log.error("access gate: sending the code failed: %s", exc)
            return _err(502, "send_failed", "We couldn't send the email. Please check the address or try again later.")
        return {"sent": True}

    @app.post("/access/verify", tags=["access"])
    def access_verify(body: VerifyIn, request: Request):
        """Step 2: check the emailed code, remember the visitor for 30 days."""
        if not gate.enabled:
            return {"verified": True}
        if not _same_origin(request):
            return _err(403, "cross_origin", "Cross-origin request blocked.")
        contact, problem = clean_contact(body)
        if problem or not re.fullmatch(r"\s*\d{6}\s*", body.code):
            return _err(422, "invalid_code", problem or "Please enter the 6-digit code from the email.")
        result = gate.check_code(contact["email"], body.code)
        if result == "expired":
            return _err(422, "code_expired", "This code has expired. Please ask for a new one.", expired=True)
        if result == "wrong":
            return _err(422, "code_wrong", "That code isn't right. Please check the email and try again.")
        gate.log_lead(contact)
        threading.Thread(target=gate.notify_owner, args=(contact,), daemon=True).start()
        res = JSONResponse({"verified": True, "name": contact["name"], "email": contact["email"]})
        token = gate.sign({**contact, "exp": int(time.time()) + VERIFIED_TTL_S})
        res.set_cookie(
            COOKIE, token, max_age=VERIFIED_TTL_S, httponly=True, samesite="lax", secure=_https(request), path="/"
        )
        return res

    @app.post("/access/logout", tags=["access"])
    def access_logout() -> JSONResponse:
        res = JSONResponse({"verified": False})
        res.delete_cookie(COOKIE, path="/")
        return res

    if gate.enabled and not gate.mail_configured:
        log.warning("TDR_ACCESS_GATE is on but SMTP_USER/SMTP_PASS are not set: nobody can get an access code")
