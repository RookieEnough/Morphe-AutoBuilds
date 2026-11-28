"""Uptodown mobile (app) API client.

Uptodown's public website download flow is protected by Cloudflare Turnstile
(mid/late 2026), which shared GitHub-hosted runner IPs cannot pass.  The
official Android client does not use that flow: it talks to an anonymous
``www.uptodown.app`` API, exchanging a signed anonymous identifier for a
short-lived bearer token and then resolving a catalog ``file id`` to a signed
``dw.uptodown.com`` download URL.

That path skips the Turnstile challenge entirely, so CI no longer depends on
FlareSolverr (or a lucky residential IP) to fetch Uptodown APKs.

Flow (mirrors Obtainium's ``lib/app_sources/uptodown.dart``):

1. ``POST {API_HOST}/eapi/auth/token?identifier=<32 hex>``
   form body: ``identifier``, ``id_plataforma=13`` (Android), ``lang=en``,
   ``unixtime``, ``hmac=HMAC_SHA256(hmac_key, unixtime)``.
   Response: ``{"token": "<JWT>"}``; the JWT ``exp`` claim is the expiry.
2. ``GET {API_HOST}/eapi/apps/<app code>/file/<file id>/downloadUrl``
   with ``Authorization: Bearer <token>``.
   Response: ``{"success": 1, "data": {"downloadURL": "https://dw..."}}``.

The HMAC key and client version are the anonymous client credentials shipped
inside the Android app; they are overridable through environment variables so
a rotated key can be patched in CI without touching code.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time

import requests

DEFAULT_API_HOST = "https://www.uptodown.app"
DEFAULT_HMAC_KEY = "MDGMXUMdvHJBG/vjdFgmqX6LUdy7ecfwvYNd0gyfOCs="
DEFAULT_CLIENT_VERSION = "739"
# The API is served to the Android app, so identify as that app rather than
# as a browser: a browser UA is what the Turnstile-protected website expects.
DEFAULT_USER_AGENT = (
    "Dalvik/2.1.0 (Linux; U; Android 16; Pixel 8 Pro Build/BP4A.260205.001)"
)
PLATFORM_ID = "13"  # Uptodown platform id for Android
LANG = "en"
REQUEST_TIMEOUT = 30
RETRY_DELAY = 1.5

# Cached anonymous session: (token, unix expiry).
_token: str | None = None
_token_expiry: float = 0.0


def _api_host() -> str:
    return (os.environ.get("UPTODOWN_API_HOST") or DEFAULT_API_HOST).rstrip("/")


def _hmac_key() -> str:
    return os.environ.get("UPTODOWN_API_HMAC_KEY") or DEFAULT_HMAC_KEY


def _client_version() -> str:
    return os.environ.get("UPTODOWN_API_CLIENT_VERSION") or DEFAULT_CLIENT_VERSION


def _user_agent() -> str:
    return os.environ.get("UPTODOWN_API_USER_AGENT") or DEFAULT_USER_AGENT


def _headers(token: str | None = None, *, for_auth: bool = False) -> dict[str, str]:
    headers = {
        "User-Agent": _user_agent(),
        "Identificador": "Uptodown_Android",
        "Identificador-Version": _client_version(),
    }
    if for_auth:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _request(method: str, url: str, **kwargs) -> requests.Response | None:
    """HTTP helper that retries transient failures once.

    Kept deliberately small: one retry for connection errors and 429/5xx
    responses.  A persistent failure is returned to the caller (or None when
    the request never completed) so callers can fall back to the HTML flow.
    """
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    for attempt in range(2):
        try:
            response = requests.request(method, url, **kwargs)
        except requests.RequestException as exc:
            logging.debug("Uptodown API %s %s failed: %s", method, url, exc)
            if attempt == 0:
                time.sleep(RETRY_DELAY)
                continue
            return None
        if response.status_code in (429, 500, 502, 503, 504) and attempt == 0:
            logging.debug(
                "Uptodown API %s %s returned %s; retrying", method, url, response.status_code
            )
            time.sleep(RETRY_DELAY)
            continue
        return response
    return None  # pragma: no cover - defensive


def _jwt_expiry(token: str) -> float | None:
    """Return the JWT ``exp`` claim as a unix timestamp, or None."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    try:
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    expiry = claims.get("exp") if isinstance(claims, dict) else None
    if isinstance(expiry, (int, float)):
        return float(expiry)
    return None


