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
from urllib.parse import quote

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


def _app_url(config: Dict) -> Optional[str]:
    slug = (config.get("slug") or config.get("name") or "").strip()
    package = (config.get("package") or "").strip()
    if not slug or not package:
        return None
    return f"https://apkfab.com/{quote(slug, safe='')}/{package}"


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
    base = _app_url(config)
    if not base:
        return None

    # The versions page lists every version with its download sha1.
    # The app page also embeds a version history, so fall back to it.
    soup = None
    for page_url in (base + "/versions", base):
        res = _fetch(page_url)
        if res:
            soup = BeautifulSoup(res.content, "html.parser")
            if soup.select(".version_history > div.list"):
                break
    if soup is None:
        logging.warning(f"APKFab: could not load pages for {app_name}")
        return None

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
                    break
    if not target_sha1:
        logging.warning(f"Version {version} not found on APKFab for {app_name}")
        return None

    # The /download?sha1= page issues a signed, expiring d.apkfab.com URL.
    # Fetch it fresh every time; do not trust its displayed version text for
    # old versions (it can show the latest version's metadata).
    res = _fetch(f"{base}/download?sha1={target_sha1}")
    if not res:
        return None
    soup = BeautifulSoup(res.content, "html.parser")
    final = soup.select_one("main.dt-download-page .download_button_box a.down_btn.download_blue_button")
    if final and final.get("href") and "d.apkfab.com" in final["href"]:
        return final["href"]
    for a in soup.find_all("a", href=True):
        if "d.apkfab.com/get-download" in a["href"]:
            return a["href"]
    logging.warning(f"APKFab: no final download URL for {app_name} v{version}")
    return None
