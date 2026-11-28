"""APKPure mirror (https://apkpure.net).

Primary path: APKPure's official app API (tapi.pureapk.com), which needs no
Cloudflare bypass. Falls back to FlareSolverr page scraping for older
versions the API does not list (it returns only the ~20 latest).

API: GET https://tapi.pureapk.com/v3/get_app_his_version
     params: package_name, hl=en
     returns: version_list[] with version_name and asset.urls[0] (direct link)
"""

import json
import logging
import re
import time

from bs4 import BeautifulSoup

from src import session, flaresolverr, utils

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://apkpure.net/'
}

# Mimics the APKPure Android app so tapi.pureapk.com serves us like a client.
API_URL = "https://tapi.pureapk.com/v3/get_app_his_version"
_BASE_DEVICE_INFO = {
    "android_id": "50f838123d9a9c94",
    "brand": "google",
    "country": "United States",
    "country_code": "US",
    "imei": "",
    "language": "en-US",
    "manufacturer": "Google",
    "mode": "Pixel 5",
    "os_ver": "33",
    "os_ver_name": "13",
    "platform": 1,
    "product": "redfin",
    "screen_height": 1080,
    "screen_width": 1920,
}
_HOST_APP_INFO = {
    "build_no": "468",
    "channel": "",
    "md5": "6756e53158d6f6c013650a40d8f1147b",
    "pkg_name": "com.apkpure.aegon",
    "sdk_ver": "3.20.34",
    "version_code": 3203427,
    "version_name": "3.20.34",
}


def _api_headers(abis: list[str] | None = None) -> dict:
    """Build APKPure API headers tailored to the requested native ABIs."""
    payload = {
        "device_info": {
            "abis": abis or ["arm64-v8a"],
            **_BASE_DEVICE_INFO,
        },
        "host_app_info": _HOST_APP_INFO,
    }
    return {
        "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 13; Pixel 5 Build/TQ3A.230901.001); APKPure/3.20.34 (Aegon)",
        "Ual-Access-Businessid": "projecta",
        "Ual-Access-ProjectA": json.dumps(payload, separators=(",", ":")),
        "Connection": "Keep-Alive",
        "Accept-Encoding": "gzip",
    }


API_HEADERS = _api_headers(["x86_64", "arm64-v8a", "x86", "armeabi-v7a", "armeabi"])


def _target_arch(config) -> str:
    if isinstance(config, dict):
        return (config.get("arch") or "universal").strip().lower()
    return "universal"


def _abi_queries_for_config(config) -> list[list[str]]:
    arch = _target_arch(config)
    if arch in ("armeabi-v7a", "x86", "x86_64"):
        return [[arch]]
    return [["arm64-v8a"], ["arm64-v8a", "armeabi-v7a"]]


def _api_versions(package: str, abis: list[str] | None = None):
    """Return the API's version list for a package, or None on failure."""
    for attempt in range(2):
        try:
            res = session.get(
                API_URL,
                headers=_api_headers(abis),
                params={"package_name": package, "hl": "en"},
                timeout=20,
            )
            if res.status_code == 200:
                data = res.json()
                versions = data.get("version_list") or []
                if versions:
                    logging.info(f"APKPure API: {len(versions)} versions for {package}")
                return versions or None
            logging.debug(f"APKPure API returned {res.status_code} for {package}")
            if attempt == 0 and res.status_code in (429, 500, 502, 503, 504):
                time.sleep(2)
                continue
            return None
        except Exception as e:
            logging.debug(f"APKPure API failed for {package}: {e}")
            if attempt == 0:
                time.sleep(2)
                continue
            return None
    return None


_ARCH_TAG_RE = re.compile(r"(arm64|armeabi|armv7|arm7|x86_64|x86)", re.IGNORECASE)


def _extract_arch_tag(value: str) -> str | None:
    match = _ARCH_TAG_RE.search(value or "")
    if not match:
        return None
    tag = match.group(1).lower()
    if tag in ("armv7", "arm7"):
        return "armeabi"
    return tag


def _entry_matches_arch(entry: dict, target_arch: str) -> bool:
    """Reject entries whose native_code conflicts with the target ABI."""
    native = {utils._normalize_abi(a) for a in (entry.get("native_code") or []) if a}
    if not native:
        return True
    norm_target = utils._normalize_abi(target_arch or "universal")
    if norm_target in ("universal", "arm64-v8a", "noarch", ""):
        return "arm64-v8a" in native
    return norm_target in native


def _version_name_matches(name: str, version: str) -> bool:
    if not name:
        return False
    if name == version.strip():
        return True
    name_tag = _extract_arch_tag(name)
    ver_tag = _extract_arch_tag(version)
    if name_tag and ver_tag and name_tag != ver_tag:
        return False
    fn, rn = utils.normalize_version(name), utils.normalize_version(version)
    return bool(fn) and bool(rn) and fn == rn


def _api_match(versions, version: str, target_arch: str = "universal"):
    """Find the API entry matching the requested version and architecture.

    Prefers entries that actually have download URLs, since some API entries
    list a version without usable assets.
    """
    matches = [
        v for v in versions
        if _version_name_matches((v.get("version_name") or "").strip(), version)
        and _entry_matches_arch(v, target_arch)
    ]
    for v in matches:
        if _api_download_url(v):
            return v
    return matches[0] if matches else None


