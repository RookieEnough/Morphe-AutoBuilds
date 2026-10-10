"""APKCombo fallback downloader, based on its public download pages.

APKCombo is intentionally last in the provider cascade.  It is used only when
the primary stores decline GitHub-hosted traffic or do not carry the app.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests as plain_requests
from bs4 import BeautifulSoup

from src import session, utils, flaresolverr

BASE_URL = "https://apkcombo.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36"}


def _page(package: str, suffix: str = ""):
    resp = flaresolverr.get_with_bypass(f"{BASE_URL}/search/{package}/download{suffix}", session=session, headers=HEADERS, timeout=25)
    return resp if resp else session.get(f"{BASE_URL}/search/{package}/download{suffix}", headers=HEADERS, timeout=25)


def get_latest_version(app_name: str, config: dict) -> str | None:
    package = (config.get("package") or "").strip()
    if not package:
        return None
    try:
        response = _page(package)
        response.raise_for_status()
        versions = re.findall(r"phone-([0-9][^-]*)-(?:apk|xapk|apks)", response.text)
        versions = [value for value in versions if value and value[0].isdigit()]
        if versions:
            return utils.get_highest_version(versions)
    except Exception as exc:
        logging.debug("APKCombo latest-version lookup failed for %s: %s", app_name, exc)
    return None


def _unwrap_redirect(url: str) -> str:
    parsed = urlparse(url)
    if parsed.path == "/r2":
        target = parse_qs(parsed.query).get("u", [""])[0]
        if target:
            return unquote(target)
    return url


def _dynamic_download_link(response, package: str) -> str | None:
    """Resolve APKCombo's JavaScript-loaded download tab.

    APKCombo no longer embeds a ``.variant`` link in many download pages.  The
    browser POSTs to ``<app>/<xid>/dl`` and receives the same links as an HTML
    fragment.  Calling that public endpoint directly is both less fragile than
    a browser and is the flow used by the rvb project this fallback follows.
    """
    page_url = response.url
    page_html = response.text
    xid_match = re.search(r'\bxid\s*=\s*["\']([^"\']+)', page_html)
    if not xid_match:
        return None

    # Canonical URLs have the form /<slug>/<package>/download/phone-<version>.
    # Keep the app path and replace only the download suffix.
    app_path = re.sub(r"/download(?:/[^/?#]+)?/?(?:[?#].*)?$", "/", urlparse(page_url).path)
    if not app_path.endswith("/"):
        app_path += "/"
    endpoint = urljoin(page_url, f"{app_path.lstrip('/')}{xid_match.group(1)}/dl")

    request_kwargs = {
        "data": {"package_name": package, "version": ""},
        "headers": {**HEADERS, "Referer": page_url, "X-Requested-With": "XMLHttpRequest"},
        "timeout": 25,
    }
    # curl-cffi is normally better at Cloudflare, but APKCombo's AJAX endpoint
    # intermittently resets that TLS fingerprint.  Retry once with requests;
    # each response uses the same public endpoint and contains no session-only
    # data, so this is safe and fixes otherwise random per-app failures.
    for post in (session.post, plain_requests.post):
        try:
            fragment = post(endpoint, **request_kwargs)
            fragment.raise_for_status()
        except Exception as exc:
            logging.debug("APKCombo dynamic request failed for %s: %s", package, exc)
            continue
        soup = BeautifulSoup(fragment.content, "html.parser")
        for anchor in soup.select("a.variant[href]"):
            href = anchor.get("href")
            if href:
                return _unwrap_redirect(urljoin(fragment.url, href))
    return None


def get_download_link(version: str, app_name: str, config: dict) -> str | None:
    package = (config.get("package") or "").strip()
    if not package or not version:
        return None
    for extension in ("apk", "xapk", "apks"):
        page_url = f"{BASE_URL}/search/{package}/download/phone-{version}-{extension}"
        try:
            response = _page(package, f"/phone-{version}-{extension}")
            response.raise_for_status()
            soup = BeautifulSoup(response.content, "html.parser")
            # The public page puts signed assets behind /r2?u=… redirects.
            # Select an actual variant link, never advertising/navigation links.
            for anchor in soup.select("a.variant[href]"):
                href = anchor.get("href")
                if href:
                    return _unwrap_redirect(urljoin(response.url, href))
            dynamic_link = _dynamic_download_link(response, package)
            if dynamic_link:
                return dynamic_link
        except Exception as exc:
            logging.debug("APKCombo download-link lookup failed for %s %s: %s", app_name, version, exc)
        # Retry with regular requests when the impersonated browser connection
        # was reset or served a transient empty shell (observed in CI for
        # MacroFactor).  APKCombo's pages are public and this does not alter
        # which version is selected.
        try:
            response = plain_requests.get(page_url, headers=HEADERS, timeout=25)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, "html.parser")
            for anchor in soup.select("a.variant[href]"):
                href = anchor.get("href")
                if href:
                    return _unwrap_redirect(urljoin(response.url, href))
            dynamic_link = _dynamic_download_link(response, package)
            if dynamic_link:
                return dynamic_link
        except Exception as exc:
            logging.debug("APKCombo requests fallback failed for %s %s: %s", app_name, version, exc)
    return None
