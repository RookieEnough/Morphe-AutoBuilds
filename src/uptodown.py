"""Uptodown scraper with CI-safe page discovery.

Uptodown serves the same catalog from several locale hostnames.  Its English
edge occasionally rejects GitHub-hosted IP ranges, so a valid app slug must be
tried on alternate official locale hosts before treating the app as missing.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin

import requests as plain_requests
from bs4 import BeautifulSoup

from src import session, flaresolverr, utils


LOCALES = ("en", "de", "fr", "in", "it", "ru", "jp", "kr")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def _is_app_page(response) -> bool:
    if response.status_code != 200:
        return False
    return BeautifulSoup(response.content, "html.parser").find("h1", id="detail-app-name") is not None


def _get(url: str):
    """Use curl-cffi first, then FlareSolverr for Cloudflare bypass, then plain requests."""
    try:
        response = session.get(url, headers=HEADERS, timeout=15)
        if _is_app_page(response) or response.status_code == 200 and "/apps/" in url:
            return response
        # Check for Cloudflare challenge - use FlareSolverr
        try:
            text = response.text[:5000] if hasattr(response, 'text') else ""
        except:
            text = ""
        if flaresolverr._is_cloudflare_challenge(response.status_code, text):
            logging.info(f"Uptodown Cloudflare challenge for {url}, trying FlareSolverr")
            fs_resp = flaresolverr.get_with_bypass(url, session=None, headers=HEADERS, timeout=30)
            if fs_resp:
                return fs_resp
        status = response.status_code
    except Exception as exc:
        status = type(exc).__name__

    try:
        fallback = plain_requests.get(url, headers=HEADERS, timeout=15)
        logging.debug("Uptodown fallback request %s -> %s (curl-cffi: %s)", url, fallback.status_code, status)
        return fallback
    except Exception as exc:
        logging.debug("Uptodown request failed for %s: curl-cffi=%s; requests=%s", url, status, exc)
        return None


def _same_version(left: str, right: str) -> bool:
    clean = lambda value: re.sub(r"[\(\[].*?[\)\]]", "", value or "").strip()
    return (
        left == right
        or clean(left) == clean(right)
        or utils.normalize_version(left) == utils.normalize_version(right)
    )


def _store_bases(config: dict):
    """Yield known slug/locale paths before heuristic fallback slugs."""
    slugs = generate_possible_uptodown_names(config)
    if not slugs:
        return
    # The configured slug is authoritative.  Probe all official locale hosts
    # before spending time on guessed names that cannot represent this app.
    for locale in LOCALES:
        yield f"https://{slugs[0]}.{locale}.uptodown.com/android"
    for slug in slugs[1:]:
        yield f"https://{slug}.en.uptodown.com/android"


def _get_app_page(config: dict, suffix: str = ""):
    for base_url in _store_bases(config):
        url = f"{base_url}{suffix}"
        response = _get(url)
        if response and _is_app_page(response):
            return base_url, response
        status = getattr(response, "status_code", "request failed")
        logging.debug("Uptodown probe %s returned %s", url, status)
    return None, None


def get_latest_version(app_name: str, config: dict) -> str | None:
    base_url, response = _get_app_page(config, "/versions")
    if not response:
        logging.warning("Uptodown: no usable page for %s after locale fallback", app_name)
        return None

    soup = BeautifulSoup(response.content, "html.parser")
    versions = [item.get_text(strip=True) for item in soup.select("#versions-items-list .version")]
    versions = [item for item in versions if item]
    if versions:
        logging.info("Uptodown found %s for %s at %s", versions[0], app_name, response.url)
        return versions[0]

    version = soup.select_one("div.version, [itemprop='softwareVersion']")
    if version and version.get_text(strip=True):
        return version.get_text(strip=True)
    logging.warning("Uptodown page for %s had no version rows: %s", app_name, base_url)
    return None


def _direct_url_from_page(soup: BeautifulSoup, page_url: str) -> str | None:
    button = soup.find(id="detail-download-button")
    if button:
        data_url = button.get("data-url")
        if data_url and data_url != "apps":
            return urljoin("https://dw.uptodown.com/dwn/", data_url)
    for selector in ("a#detail-download-button[href]", "a.download[href]"):
        link = soup.select_one(selector)
        if link and link.get("href"):
            href = link["href"]
            if "dw.uptodown.com" in href or href.endswith((".apk", ".xapk")):
                return urljoin(page_url, href)
    return None


def _xapk_url_via_trawl(page_url: str) -> str | None:
    """Extract the download URL for XAPK-only versions via browser rendering.

    Uptodown serves some versions (e.g. Facebook 580.0.0.51.74) as XAPK-only.
    The download button is a <button> without a data-url; the actual URL is
    resolved by JavaScript. Use FlareSolverr to render the page and capture
    the resulting download link.
    """
    result = flaresolverr.solve(page_url, timeout=45)
    if not result or not result.get("html"):
        return None
    soup = BeautifulSoup(result["html"], "html.parser")
    # After JS runs, the button may gain an href/data-url, or the page may
    # contain a direct dw.uptodown.com link.
    link = _direct_url_from_page(soup, page_url)
    if link:
        logging.info("Uptodown XAPK download URL obtained via browser rendering")
        return link
    # Fallback: search rendered HTML for dw.uptodown.com URLs
    for match in re.finditer(r'https://dw\.uptodown\.com/dwn/[A-Za-z0-9_\-/]+', result["html"]):
        url = match.group(0)
        logging.info("Uptodown XAPK download URL found in rendered page")
        return url
    return None


def _variant_file_id(base_url: str, data_code: str, version_page: BeautifulSoup, arch: str) -> str | None:
    """Select an Uptodown variant as rvb does before opening the -x page."""
    variants_button = version_page.select_one(".button.variants[data-version]")
    if not variants_button:
        return None
    data_version = variants_button.get("data-version")
    catalog_url = f"{base_url.rsplit('/android', 1)[0]}/app/{data_code}/version/{data_version}/files"
    response = _get(catalog_url)
    if not response or response.status_code != 200:
        return None
    try:
        content = (response.json() or {}).get("content") or ""
    except ValueError:
        return None
    soup = BeautifulSoup(content, "html.parser")
    requested = "armeabi-v7a" if arch == "arm-v7a" else arch
    current_arch = ""
    fallback_id = None
    for node in soup.select("section.variants > .content > *"):
        classes = node.get("class", [])
        if node.name == "p":
            current_arch = node.get_text(" ", strip=True).lower()
            continue
        if "variant" not in classes:
            continue
        report = node.select_one(".v-report[data-file-id]")
        if not report:
            continue
        file_id = report.get("data-file-id")
        if not fallback_id:
            fallback_id = file_id
        # Prefer full/universal variants, then the requested ABI.
        if arch == "universal" and "arm64-v8a" in current_arch and "armeabi-v7a" in current_arch:
            return file_id
        if requested and requested in current_arch:
            return file_id
    return fallback_id


def get_download_link(version: str, app_name: str, config: dict) -> str | None:
    if not version:
        return None

    base_url, listing = _get_app_page(config, "/versions")
    if not listing:
        return None
    heading = BeautifulSoup(listing.content, "html.parser").find("h1", id="detail-app-name")
    data_code = heading.get("data-code") if heading else None
    if not data_code:
        logging.warning("Uptodown page has no application code for %s", app_name)
        return None

    for page in range(1, 11):
        response = _get(f"{base_url}/apps/{data_code}/versions/{page}")
        if not response or response.status_code != 200:
            break
        try:
            entries = (response.json() or {}).get("data") or []
        except ValueError:
            break
        if not entries:
            break
        for entry in entries:
            if not _same_version(entry.get("version", ""), version):
                continue
            parts = entry.get("versionURL") or {}
            version_url = "/".join(str(parts.get(key, "")).strip("/") for key in ("url", "extraURL", "versionID"))
            page_response = _get(version_url) if version_url.startswith("http") else None
            if not page_response:
                continue
            version_soup = BeautifulSoup(page_response.content, "html.parser")
            # The main file is often a split/XAPK. Resolve the public variants
            # catalog first, then use its -x page just like rvb.
            variant_id = _variant_file_id(base_url, data_code, version_soup, config.get("arch", "universal"))
            if variant_id:
                variant_url = f"{base_url}/download/{variant_id}-x"
                variant_response = _get(variant_url)
                if variant_response:
                    link = _direct_url_from_page(
                        BeautifulSoup(variant_response.content, "html.parser"), variant_response.url
                    )
                    if not link:
                        # XAPK-only versions need JS rendering to resolve the URL
                        link = _xapk_url_via_trawl(variant_url)
                    if link:
                        return link
            link = _direct_url_from_page(version_soup, page_response.url)
            if link:
                return link
            logging.warning(
                "Uptodown found %s %s but its download endpoint requires an interactive token",
                app_name, version,
            )
            return None

        target = utils.normalize_version(version)
        if target and all(utils.normalize_version(item.get("version", "")) < target for item in entries):
            break
    logging.warning("Uptodown: version %s was not found for %s", version, app_name)
    return None


# Apps whose Uptodown slug differs from the config name/package guesses.
_SLUG_OVERRIDES = {
    "com.google.android.inputmethod.latin": ["gboard-go"],
}


def generate_possible_uptodown_names(config: dict) -> list[str]:
    """Return deterministic candidates, with the configured slug first."""
    app_name = (config.get("slug") or config.get("name") or "").strip().lower()
    package = (config.get("package") or "").strip().lower()
    candidates: list[str] = []

    def add(value: str) -> None:
        value = (value or "").strip().lower()
        if len(value) > 1 and value not in candidates:
            candidates.append(value)

    for override in _SLUG_OVERRIDES.get(package, []):
        add(override)
    add(app_name)
    add(app_name.replace("-", ""))
    add(app_name.replace("-plus", "plus"))
    add(app_name.replace("-", "_"))
    package_dash = package.replace(".", "-")
    add(package_dash)
    if package.startswith("com."):
        parts = package.split(".")
        add(package_dash.removeprefix("com-"))
        if len(parts) >= 2:
            add(f"com-{parts[1]}")
            add(f"com-{parts[1]}-{parts[-1]}")
            add(parts[1])
            add(parts[-1])
        if len(parts) >= 3:
            add(f"com-{parts[1]}{parts[2]}")
            add(f"com-{parts[1]}{parts[2]}-mea")
            add(f"com-{'-'.join(parts[1:])}")
    for suffix in ("", "-android", "-mobile", "-mea", "-plus", "-pro", "-lite", "-hd", "-apk"):
        add(app_name + suffix)
        add(package_dash + suffix)
    return candidates
