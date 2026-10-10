import base64
import json
import logging
import re
from typing import Dict, List, Optional, Tuple
from bs4 import BeautifulSoup
from src import session, flaresolverr, utils

BASE_URL = "https://ws75.aptoide.com/api/7/"

# Config names whose aptoide.com website slug differs from the config name.
_APTOIDE_SLUG_OVERRIDES = {
    "mirinae-learn-korean-with-ai": "mirinae",
    "soccer-scores-and-sports-livescore-sofascore": "sofascore-live-score",
    "snorelab-record-your-snoring": "snorelab",
}


def _safe_get_json(url: str) -> Optional[dict]:
    """Fetch JSON from Aptoide, returning None (with a warning) on any failure
    instead of raising."""
    try:
        res = session.get(url, timeout=20)
        res.raise_for_status()
        return res.json()
    except Exception as e:
        logging.debug(f"Aptoide request failed ({url}): {e}")
        return None


def _website_slug(config: Dict) -> str:
    name = (config.get('slug') or config.get('name') or '').strip().lower()
    return _APTOIDE_SLUG_OVERRIDES.get(name, name)


def _fetch_website_versions(slug: str) -> Optional[Tuple[List[Tuple[str, int]], Optional[str]]]:
    """Scrape the Aptoide website for current versions.

    The ws75 API backend is stale/decoupled from aptoide.com for many apps,
    so the website's own version list is the reliable source.
    Returns ([(vername, vercode), ...], latest_download_path_or_None).
    Only the latest version carries a direct download path on the page.
    """
    if not slug:
        return None
    url = f"https://{slug}.en.aptoide.com/versions"
    html = None
    try:
        res = session.get(url, timeout=20)
        if res.status_code == 200 and b"vername" in res.content:
            html = res.content
        else:
            logging.info(f"Aptoide website direct fetch for {slug} returned {res.status_code}; trying FlareSolverr")
    except Exception as e:
        logging.info(f"Aptoide website direct fetch for {slug} failed ({e}); trying FlareSolverr")
    if html is None:
        try:
            fres = flaresolverr.get_with_bypass(url, session=session, timeout=45)
            if fres is not None and getattr(fres, "status_code", 0) == 200 and b"vername" in fres.content:
                html = fres.content
                logging.info(f"Aptoide website scrape for {slug} succeeded via FlareSolverr")
        except Exception as e:
            logging.info(f"Aptoide website FlareSolverr fetch for {slug} failed: {e}")
    if html is None:
        logging.info(f"Aptoide website scrape found no version data for {slug}")
        return None
    try:
        soup = BeautifulSoup(html, "html.parser")
        for script in soup.find_all("script"):
            txt = script.string
            if txt and '"vername"' in txt and txt.strip().startswith('{"props"'):
                data = json.loads(txt)
                vers = data["props"]["pageProps"]["versions"]
                versions = [(v["vername"], v["vercode"]) for v in vers if v.get("vername")]
                if not versions:
                    return None
                path = None
                m = re.search(r'"file":\s*\{[^}]*"path":\s*"([^"]+)"', json.dumps(data))
                if m:
                    path = m.group(1)
                logging.info(f"Aptoide website: {slug} has {len(versions)} versions, latest {versions[0][0]}")
                return versions, path
    except Exception as e:
        logging.debug(f"Aptoide website parse failed for {slug}: {e}")
    return None


def _version_matches(vname: str, version: str) -> bool:
    """Check whether an Aptoide vername matches a requested version."""
    clean = lambda v: re.sub(r'[\(\[].*?[\)\]]', '', v or '').strip()
    clean_entry = clean(vname)
    clean_target = clean(version)
    entry_norm = utils.normalize_version(vname)
    target_norm = utils.normalize_version(version)
    return (
        vname == version
        or clean_entry == clean_target
        or clean_entry == version
        or vname == clean_target
        or (bool(entry_norm) and bool(target_norm) and entry_norm == target_norm)
    )


