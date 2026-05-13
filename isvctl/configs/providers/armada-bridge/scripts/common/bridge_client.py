#!/usr/bin/env python3
"""BridgeClient — HTTP client for the Armada Bridge auth-gateway API.

Auth: POST /auth/login with {email, password} sets a session cookie (sid).
All subsequent requests carry that cookie automatically — same as the browser UI.
No Keycloak token endpoint or client credentials needed.

Cookie persistence: the session cookie is saved to ~/.cache/isvctl/bridge_session.cookies
and reloaded on the next run. Login is only performed when no valid cookie exists on disk.

MFA: set BRIDGE_TOTP_SECRET to the base32 TOTP secret — the current 6-digit code is
generated automatically at login time (RFC 6238, same algorithm as Google Authenticator).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.cookiejar
import json
import os
import ssl
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .file_logger import FileLogger, get_file_logger

_log: FileLogger = get_file_logger(__name__)


def _totp(secret_b32: str, *, digits: int = 6, interval: int = 30) -> str:
    """Generate the current TOTP code from a base32-encoded secret (RFC 6238)."""
    key = base64.b32decode(secret_b32.upper().replace(" ", ""))
    counter = int(time.time()) // interval
    msg = struct.pack(">Q", counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code = struct.unpack(">I", h[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10**digits)).zfill(digits)

_DEFAULT_COOKIE_PATH: Path = Path.home() / ".cache" / "isvctl" / "bridge_session.cookies"

_SENSITIVE_KEYS: frozenset[str] = frozenset({"password", "token", "secret", "totp", "apiKey", "api_key"})


def _redact(body: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of body with sensitive field values replaced by '***'."""
    return {k: "***" if k in _SENSITIVE_KEYS else v for k, v in body.items()}


