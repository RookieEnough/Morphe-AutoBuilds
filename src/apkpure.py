"""APKPure mirror (https://apkpure.net).

Primary path: APKPure's official app API (tapi.pureapk.com), which needs no
Cloudflare bypass. Falls back to FlareSolverr page scraping for older
versions the API does not list (it returns only the ~20 latest).

API: GET https://tapi.pureapk.com/v3/get_app_his_version
     params: package_name, hl=en
     returns: version_list[] with version_name and asset.urls[0] (direct link)
"""

import logging
import re

from bs4 import BeautifulSoup

from src import session, flaresolverr, utils

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://apkpure.net/'
}

# Mimics the APKPure Android app so tapi.pureapk.com serves us like a client.
API_URL = "https://tapi.pureapk.com/v3/get_app_his_version"
API_HEADERS = {
    "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 13; Pixel 5 Build/TQ3A.230901.001); APKPure/3.20.34 (Aegon)",
    "Ual-Access-Businessid": "projecta",
    "Ual-Access-ProjectA": '{"device_info":{"abis":["x86_64","arm64-v8a","x86","armeabi-v7a","armeabi"],"android_id":"50f838123d9a9c94","brand":"google","country":"United States","country_code":"US","imei":"","language":"en-US","manufacturer":"Google","mode":"Pixel 5","os_ver":"33","os_ver_name":"13","platform":1,"product":"redfin","screen_height":1080,"screen_width":1920},"host_app_info":{"build_no":"468","channel":"","md5":"6756e53158d6f6c013650a40d8f1147b","pkg_name":"com.apkpure.aegon","sdk_ver":"3.20.34","version_code":3203427,"version_name":"3.20.34"}}',
    "Connection": "Keep-Alive",
    "Accept-Encoding": "gzip",
}


def _api_versions(package: str):
    """Return the API's version list for a package, or None on failure."""
    try:
        res = session.get(
            API_URL,
            headers=API_HEADERS,
            params={"package_name": package, "hl": "en"},
            timeout=20,
        )
        if res.status_code != 200:
            logging.debug(f"APKPure API returned {res.status_code} for {package}")
            return None
        data = res.json()
        versions = data.get("version_list") or []
        if versions:
            logging.info(f"APKPure API: {len(versions)} versions for {package}")
        return versions or None
    except Exception as e:
        logging.debug(f"APKPure API failed for {package}: {e}")
        return None


def _api_match(versions, version: str):
    """Find the API entry matching the requested version.

    Prefers entries that actually have download URLs, since some API entries
    list a version without usable assets.
    """
    def _name_matches(name: str) -> bool:
        if not name:
            return False
        if name == version.strip():
            return True
        fn, rn = utils.normalize_version(name), utils.normalize_version(version)
        return bool(fn) and bool(rn) and fn == rn

    # First pass: matching version WITH download URLs
    for v in versions:
        if _name_matches((v.get("version_name") or "").strip()):
            if _api_download_url(v):
                return v
    # Second pass: matching version even without URLs (caller decides)
    for v in versions:
        if _name_matches((v.get("version_name") or "").strip()):
            return v
    return None


def _api_download_url(entry) -> str | None:
    urls = (entry.get("asset") or {}).get("urls") or []
    return urls[0] if urls else None


def get_latest_version(app_name: str, config: str) -> str:
    package = config.get("package", "")

    # API first: no Cloudflare, no FlareSolverr needed.
    versions = _api_versions(package)
    if versions:
        latest = (versions[0].get("version_name") or "").strip()
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
    versions = _api_versions(package)
    if versions:
        entry = _api_match(versions, version)
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

        download_link = soup.find('a', id='download_link')
        if download_link and download_link.get('href'):
            href = download_link['href']
            if 'd.apkpure.com' in href:
                return href

        html = response.text
        match = re.search(r'href="(https://d\.apkpure\.com/b/(?:XAPK|APK)/[^"]+)"', html)
        if match:
            dl_url = match.group(1).replace("&amp;", "&")
            logging.info(f"APKPure direct link found via regex for {app_name}")
            return dl_url

    except Exception as e:
        logging.error(f"Failed to fetch download link for {app_name} v{version}: {e}")

    return None
