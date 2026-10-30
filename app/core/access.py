"""
Access Gate
-----------
Two protections that run before every request:

1. Cloudflare Access check (optional, on when ACCESS_TEAM_DOMAIN and
   ACCESS_AUD are set). Cloudflare Access sits in front of the app with
   Authentik as the login provider and adds a signed JWT to every request
   (Cf-Access-Jwt-Assertion). The app verifies that JWT itself, so even a
   request that reaches the port directly (skipping the tunnel) is refused.

2. Cross-site request check (always on). State-changing requests must come
   from this app's own page, so a random website can't make your browser
   POST to the API behind your back.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request

from google.auth import jwt as google_jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from app.core.config import settings

logger = logging.getLogger(__name__)

JWT_HEADER = "cf-access-jwt-assertion"
CERT_TTL_SECONDS = 3600
PUBLIC_PATHS = {"/healthz"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class _CertCache:
    """Caches Cloudflare Access signing certs (kid -> PEM)."""

    def __init__(self):
        self._certs: dict[str, str] = {}
        self._fetched_at = 0.0
        self._lock = threading.Lock()

    def _fetch(self) -> dict[str, str]:
        url = f"https://{settings.access_team_domain.strip()}/cdn-cgi/access/certs"
        with urllib.request.urlopen(url, timeout=10) as resp:  # nosec B310 (fixed https URL)
            data = json.loads(resp.read().decode())
        return {c["kid"]: c["cert"] for c in data.get("public_certs", []) if "kid" in c}

    def get(self, kid: str | None) -> dict[str, str]:
        with self._lock:
            stale = time.time() - self._fetched_at > CERT_TTL_SECONDS
            if stale or (kid and kid not in self._certs):
                try:
                    self._certs = self._fetch()
                    self._fetched_at = time.time()
                except Exception:
                    logger.exception("Could not fetch Cloudflare Access certs")
            return dict(self._certs)


cert_cache = _CertCache()


def _allowed_emails() -> set[str]:
    return {
        e.strip().lower()
        for e in settings.access_allowed_emails.split(",")
        if e.strip()
    }


def verify_access_jwt(token: str) -> dict:
    """Verify a Cloudflare Access JWT. Returns its claims or raises ValueError."""
    if not token:
        raise ValueError("missing Cloudflare Access token")
    try:
        header = google_jwt.decode_header(token)
    except Exception as e:
        raise ValueError("malformed token") from e
    if header.get("alg") != "RS256":
        raise ValueError("unexpected signing algorithm")

    certs = cert_cache.get(header.get("kid"))
    if not certs:
        raise ValueError("no signing certificates available")

    try:
        # Signature, iat and exp are checked here. The audience is checked
        # below because Cloudflare sends "aud" as a list, which the library's
        # own audience check doesn't accept.
        claims = google_jwt.decode(token, certs=certs, clock_skew_in_seconds=30)
    except Exception as e:
        raise ValueError(f"invalid token: {e}") from e

    aud_claim = claims.get("aud")
    audiences = aud_claim if isinstance(aud_claim, list) else [aud_claim]
    if settings.access_aud.strip() not in audiences:
        raise ValueError("token is for a different Access application")

    expected_iss = f"https://{settings.access_team_domain.strip()}"
    if claims.get("iss") != expected_iss:
        raise ValueError("token issued by a different Cloudflare team")

    allowed = _allowed_emails()
    if allowed and str(claims.get("email", "")).lower() not in allowed:
        raise ValueError("this account is not allowed")
    return claims


def _same_origin(request: Request) -> bool:
    """Is a state-changing request coming from this app's own page?"""
    # Modern browsers always send Sec-Fetch-Site; trust it when present.
    site = request.headers.get("sec-fetch-site")
    if site:
        return site in ("same-origin", "none")
    # Older clients: fall back to comparing Origin with Host.
    origin = request.headers.get("origin")
    if origin:
        host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
        return origin.split("://", 1)[-1].rstrip("/") == host
    return True


class AccessGateMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if path in PUBLIC_PATHS:
            return await call_next(request)

        if settings.access_gate_enabled:
            try:
                claims = verify_access_jwt(request.headers.get(JWT_HEADER, ""))
                request.state.user_email = claims.get("email")
            except ValueError as e:
                logger.warning("Blocked request to %s: %s", path, e)
                if path.startswith("/api/"):
                    return JSONResponse({"detail": "Not signed in"}, status_code=403)
                return PlainTextResponse(
                    "403: sign in through your Cloudflare Access / Authentik link.",
                    status_code=403,
                )

        if request.method not in SAFE_METHODS and not _same_origin(request):
            logger.warning("Blocked cross-site %s to %s", request.method, path)
            return JSONResponse({"detail": "Cross-site request blocked"}, status_code=403)

        return await call_next(request)