def reset_session() -> None:
    """Drop the cached token (used by tests and long-lived processes)."""
    global _token, _token_expiry
    _token = None
    _token_expiry = 0.0


def get_token(force_refresh: bool = False) -> str | None:
    """Return a cached anonymous bearer token, authenticating when needed."""
    global _token, _token_expiry

    now = time.time()
    if not force_refresh and _token and now < _token_expiry - 60:
        return _token

    identifier = secrets.token_hex(8)
    timestamp = str(int(now))
    signature = hmac.new(
        _hmac_key().encode("utf-8"), timestamp.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    form = {
        "identifier": identifier,
        "id_plataforma": PLATFORM_ID,
        "lang": LANG,
        "unixtime": timestamp,
        "hmac": signature,
    }
    url = f"{_api_host()}/eapi/auth/token?identifier={identifier}"
    response = _request("POST", url, data=form, headers=_headers(for_auth=True))
    if response is None:
        logging.warning("Uptodown mobile API unreachable for anonymous auth")
        return None
    if response.status_code != 200:
        logging.warning(
            "Uptodown mobile API auth failed with HTTP %s", response.status_code
        )
        return None

    try:
        body = response.json()
    except ValueError:
        logging.warning("Uptodown mobile API auth returned a non-JSON body")
        return None
    token = body.get("token") if isinstance(body, dict) else None
    expiry = _jwt_expiry(token) if isinstance(token, str) else None
    if not token or expiry is None:
        logging.warning("Uptodown mobile API auth response did not contain a usable token")
        return None

    _token, _token_expiry = token, expiry
    logging.info(
        "Uptodown mobile API session established (valid for %d min)",
        max(0, int((expiry - now) // 60)),
    )
    return token


def resolve_download_url(
    app_code: str | int | None, file_id: str | int | None
) -> str | None:
    """Resolve an Uptodown catalog file id to a signed download URL.

    ``app_code`` is the numeric application code published as
    ``data-code``/``data-app-id`` on the app page, and ``file_id`` is the
    ``data-file-id`` of the main file or of a variant.  Returns None so the
    caller can fall back to the HTML flow when the API is unavailable.
    """
    app_code = str(app_code or "").strip()
    file_id = str(file_id or "").strip()
    if not app_code or not file_id:
        return None

    url = f"{_api_host()}/eapi/apps/{app_code}/file/{file_id}/downloadUrl"
    for attempt in range(2):
        token = get_token(force_refresh=attempt > 0)
        if not token:
            return None
        response = _request("GET", url, headers=_headers(token))
        if response is None:
            return None
        if response.status_code == 401 and attempt == 0:
            logging.info("Uptodown mobile API token rejected; refreshing session")
            continue
        if response.status_code != 200:
            logging.debug(
                "Uptodown mobile API downloadUrl for app %s file %s returned HTTP %s",
                app_code,
                file_id,
                response.status_code,
            )
            return None
        try:
            body = response.json()
        except ValueError:
            logging.warning("Uptodown mobile API downloadUrl returned a non-JSON body")
            return None
        if not isinstance(body, dict) or body.get("success") != 1:
            logging.debug("Uptodown mobile API downloadUrl reported failure for file %s", file_id)
            return None
        data = body.get("data")
        download_url = data.get("downloadURL") if isinstance(data, dict) else None
        if isinstance(download_url, str) and download_url.startswith(("http://", "https://")):
            return download_url
        return None
    return None
