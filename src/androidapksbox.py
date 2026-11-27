"""AndroidAPKsBox fallback downloader for older standalone APKs."""

import logging
import re
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup

from src import session, utils

BASE_URL = "https://www.androidapksbox.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{BASE_URL}/",
}


def _slug_candidates(app_name: str, config: dict) -> list[str]:
    """Return ordered unique AndroidAPKsBox URL slugs to probe."""
    candidates = [
        config.get("name"),
        app_name,
        *(config.get("aliases") or []),
        config.get("org"),
    ]
    return list(dict.fromkeys(str(c).strip().lower() for c in candidates if c and str(c).strip()))


def _search_app_url(app_name: str, config: dict) -> str | None:
    """Discover an AndroidAPKsBox app page URL via site search."""
    queries = []
    for cand in _slug_candidates(app_name, config):
        queries.append(cand.replace("-", " "))
    for query in list(dict.fromkeys(q for q in queries if q))[:2]:
        try:
            res = session.get(f"{BASE_URL}/?s={quote(query)}", headers=HEADERS, timeout=20)
            if res.status_code != 200 or not res.content:
                continue
            soup = BeautifulSoup(res.content, "html.parser")
            tokens = {t for t in re.split(r"[-_\s]+", query.lower()) if len(t) >= 3}
            for anchor in soup.select("div.app-title a[href]"):
                href = anchor.get("href", "")
                title = anchor.get_text(" ", strip=True).lower()
                if any(tok in title or tok in href.lower() for tok in tokens):
                    return urljoin(BASE_URL, href)
        except Exception as e:
            logging.debug(f"AndroidAPKsBox search failed for '{query}': {e}")
    return None


def _fetch_app_soup(app_name: str, config: dict) -> tuple[str | None, BeautifulSoup | None]:
    """Fetch and parse the main app page for the first matching slug or search hit."""
    for slug in _slug_candidates(app_name, config):
        url = f"{BASE_URL}/{slug}/"
        try:
            res = session.get(url, headers=HEADERS, timeout=20)
            if res.status_code == 200 and res.content:
                return url, BeautifulSoup(res.content, "html.parser")
        except Exception as e:
            logging.debug(f"AndroidAPKsBox fetch failed for {url}: {e}")
    found_url = _search_app_url(app_name, config)
    if found_url:
        try:
            res = session.get(found_url, headers=HEADERS, timeout=20)
            if res.status_code == 200 and res.content:
                return found_url, BeautifulSoup(res.content, "html.parser")
        except Exception as e:
            logging.debug(f"AndroidAPKsBox discovered fetch failed for {found_url}: {e}")
    return None, None


def _extract_apk_from_get_page(get_url: str) -> str | None:
    """Resolve an intermediate /get/(old|new)/<id>/ page to the direct .apk URL."""
    try:
        res = session.get(get_url, headers=HEADERS, allow_redirects=False, timeout=20)
        if 300 <= res.status_code < 400 and res.headers.get("location"):
            return urljoin(get_url, res.headers["location"])
        if res.status_code != 200:
            return None
        soup = BeautifulSoup(res.content, "html.parser")
        for anchor in soup.select("a[href]"):
            href = anchor.get("href", "")
            if "aabstatic.com" in href or href.lower().endswith(".apk"):
                return urljoin(get_url, href)
    except Exception as e:
        logging.debug(f"AndroidAPKsBox /get/ lookup failed for {get_url}: {e}")
    return None


def get_latest_version(app_name: str, config: dict) -> str | None:
    """Return the latest version string advertised on the AndroidAPKsBox app page."""
    _, soup = _fetch_app_soup(app_name, config)
    if not soup:
        return None
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "")
        if "/get/new/" in href:
            text = anchor.get_text(" ", strip=True)
            match = re.search(r"(\d+(?:\.\d+)+)", text)
            if match:
                return match.group(1)
    title = soup.title.string if soup.title and soup.title.string else ""
    match = re.search(r"(\d+(?:\.\d+)+)", title)
    return match.group(1) if match else None


def get_download_link(version: str, app_name: str, config: dict) -> str | None:
    """Find a direct .apk download URL for the requested version on AndroidAPKsBox."""
    if not version:
        return None
    app_url, soup = _fetch_app_soup(app_name, config)
    if not soup or not app_url:
        return None

    target_norm = utils.normalize_version(version)
    ver_dash = re.escape(version.strip().replace(".", "-"))
    ver_page_url = None

    for anchor in soup.select("a[href*='-old-apk/']"):
        href = anchor.get("href", "")
        text = anchor.get_text(" ", strip=True)
        if re.search(rf"(?:^|-){ver_dash}(?:-|$)", href, re.IGNORECASE):
            ver_page_url = urljoin(app_url, href)
            break
        match = re.search(r"(\d+(?:\.\d+)+)", text)
        if match and utils.normalize_version(match.group(1)) == target_norm:
            ver_page_url = urljoin(app_url, href)
            break

    if ver_page_url:
        try:
            ver_res = session.get(ver_page_url, headers={**HEADERS, "Referer": app_url}, timeout=20)
            if ver_res.status_code == 200:
                ver_soup = BeautifulSoup(ver_res.content, "html.parser")
                for anchor in ver_soup.select("a[href*='/get/']"):
                    return _extract_apk_from_get_page(urljoin(ver_page_url, anchor["href"]))
        except Exception as e:
            logging.debug(f"AndroidAPKsBox version page failed for {ver_page_url}: {e}")
        return None

    # Fall back to the latest /get/new/ button only when latest matches the requested version.
    latest = get_latest_version(app_name, config)
    if latest and utils.normalize_version(latest) == target_norm:
        for anchor in soup.select("a[href*='/get/new/']"):
            return _extract_apk_from_get_page(urljoin(app_url, anchor["href"]))

    return None
