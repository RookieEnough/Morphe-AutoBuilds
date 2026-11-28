"""APKFab mirror (https://apkfab.com).

APKFab keeps a version history per app and serves direct downloads via
signed d.apkfab.com URLs. The site is Cloudflare-protected, so page fetches
go through FlareSolverr first (as with APKPure), falling back to direct.

URL structure:
  App page:      https://apkfab.com/{slug}/{package}
  Versions page: https://apkfab.com/{slug}/{package}/versions
  Download page: https://apkfab.com/{slug}/{package}/download?sha1={sha1}
  Final file:    https://d.apkfab.com/get-download?... (signed, fetch fresh)
"""

import logging
import re
from typing import Dict, Optional
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup

from src import session, flaresolverr, utils

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://apkfab.com/",
}


def _fetch(url: str):
    """Fetch an APKFab page, via FlareSolverr first (Cloudflare), then direct."""
    try:
        res = flaresolverr.get_with_bypass(url, session=session, headers=HEADERS, timeout=60)
        if res is not None and getattr(res, "status_code", 0) == 200 and res.content:
            return res
    except Exception as e:
        logging.debug(f"APKFab FlareSolverr fetch failed for {url}: {e}")
    try:
        res = session.get(url, headers=HEADERS, timeout=20)
        if res.status_code == 200 and res.content:
            return res
        logging.debug(f"APKFab direct fetch for {url} returned {res.status_code}")
    except Exception as e:
        logging.debug(f"APKFab direct fetch failed for {url}: {e}")
    return None


def _app_urls(config: Dict) -> list[str]:
    """Return ordered unique APKFab app URLs from primary slug and synthesized aliases."""
    package = (config.get("package") or "").strip()
    if not package:
        return []
    slugs = [config.get("slug"), config.get("name"), *(config.get("aliases") or [])]
    return [
        f"https://apkfab.com/{quote(str(s).strip(), safe='')}/{package}"
        for s in dict.fromkeys(str(s).strip() for s in slugs if s and str(s).strip())
    ]


def _app_url(config: Dict) -> Optional[str]:
    urls = _app_urls(config)
    return urls[0] if urls else None


def _versions_match(found: str, requested: str) -> bool:
    """Compare an APKFab version string against the requested version."""
    if not found or not requested:
        return False
    f, r = found.strip(), requested.strip()
    if f == r:
        return True
    # Strip common suffixes APKFab/pipeline add (release tags, arch, variant)
    clean = lambda v: re.sub(r"[-_](release|arm64-v8a|armeabi-v7a|x86_64|x86|universal|apk|xapk).*$", "", v, flags=re.IGNORECASE).strip("-_. ")
    if clean(f) == clean(r) and clean(f):
        return True
    fn, rn = utils.normalize_version(f), utils.normalize_version(r)
    return bool(fn) and bool(rn) and fn == rn


def get_latest_version(app_name: str, config: Dict) -> Optional[str]:
    url = _app_url(config)
    if not url:
        return None
    res = _fetch(url)
    if not res:
        return None
    soup = BeautifulSoup(res.content, "html.parser")
    # 1. <title>: "SnoreLab : Record Your Snoring APK 2.33.2.7665 for Android ..."
    if soup.title and soup.title.string:
        m = re.search(r"APK ([\d.]+) for Android", soup.title.string)
        if m:
            return m.group(1)
    # 2. Banner span
    banner = soup.select_one('.detail_banner span[style*="#0284fe"]')
    if banner:
        return banner.get_text(strip=True).lstrip("v")
    # 3. First version-history entry == latest
    first = soup.select_one(".version_history .list .package_info .title .version")
    if first:
        return first.get_text(strip=True)
    return None


def get_download_link(version: str, app_name: str, config: Dict) -> Optional[str]:
    bases = _app_urls(config)
    if not bases:
        return None

    # The versions page lists every version with its download sha1.
    # The app page also embeds a version history, so fall back to it.
    base = None
    soup = None
    for candidate_base in bases:
        for page_url in (candidate_base + "/versions", candidate_base):
            res = _fetch(page_url)
            if res:
                candidate_soup = BeautifulSoup(res.content, "html.parser")
                if candidate_soup.select(".version_history > div.list"):
                    base = candidate_base
                    soup = candidate_soup
                    break
        if soup is not None:
            break
    if soup is None or base is None:
        logging.warning(f"APKFab: could not load pages for {app_name}")
        return None

    target_page_url = None
    target_sha1 = None
    for entry in soup.select(".version_history > div.list"):
        ver_el = entry.select_one(".package_info .title .version")
        if not ver_el:
            continue
        if _versions_match(ver_el.get_text(strip=True), version):
            link = entry.select_one('a[href*="/download?sha1="]')
            if link and link.get("href"):
                m = re.search(r"sha1=([a-f0-9]{40})", link["href"])
                if m:
                    target_sha1 = m.group(1)
                    target_page_url = urljoin(base + "/", link["href"])
                    break
    if not target_sha1 or not target_page_url:
        logging.warning(f"Version {version} not found on APKFab for {app_name}")
        return None

    # The /download?sha1= page issues a signed, expiring d.apkfab.com URL.
    # For some legacy versions APKFab ignores ?sha1= and renders the latest
    # version's download button without sha1= in its query string; verify
    # either the sha1 matches or the advertised version matches requested.
    res = _fetch(target_page_url)
    if not res:
        return None
    soup = BeautifulSoup(res.content, "html.parser")
    page_title = soup.title.get_text(" ", strip=True) if soup.title else ""

    def _is_valid_download_href(href: str) -> bool:
        if "d.apkfab.com/get-download" not in href:
            return False
        if f"sha1={target_sha1}" in href:
            return True
        if "sha1=" in href:
            return False
        # Without sha1= in URL, APKFab is serving a generic/latest link:
        # only accept if the page title does not name a conflicting version.
        title_vers = re.findall(r"\b(\d+(?:\.\d+)+)\b", page_title)
        if title_vers and not any(_versions_match(v, version) for v in title_vers):
            logging.warning(
                f"APKFab download page for {app_name} v{version} redirected to {title_vers[0]}; skipping mismatched version"
            )
            return False
        return True

    final = soup.select_one("main.dt-download-page .download_button_box a.down_btn.download_blue_button")
    if final and final.get("href") and _is_valid_download_href(final["href"]):
        return final["href"]
    for a in soup.find_all("a", href=True):
        if _is_valid_download_href(a["href"]):
            return a["href"]
    logging.warning(f"APKFab: no matching download URL for {app_name} v{version}")
    return None
