"""Local OAuth 2.0 + PKCE helpers for HH API credentials."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import tempfile
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

import requests


AUTHORIZE_URL = "https://hh.ru/oauth/authorize"
TOKEN_URL = "https://api.hh.ru/token"


def pkce_pair() -> tuple[str, str]:
    """Return high-entropy verifier and S256 challenge without padding."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def authorization_url(*, client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    query = urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    })
    return f"{AUTHORIZE_URL}?{query}"


def validate_loopback_redirect(redirect_uri: str) -> tuple[str, int, str]:
    """Permit only local HTTP redirect handlers controlled by this process."""
    parsed = urlparse(redirect_uri)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("--redirect-uri must use http://127.0.0.1 or http://localhost")
    if parsed.port is None:
        raise ValueError("--redirect-uri must include an explicit port")
    if parsed.query or parsed.fragment:
        raise ValueError("--redirect-uri must not include query or fragment")
    return parsed.hostname, parsed.port, parsed.path or "/"


def wait_for_authorization_code(redirect_uri: str, expected_state: str, timeout: int) -> str:
    """Run a one-shot local callback server and return a validated OAuth code."""
    host, port, callback_path = validate_loopback_redirect(redirect_uri)
    result: dict[str, str] = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - HTTP handler API requires this name.
            parsed = urlparse(self.path)
            values = parse_qs(parsed.query)
            if parsed.path != callback_path:
                self.send_response(404); self.end_headers(); return
            if values.get("state", [None])[0] != expected_state:
                result["error"] = "OAuth callback state mismatch"
                self.send_response(400); self.end_headers(); self.wfile.write(b"OAuth state mismatch. You may close this tab."); return
            if values.get("error"):
                result["error"] = f"OAuth authorization failed: {values['error'][0]}"
                self.send_response(400); self.end_headers(); self.wfile.write(b"Authorization was denied. You may close this tab."); return
            code = values.get("code", [None])[0]
            if not code:
                result["error"] = "OAuth callback did not contain code"
                self.send_response(400); self.end_headers(); self.wfile.write(b"Authorization code missing. You may close this tab."); return
            result["code"] = code
            self.send_response(200); self.send_header("Content-Type", "text/plain; charset=utf-8"); self.end_headers()
            self.wfile.write(b"HH authorization completed. You may close this tab.")

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = HTTPServer((host, port), CallbackHandler)
    server.timeout = min(1, timeout)
    deadline = time.monotonic() + timeout
    try:
        while "code" not in result and "error" not in result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if "error" in result:
        raise ValueError(result["error"])
    if "code" not in result:
        raise TimeoutError("OAuth callback timed out")
    return result["code"]


def request_token(
    payload: dict[str, str], *, user_agent: str, session: requests.Session | None = None,
) -> dict[str, Any]:
    """Exchange or refresh HH token; return only a validated OAuth payload."""
    client = session or requests.Session()
    response = client.post(
        TOKEN_URL, data=payload, timeout=30,
        headers={"HH-User-Agent": user_agent, "Accept": "application/json"},
    )
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict) or not isinstance(result.get("access_token"), str) or not result["access_token"]:
        raise ValueError("HH OAuth response has no access_token")
    if result.get("refresh_token") is not None and not isinstance(result["refresh_token"], str):
        raise ValueError("HH OAuth response has invalid refresh_token")
    if result.get("expires_in") is not None and not isinstance(result["expires_in"], (int, float)):
        raise ValueError("HH OAuth response has invalid expires_in")
    return result


def read_token_file(path: str | Path) -> dict[str, Any]:
    token_path = Path(path)
    if not token_path.is_file():
        raise ValueError(f"token file does not exist: {token_path}")
    try:
        data = json.loads(token_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"token file is not valid JSON: {token_path}") from error
    if not isinstance(data, dict) or not isinstance(data.get("access_token"), str) or not data["access_token"]:
        raise ValueError(f"token file has no access_token: {token_path}")
    return data


def write_token_file(path: str | Path, token: dict[str, Any], *, overwrite: bool) -> None:
    """Atomically persist secret material with owner-only permissions."""
    token_path = Path(path)
    if token_path.exists() and not overwrite:
        raise ValueError(f"token file already exists: {token_path}; use --overwrite")
    token_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(token, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{token_path.name}.", dir=token_path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
        os.replace(temporary, token_path)
        os.chmod(token_path, 0o600)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass
        raise


def token_metadata(token: dict[str, Any]) -> dict[str, Any]:
    """Safe user-visible metadata; never return token values."""
    result: dict[str, Any] = {"refreshable": bool(token.get("refresh_token"))}
    if isinstance(token.get("expires_in"), (int, float)):
        result["expires_in"] = int(token["expires_in"])
    return result