class BridgeClient:
    """HTTP client for the Bridge auth-gateway API.

    Authenticates via POST /auth/login — receives a session cookie (sid) and
    replays it on every subsequent request, exactly as the browser UI does.
    The cookie is persisted to disk and reused across process runs; a fresh
    login is performed only when the cookie is absent or has expired.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        totp_secret: str | None = None,
        ssl_context: ssl.SSLContext | None = None,
        cookie_path: Path | None = _DEFAULT_COOKIE_PATH,
    ) -> None:
        """Initialise client fields; does NOT authenticate. Call login() or use from_env()."""
        self.base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._totp_secret = totp_secret
        self._ssl_context = ssl_context
        self._cookie_path = cookie_path

        if cookie_path is not None:
            jar: http.cookiejar.MozillaCookieJar = http.cookiejar.MozillaCookieJar(str(cookie_path))
            if cookie_path.exists():
                try:
                    jar.load(ignore_discard=True, ignore_expires=True)
                except (OSError, http.cookiejar.LoadError):
                    pass
        else:
            jar = http.cookiejar.MozillaCookieJar()

        self._jar = jar
        handlers: list[urllib.request.BaseHandler] = [urllib.request.HTTPCookieProcessor(jar)]
        if ssl_context is not None:
            handlers.append(urllib.request.HTTPSHandler(context=ssl_context))
        self._opener = urllib.request.build_opener(*handlers)

    @classmethod
    def from_env(cls) -> BridgeClient:
        """Construct a client from environment variables, logging in only when needed.

        Reads: BRIDGE_URL, BRIDGE_USERNAME (email), BRIDGE_PASSWORD.
        Optional: BRIDGE_TOTP_SECRET — base32 TOTP secret; when set, the current
        6-digit code is generated at login time (MFA-enabled accounts).
        Optional: BRIDGE_INSECURE=1 — disable TLS certificate verification (dev/self-signed certs).
        Loads a cached session cookie from disk if available and unexpired.
        Re-authenticates (and saves the new cookie) only when the session is missing or expired.
        """
        ssl_context: ssl.SSLContext | None = None
        if os.environ.get("BRIDGE_INSECURE") == "1":
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
        client = cls(
            base_url=os.environ["BRIDGE_URL"],
            username=os.environ["BRIDGE_USERNAME"],
            password=os.environ["BRIDGE_PASSWORD"],
            totp_secret=os.environ.get("BRIDGE_TOTP_SECRET"),
            ssl_context=ssl_context,
        )
        if not client._session_valid():
            client.login()
        return client

    def _session_valid(self) -> bool:
        """Return True if a non-expired sid cookie is present in the jar."""
        now = time.time()
        for cookie in self._jar:
            if cookie.name == "sid":
                # Session cookies have no explicit expiry — trust them until the server rejects.
                return cookie.expires is None or cookie.expires > now
        return False

    def _save_cookies(self) -> None:
        if self._cookie_path is None:
            return
        self._cookie_path.parent.mkdir(parents=True, exist_ok=True)
        self._jar.save(ignore_discard=True, ignore_expires=True)

    def login(self) -> None:
        """Authenticate via POST /auth/login and persist the session cookie to disk.

        POST {base_url}/auth/login
        Content-Type: application/json
        Body: {email: BRIDGE_USERNAME, password: BRIDGE_PASSWORD}
        The auth-gateway sets a session cookie (sid) which the opener stores
        and replays automatically on all subsequent requests.
        """
        body: dict[str, str] = {"email": self._username, "password": self._password}
        if self._totp_secret:
            body["totp"] = _totp(self._totp_secret)
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            self.base_url + "/auth/login",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        _log.debug("POST %s/auth/login (credentials redacted)", self.base_url)
        with self._opener.open(req, timeout=30) as resp:
            _log.debug("POST /auth/login -> %d", resp.status)
        self._save_cookies()

    def login_as(self, email: str, password: str) -> BridgeClient:
        """Return a new BridgeClient authenticated as a different user.

        Used in IAM create_user flow: POST /key-manager/api-key must be called
        by the target user (not the admin). Creates a fresh, ephemeral session
        (not persisted to disk) for the given credentials.
        """
        client = BridgeClient(
            base_url=self.base_url,
            username=email,
            password=password,
            ssl_context=self._ssl_context,
            cookie_path=None,
        )
        client.login()
        return client

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET {self.base_url}{path} with session cookie auth."""
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        _log.debug("GET %s", url)
        req = urllib.request.Request(url, method="GET")
        try:
            with self._opener.open(req, timeout=30) as resp:
                raw = resp.read().decode()
                _log.debug("GET %s -> %d  body=%s", url, resp.status, raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            _log.debug("GET %s -> %d  body=%s", url, e.code, raw)
            raise ValueError(f"GET {path} failed with status {e.code}: {raw}") from e
        return json.loads(raw)

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST {self.base_url}{path} with session cookie auth."""
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        _log.debug("POST %s%s  body=%s", self.base_url, path, json.dumps(_redact(body)))
        try:
            with self._opener.open(req, timeout=30) as resp:
                raw = resp.read().decode()
                _log.debug("POST %s%s -> %d  body=%s", self.base_url, path, resp.status, raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            _log.debug("POST %s%s -> %d  body=%s", self.base_url, path, e.code, raw)
            raise ValueError(f"POST {path} failed with status {e.code}: {raw}") from e
        return json.loads(raw) if raw else {}

    def delete(self, path: str) -> dict[str, Any]:
        """DELETE {self.base_url}{path} with session cookie auth."""
        _log.debug("DELETE %s%s", self.base_url, path)
        req = urllib.request.Request(self.base_url + path, method="DELETE")
        try:
            with self._opener.open(req, timeout=30) as resp:
                raw = resp.read().decode()
                _log.debug("DELETE %s%s -> %d  body=%s", self.base_url, path, resp.status, raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            _log.debug("DELETE %s%s -> %d  body=%s", self.base_url, path, e.code, raw)
            if e.code != 404:
                raise ValueError(f"DELETE {path} failed with status {e.code}: {raw}") from e
        return json.loads(raw) if raw else {}

    def patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """PATCH {self.base_url}{path} with session cookie auth."""
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="PATCH",
        )
        _log.debug("PATCH %s%s  body=%s", self.base_url, path, json.dumps(_redact(body)))
        try:
            with self._opener.open(req, timeout=30) as resp:
                raw = resp.read().decode()
                _log.debug("PATCH %s%s -> %d  body=%s", self.base_url, path, resp.status, raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            _log.debug("PATCH %s%s -> %d  body=%s", self.base_url, path, e.code, raw)
            raise ValueError(f"PATCH {path} failed with status {e.code}: {raw}") from e
        return json.loads(raw) if raw else {}

    def wait_for_state(
        self,
        path: str,
        target_state: str,
        *,
        state_field: str = "state",
        timeout: int = 300,
        interval: int = 10,
    ) -> dict[str, Any]:
        """Poll GET path until response[state_field] == target_state or timeout.

        Returns the final response dict.
        Raises TimeoutError if target_state is not reached within timeout seconds.
        """
        deadline = time.monotonic() + timeout
        while True:
            resp = self.get(path)
            if resp.get(state_field) == target_state:
                return resp
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Timed out after {timeout}s waiting for {path} "
                    f"{state_field!r} == {target_state!r}; last value: {resp.get(state_field)!r}"
                )
            time.sleep(interval)