def get_latest_version(app_name: str, config: Dict) -> Optional[str]:
    # 1. Try the website first (the API backend is stale for many apps).
    slug = _website_slug(config)
    if slug:
        result = _fetch_website_versions(slug)
        if result and result[0]:
            return result[0][0][0]

    # 2. Fall back to the API.
    package = config.get('package', '')
    if not package:
        return None
    arch = config.get('arch', 'universal')
    q = _get_q_param(arch)

    # 1. Try listAppVersions first (direct exact package lookup)
    url_versions = f"{BASE_URL}listAppVersions?package_name={package}&limit=1{q}"
    data = _safe_get_json(url_versions) or {}
    items = data.get("list") or (((data.get("datalist") or {}).get("list")) or [])
    for it in items:
        if it.get("package") == package and it.get("file", {}).get("vername"):
            return it["file"]["vername"]

    # 2. Fallback to apps/search filtered by package
    url = f"{BASE_URL}apps/search?query={package}&limit=10&trusted=true{q}"
    data = _safe_get_json(url) or {}
    items = (((data.get("datalist") or {}).get("list")) or data.get("list") or [])
    for app in items:
        if app.get("package") == package and app.get("file", {}).get("vername"):
            return app["file"]["vername"]

    logging.warning(f"No Aptoide result for {package}")
    return None


def get_download_link(version: str, app_name: str, config: Dict) -> Optional[str]:
    # Strip variant suffix from version for matching (e.g., "18.0.3.954559732-release-arm64-v8a" -> "18.0.3.954559732")
    import re
    base_version = re.match(r'^(\d[\d.]*\d)', version)
    if base_version:
        version = base_version.group(1)
    # Exclude beta variants if configured
    exclude = config.get('exclude_variant', '')
    # 1. Try the website first (the API backend is stale for many apps).
    slug = _website_slug(config)
    if slug:
        result = _fetch_website_versions(slug)
        if result:
            versions, latest_path = result
            for vername, _vercode in versions:
                if exclude and exclude.lower() in vername.lower():
                    continue
                if _version_matches(vername, version):
                    # Only the latest version carries a direct download path
                    # on the website; older versions have no URL without their
                    # file md5, which the page does not expose.
                    if vername == versions[0][0] and latest_path:
                        logging.info(f"Aptoide website download link for {config.get('package', '')} v{version}")
                        return latest_path
                    logging.debug(
                        f"Aptoide website lists v{version} for {slug} but exposes "
                        f"no download URL for it")
                    break

    # 2. Fall back to the API.
    package = config.get('package', '')
    if not package:
        return None
    arch = config.get('arch', 'universal')
    q = _get_q_param(arch)

    # Find vercode for specific version (search up to 100 versions)
    url_versions = f"{BASE_URL}listAppVersions?package_name={package}&limit=100{q}"
    data = _safe_get_json(url_versions) or {}
    items = data.get("list") or (((data.get("datalist") or {}).get("list")) or [])
    items = [it for it in items if it.get("package") == package]

    vercode = None

    if version and version.lower() != "latest":
        for app in items:
            try:
                vname = app["file"]["vername"].strip()
                if _version_matches(vname, version):
                    vercode = app["file"]["vercode"]
                    break
            except (KeyError, TypeError):
                continue

    # Only fall back to Aptoide's latest when no specific version was requested.
    # Silently substituting a different version for a requested one produces
    # APKs the patches were never made for (and mislabels the output), so a
    # miss here must return None and let the next source try.
    if not vercode and (not version or version.lower() == "latest") and items:
        try:
            vercode = items[0]["file"]["vercode"]
            logging.info(f"Using latest Aptoide version {items[0]['file']['vername']} for {package}")
        except (KeyError, TypeError):
            pass

    if not vercode:
        logging.warning(f"Version {version} not found on Aptoide for {package}")
        return None

    # Get meta with download path
    url_meta = f"{BASE_URL}getAppMeta?package_name={package}&vercode={vercode}{q}"
    data = _safe_get_json(url_meta) or {}
    try:
        return data["data"]["file"]["path"]
    except (KeyError, TypeError):
        logging.warning(f"Aptoide meta missing download path for {package}@{vercode}")
        return None


def _get_q_param(arch: str) -> str:
    if arch == 'universal':
        return ''
    cpu_map = {
        'arm64-v8a': 'arm64-v8a,armeabi-v7a,armeabi',
        'armeabi-v7a': 'armeabi-v7a,armeabi',
        # Add others as needed
    }
    cpu = cpu_map.get(arch, '')
    if cpu:
        q_str = f"myCPU={cpu}&leanback=0"
        return f"&q={base64.b64encode(q_str.encode('utf-8')).decode('utf-8')}"
    return ''
