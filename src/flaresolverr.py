"""FlareSolverr client for transparent Cloudflare bypass.

Adapted from vietanhbui2000/morphe-apps-builder's HttpClient.
When a request hits a Cloudflare challenge (403/503 or challenge markers),
it falls back to FlareSolverr (a browser automation service) to solve it.

FlareSolverr runs as a Docker service in CI on port 8192.
Set FLARESOLVERR_URL env var to override (default: http://localhost:8192/v1).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request


def _flaresolverr_url() -> str:
    return os.environ.get("FLARESOLVERR_URL", "http://localhost:8192/v1")


def _is_available() -> bool:
    """Check if FlareSolverr service is reachable."""
    try:
        base = _flaresolverr_url().replace("/v1", "")
        req = urllib.request.Request(
            base, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def _is_cloudflare_challenge(status_code: int, text: str) -> bool:
    """Detect Cloudflare challenge from status code and response text."""
    if status_code in (403, 503):
        return True
    markers = [
        "Just a moment...",
        "cf-chl-bypass",
        "Checking your browser",
        "cloudflare-static",
        "Attention Required",
        "cf-turnstile",
    ]
    return any(m in text for m in markers)


def solve(url: str, timeout: int = 60) -> dict | None:
    """Solve Cloudflare challenge via FlareSolverr.

    Returns dict with 'html', 'cookies', 'user_agent', or None on failure.
    """
    if not _is_available():
        logging.debug("FlareSolverr not available, skipping bypass")
        return None

    try:
        payload = json.dumps({
            "cmd": "request.get",
            "url": url,
            "maxTimeout": timeout * 1000,
        }).encode("utf-8")
        req = urllib.request.Request(
            _flaresolverr_url(),
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout + 10) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            if data.get("status") != "ok":
                logging.warning(
                    f"FlareSolverr failed for {url}: {data.get('message', 'unknown')}")
                return None
            solution = data.get("solution", {})
            return {
                "html": solution.get("response", ""),
                "cookies": {
                    c["name"]: c["value"]
                    for c in solution.get("cookies", [])
                    if c.get("name")
                },
                "user_agent": solution.get("userAgent", ""),
                "url": solution.get("url", url),
            }
    except Exception as e:
        logging.warning(f"FlareSolverr error for {url}: {e}")
        return None


class FlareSolverrResponse:
    """Response-like object for FlareSolverr results."""

    def __init__(self, url: str, html: str, cookies: dict, status_code: int = 200):
        self.url = url
        self._html = html
        self.cookies = cookies
        self.status_code = status_code
        self.content = html.encode("utf-8", errors="ignore")
        self.text = html
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


def get_with_bypass(url: str, session=None, headers: dict | None = None,
                    timeout: int = 30) -> FlareSolverrResponse | None:
    """GET a URL, falling back to FlareSolverr on Cloudflare challenge.

    Args:
        url: URL to fetch
        session: requests-like session to try first (optional)
        headers: headers for the initial request
        timeout: timeout in seconds

    Returns:
        Response-like object, or None on complete failure.
    """
    # Try normal request first
    if session is not None:
        try:
            resp = session.get(url, headers=headers, timeout=timeout)
            # Check for Cloudflare challenge
            text = ""
            try:
                text = resp.text[:5000] if hasattr(resp, 'text') else ""
            except Exception:
                pass
            if not _is_cloudflare_challenge(resp.status_code, text):
                return resp
            logging.info(f"Cloudflare challenge detected for {url}, trying FlareSolverr")
        except Exception as e:
            logging.debug(f"Direct request failed for {url}: {e}")

    # Fall back to FlareSolverr
    result = solve(url, timeout=timeout)
    if result and result["html"]:
        logging.info(f"FlareSolverr bypassed Cloudflare for {url}")
        return FlareSolverrResponse(
            url=result["url"],
            html=result["html"],
            cookies=result["cookies"],
        )

    logging.warning(f"All fetch methods failed for {url}")
    return None
