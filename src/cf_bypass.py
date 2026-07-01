"""
Lightweight Cloudflare Turnstile bypass using nodriver (undetected Chrome).

Uses the system-installed Chrome binary (pre-installed on GitHub Actions
ubuntu-latest runners), so there is NO extra browser download.

Flow:
  1. Detect a 403 "Just a moment..." response from APKMirror.
  2. Spin up a headless Chrome via nodriver.
  3. Let Cloudflare solve itself (Turnstile auto-clears for real browsers).
  4. Grab cf_clearance + other cookies.
  5. Inject them into the curl_cffi session for all subsequent requests.
  6. Cache cookies so the browser only launches ONCE per run.
"""
import asyncio
import logging

import os
import shutil
import subprocess

# Module-level cookie cache: once solved, reuse for every request.
_cf_cookie_cache: dict = {}
_solve_attempted: bool = False


def find_working_chrome() -> str | None:
    """Finds a native, working Google Chrome/Chromium binary, skipping snap packages
    that fail in headless container/CI environments."""
    candidates = [
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "google-chrome-stable",
        "google-chrome",
        "chromium-browser",
        "chromium",
    ]

    found_paths = []
    for candidate in candidates:
        path = shutil.which(candidate) or (candidate if os.path.exists(candidate) else None)
        if path and path not in found_paths:
            found_paths.append(path)

    logging.info(f"Detected browser candidates: {found_paths}")

    for path in found_paths:
        try:
            if "snap" in path:
                logging.info(f"Skipping snap candidate: {path}")
                continue
            
            res = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                logging.info(f"Selected working browser: {path} ({res.stdout.strip()})")
                return path
            else:
                logging.warning(f"Browser at {path} failed with exit code {res.returncode}: {res.stderr.strip()}")
        except Exception as e:
            logging.warning(f"Failed to execute browser test for {path}: {e}")

    if found_paths:
        logging.warning(f"No candidates passed execution tests. Falling back to first found: {found_paths[0]}")
        return found_paths[0]
    return None


async def _solve_challenge_async(url: str, timeout: int = 30) -> dict:
    """Navigate to *url* in headless Chrome, wait for Cloudflare to clear,
    and return a dict of cookies for the apkmirror.com domain."""
    import nodriver as uc  # imported lazily so the dep is optional

    # Monkeypatch HTTPApi.get to handle slow startup environments (CI runners)
    orig_get = uc.core.browser.HTTPApi.get

    async def patched_get(self, endpoint: str):
        if endpoint == "version":
            for attempt in range(60):
                try:
                    return await orig_get(self, endpoint)
                except Exception:
                    if attempt == 59:
                        raise
                    await asyncio.sleep(0.5)
        return await orig_get(self, endpoint)

    uc.core.browser.HTTPApi.get = patched_get

    cookies: dict = {}
    browser = None
    try:
        chrome_path = find_working_chrome()
        browser = await uc.start(
            browser_executable_path=chrome_path,
            headless=True,
            sandbox=False,
            browser_args=["--disable-gpu"]
        )
        page = await browser.get(url)

        # Poll until Cloudflare clears (title doesn't contain challenge indicators)
        elapsed = 0
        while elapsed < timeout:
            await asyncio.sleep(0.5)
            elapsed += 0.5
            try:
                title = await page.evaluate("document.title")
            except Exception:
                title = ""
            
            title_lower = title.lower() if title else ""
            if title_lower and not any(term in title_lower for term in ["just a moment", "attention required", "cloudflare", "turnstile"]):
                logging.info(f"Cloudflare cleared after {elapsed:.1f}s — title: {title}")
                break
        else:
            logging.warning(f"Cloudflare did not clear within {timeout}s")

        # Small extra wait for cookies to finalise
        await asyncio.sleep(1)

        # Extract cookies
        raw = await browser.cookies.get_all()
        for c in raw:
            domain = getattr(c, "domain", "") or ""
            name   = getattr(c, "name", "")   or ""
            value  = getattr(c, "value", "")  or ""
            if "apkmirror" in domain and name:
                cookies[name] = value

        if cookies:
            logging.info(f"Extracted {len(cookies)} APKMirror cookie(s)")
        else:
            logging.warning("No APKMirror cookies found after challenge")
    except Exception as e:
        logging.warning(f"nodriver Cloudflare bypass error: {e}")
        if chrome_path:
            try:
                logging.info("Running Chrome diagnostic command to capture stderr...")
                diag = subprocess.run(
                    [
                        chrome_path,
                        "--headless=new",
                        "--no-sandbox",
                        "--disable-gpu",
                        "--remote-debugging-port=0",
                        "--user-data-dir=/tmp/uc_diag"
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                logging.info(f"Diag exit code: {diag.returncode}")
                logging.info(f"Diag stdout: {diag.stdout}")
                logging.info(f"Diag stderr: {diag.stderr}")
            except Exception as diag_err:
                logging.warning(f"Could not run Chrome diagnostics: {diag_err}")
    finally:
        if browser:
            try:
                browser.stop()
            except Exception:
                pass
    return cookies


def solve_cloudflare(url: str, timeout: int = 30) -> dict:
    """Synchronous entry-point.  Returns cached cookies on repeat calls."""
    global _cf_cookie_cache, _solve_attempted

    if _cf_cookie_cache:
        return _cf_cookie_cache

    if _solve_attempted:
        # Already failed once this run; don't retry (saves time).
        return {}

    _solve_attempted = True

    try:
        loop = asyncio.new_event_loop()
        _cf_cookie_cache = loop.run_until_complete(
            _solve_challenge_async(url, timeout)
        )
        loop.close()
    except Exception as e:
        logging.warning(f"Cloudflare bypass failed: {e}")
        _cf_cookie_cache = {}

    return _cf_cookie_cache


def is_cf_challenge(response) -> bool:
    """Return True if the response is a Cloudflare Turnstile / JS challenge."""
    if response.status_code != 403:
        return False
    # Header set by Cloudflare when it serves a managed challenge page
    if response.headers.get("cf-mitigated") == "challenge":
        return True
    # Fallback: check body for the classic Turnstile/Challenge indicators
    try:
        snippet = response.text[:1000] if hasattr(response, "text") else ""
        snippet_lower = snippet.lower()
        if "just a moment" in snippet_lower or "attention required" in snippet_lower or "cloudflare" in snippet_lower:
            return True
    except Exception:
        pass
    return False
