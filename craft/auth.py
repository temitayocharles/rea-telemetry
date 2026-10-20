"""Authentication and TLS for the HTTP surface.

TelemetryCraft is local-first and open by default. This module adds an opt-in
lock so the same binary can sit on a shared network without a proxy in front:

    CRAFT_AUTH_TOKEN=s3cret python3 -m craft
    CRAFT_AUTH_USER=ops CRAFT_AUTH_PASSWORD=hunter2 python3 -m craft
    CRAFT_TLS_CERT=cert.pem CRAFT_TLS_KEY=key.pem python3 -m craft

Both can be combined. Nothing here is enabled unless you ask for it, so the
default experience stays zero-config.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import os
import ssl
import time
from urllib.parse import parse_qs

# Endpoints that must stay reachable without credentials, so that a container
# healthcheck and a browser's first paint (which fetches nothing sensitive)
# keep working while everything else is locked down.
PUBLIC_PATHS = {
    "/api/health",
    "/api/status/health",
    "/api/auth/status",
    "/api/auth/login",
}

FAILED_ATTEMPT_WINDOW_S = 60
FAILED_ATTEMPT_LIMIT = 10


class AuthError(Exception):
    def __init__(self, message: str = "authentication required", challenge: str = "Bearer"):
        super().__init__(message)
        self.challenge = challenge


def _clean(value: str | None) -> str:
    return (value or "").strip()


class Auth:
    """Holds the configured credential and verifies requests against it."""

    def __init__(self, token: str = "", user: str = "", password: str = ""):
        self.token = _clean(token)
        self.user = _clean(user)
        self.password = password or ""
        self._failures: dict[str, list[float]] = {}

    # ---- construction -------------------------------------------------
    @classmethod
    def from_env(cls) -> "Auth":
        return cls(
            token=os.environ.get("CRAFT_AUTH_TOKEN", ""),
            user=os.environ.get("CRAFT_AUTH_USER", ""),
            password=os.environ.get("CRAFT_AUTH_PASSWORD", ""),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.token or (self.user and self.password))

    @property
    def method(self) -> str:
        if self.token and self.user:
            return "token+basic"
        if self.token:
            return "token"
        if self.user:
            return "basic"
        return "none"

    def status(self, headers=None) -> dict:
        """Public description of the auth setup; `ok` mirrors the credential sent."""
        out = {"enabled": self.enabled, "method": self.method}
        if self.enabled:
            out["ok"] = self.accepts(headers) if headers is not None else False
        return out

    def accepts(self, headers, params=None) -> bool:
        """True when these headers carry a valid credential (no public-path shortcut)."""
        return self._credential_ok(headers or {}, params)

    def _credential_ok(self, headers, params=None) -> bool:
        header = (headers.get("Authorization") or "").strip()
        if header:
            scheme, _, value = header.partition(" ")
            scheme = scheme.lower()
            if scheme == "bearer" and self.verify_token(value.strip()):
                return True
            if scheme == "basic" and self.verify_basic(value.strip()):
                return True
        if params:
            supplied = (params.get("token") or [""])[0]
            if supplied and self.verify_token(supplied):
                return True
        return False

    # ---- verification -------------------------------------------------
    def _expected_basic(self) -> str:
        raw = f"{self.user}:{self.password}".encode()
        return base64.b64encode(raw).decode()

    def verify_token(self, candidate: str) -> bool:
        if not self.token:
            return False
        return hmac.compare_digest(candidate.encode(), self.token.encode())

    def verify_basic(self, header: str) -> bool:
        if not (self.user and self.password):
            return False
        try:
            decoded = base64.b64decode(header, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return False
        expected = f"{self.user}:{self.password}"
        return hmac.compare_digest(decoded, expected)

    def check(self, headers, path: str, params: dict | None = None) -> None:
        """Raise AuthError unless the request carries a valid credential."""
        if not self.enabled or path in PUBLIC_PATHS:
            return

        # `?token=` keeps `curl` and scrape configs simple; it is still a secret,
        # so it is documented as a convenience for local tooling only.
        if self._credential_ok(headers, params):
            return

        raise AuthError("authentication required", challenge="Bearer" if self.token else "Basic")

    def throttle(self, client: str) -> bool:
        """Return True when this client has failed too often, too recently."""
        return len(self._recent(client)) >= FAILED_ATTEMPT_LIMIT

    def locked_out(self, client: str) -> bool:
        """Same test, and it also prunes failures that have aged out."""
        return len(self._recent(client)) >= FAILED_ATTEMPT_LIMIT

    def _recent(self, client: str) -> list:
        now = time.time()
        recent = [t for t in self._failures.get(client, []) if now - t < FAILED_ATTEMPT_WINDOW_S]
        self._failures[client] = recent
        return recent

    def note_failure(self, client: str) -> None:
        self._failures.setdefault(client, []).append(time.time())

    def note_success(self, client: str) -> None:
        self._failures.pop(client, None)


def params_from_query(query: str) -> dict:
    return parse_qs(query or "")


# --------------------------------------------------------------------------
# TLS
# --------------------------------------------------------------------------
def tls_context(cert: str = "", key: str = "", client_ca: str = "") -> ssl.SSLContext | None:
    """Build an SSL context from PEM files, or None when TLS is not configured."""
    cert = _clean(cert) or _clean(os.environ.get("CRAFT_TLS_CERT", ""))
    key = _clean(key) or _clean(os.environ.get("CRAFT_TLS_KEY", ""))
    client_ca = _clean(client_ca) or _clean(os.environ.get("CRAFT_TLS_CLIENT_CA", ""))
    if not cert and not key:
        return None
    if not cert or not key:
        raise ValueError("TLS needs both a certificate and a private key")
    for path in (cert, key):
        if not os.path.exists(path):
            raise FileNotFoundError(f"TLS file not found: {path}")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    if client_ca:
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(cafile=client_ca)
    return ctx


def scheme_for(ctx: ssl.SSLContext | None) -> str:
    return "https" if ctx is not None else "http"