def get_download_links(version: str, app_name: str, config: str) -> list[str]:
    """Return direct download URLs for matching ABI variants of this version."""
    package = config.get("package", "")
    target_arch = _target_arch(config)
    for abis in _abi_queries_for_config(config):
        versions = _api_versions(package, abis=abis)
        if not versions:
            continue
        urls: list[str] = []
        all_standalone_apk = True
        for v in versions:
            if not (
                _version_name_matches((v.get("version_name") or "").strip(), version)
                and _entry_matches_arch(v, target_arch)
            ):
                continue
            dl = _api_download_url(v)
            if dl and dl not in urls:
                asset_type = ((v.get("asset") or {}).get("type") or "").upper()
                if asset_type != "APK":
                    all_standalone_apk = False
                urls.append(dl)
        if urls:
            # Standalone APKs cannot be meaningfully combined into an XAPK split bundle.
            if all_standalone_apk and len(urls) > 1:
                urls = [urls[0]]
            logging.info(f"APKPure API: {len(urls)} variant download(s) for {app_name} v{version}")
            return urls
    return []


def _api_download_url(entry) -> str | None:
    urls = (entry.get("asset") or {}).get("urls") or []
    return urls[0] if urls else None


def get_latest_version(app_name: str, config: str) -> str:
    package = config.get("package", "")
    target_arch = _target_arch(config)

    # API first: no Cloudflare, no FlareSolverr needed.
    for abis in _abi_queries_for_config(config):
        versions = _api_versions(package, abis=abis)
        if not versions:
            continue
        for entry in versions:
            if not _entry_matches_arch(entry, target_arch):
                continue
            latest = (entry.get("version_name") or "").strip()
            if latest:
                return latest

    # Fallback: scrape the versions page via FlareSolverr.
    url = f"https://apkpure.net/{config['name']}/{config['package']}/versions"
    try:
        response = flaresolverr.get_with_bypass(url, session=session, headers=HEADERS, timeout=60) or session.get(url, headers=HEADERS)
        response.raise_for_status()

        content_size = len(response.content)
        logging.info(f"URL:{response.url} [{content_size}/{content_size}] -> \"-\" [1]")

        soup = BeautifulSoup(response.content, "html.parser")
        version_info = soup.find('div', class_='ver-top-down')

        if version_info and 'data-dt-version' in version_info.attrs:
            return version_info['data-dt-version']

    except Exception as e:
        logging.error(f"Failed to fetch latest version for {app_name}: {e}")

    return None


def get_download_link(version: str, app_name: str, config: str) -> str:
    package = config.get("package", "")

    # API first: direct download URL, no Cloudflare.
    target_arch = _target_arch(config)
    for abis in _abi_queries_for_config(config):
        versions = _api_versions(package, abis=abis)
        if not versions:
            continue
        entry = _api_match(versions, version, target_arch=target_arch)
        if entry:
            dl = _api_download_url(entry)
            if dl:
                logging.info(f"APKPure API download link for {app_name} v{version}")
                return dl
    logging.info(f"APKPure API has no downloadable {version} for {app_name}; trying FlareSolverr fallback")

    # Fallback: scrape the download page via FlareSolverr (for older versions).
    url = f"https://apkpure.net/{config['name']}/{config['package']}/download/{version}"

    try:
        response = flaresolverr.get_with_bypass(url, session=session, headers=HEADERS, timeout=60) or session.get(url, headers=HEADERS)
        response.raise_for_status()

        content_size = len(response.content)
        logging.info(f"URL:{response.url} [{content_size}/{content_size}] -> \"-\" [1]")

        soup = BeautifulSoup(response.content, "html.parser")

        # 1) Common direct download anchor (id=download_link)
        download_link = soup.find('a', id='download_link')
        if download_link and download_link.get('href'):
            href = download_link['href']
            # Return any d.apkpure.com link or XAPK-like link
            if 'd.apkpure.com' in href or '/b/XAPK/' in href or href.lower().endswith(('.xapk', '.zip')):
                return href
            # Some pages use relative or intermediate links; still return them to be followed.
            return href

        # 2) Some pages embed a 'data-href' attribute on the download button/container
        btn = soup.find(attrs={'data-href': True})
        if btn:
            href = btn['data-href']
            if href:
                return href

        # 3) Regex search for d.apkpure.com XAPK/APK delivery endpoints
        html = response.text
        match = re.search(r'href=[\'\"]?(https?://d\.apkpure\.com/b/(?:XAPK|APK)/[^\'\">]+)[\'\">]?', html, flags=re.IGNORECASE)
        if match:
            dl_url = match.group(1).replace("&amp;", "&")
            logging.info(f"APKPure direct link found via regex for {app_name}")
            return dl_url

        # 4) Regex for direct .xapk/.zip/.apk links sometimes used
        match = re.search(r'href=[\'\"]?(https?://[^\'\">]+\.(?:xapk|zip|apk))[\'\">]?', html, flags=re.IGNORECASE)
        if match:
            dl_url = match.group(1).replace("&amp;", "&")
            logging.info(f"APKPure direct link found (generic) for {app_name}")
            return dl_url

        # 5) Some pages render JSON or JS variables with download_url
        match = re.search(r'download_url["\']?\s*[:=]\s*[\'\"]([^\'\"]+)[\'\"]', html, flags=re.IGNORECASE)
        if match:
            dl_url = match.group(1).replace("&amp;", "&")
            logging.info(f"APKPure download_url found in page for {app_name}")
            return dl_url

    except Exception as e:
        logging.error(f"Failed to fetch download link for {app_name} v{version}: {e}")

    return None
