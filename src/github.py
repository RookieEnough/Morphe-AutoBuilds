import os
import re
import logging
from src import session

def _get_headers():
    headers = {}
    if "GITHUB_TOKEN" in os.environ:
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    return headers

def _extract_version_from_release(release: dict) -> str | None:
    """Extract a normalized dotted version from release tag/assets."""
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if not name.lower().endswith((".apk", ".apkm", ".xapk")):
            continue
        m = re.search(r"-([\d.]+)-", name) or re.search(r"(\d+(?:\.\d+)+)", name)
        if m:
            ver = m.group(1).strip(".")
            if ver:
                return ver
    tag_name = (release.get("tag_name") or "").strip()
    m = re.search(r"(\d+(?:\.\d+)+)", tag_name)
    return m.group(1).strip(".") if m else None


def _asset_matches_version(name: str, version: str) -> bool:
    return bool(re.search(rf"(?<![\d.]){re.escape(version)}(?![\d.])", name))


def _fetch_releases(repo: str, tag: str | None) -> list[dict]:
    """Fetch pinned tag release first (if given), otherwise recent non-draft releases."""
    releases: list[dict] = []
    if tag:
        url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
        res = session.get(url, headers=_get_headers(), timeout=20)
        if res.status_code == 200:
            releases.append(res.json())
        elif res.status_code != 404:
            res.raise_for_status()
    if not tag:
        url = f"https://api.github.com/repos/{repo}/releases?per_page=30"
        res = session.get(url, headers=_get_headers(), timeout=20)
        if res.status_code == 200:
            releases.extend(r for r in res.json() if not r.get("draft"))
        elif res.status_code != 404:
            res.raise_for_status()
    return releases


def get_latest_version(app_name: str, config: dict) -> str | None:
    repo = config.get("repo")
    tag = (config.get("tag") or "").strip()
    if not repo:
        logging.debug(f"Missing 'repo' in github config for {app_name}")
        return None

    try:
        releases = _fetch_releases(repo, tag or None)
        versions = []
        for rel in releases:
            if rel.get("prerelease") and not tag:
                continue
            ver = _extract_version_from_release(rel)
            if ver:
                versions.append(ver)
        if versions:
            versions.sort(key=lambda x: [int(p) for p in x.split(".") if p.isdigit()])
            logging.info(f"Latest version found on GitHub for {app_name}: {versions[-1]}")
            return versions[-1]
    except Exception as e:
        logging.error(f"Failed to fetch GitHub release for {app_name}: {e}")
    return None


def get_download_link(version: str, app_name: str, config: dict) -> str | None:
    repo = config.get("repo")
    tag = (config.get("tag") or "").strip()
    if not repo or not version:
        return None

    try:
        releases = _fetch_releases(repo, tag or None)
        # If pinned tag did not match the requested version, also search repo releases.
        if tag:
            pinned_has_match = any(
                _asset_matches_version(a.get("name", ""), version)
                and a.get("name", "").lower().endswith((".apk", ".apkm", ".xapk"))
                for rel in releases
                for a in rel.get("assets", [])
            )
            if not pinned_has_match:
                releases.extend(_fetch_releases(repo, None))

        arch = (config.get("arch") or "universal").lower()
        arch_tokens = ["universal", "noarch"] if arch in ("all", "both", "universal") else [arch]

        for rel in releases:
            assets = [
                a for a in rel.get("assets", [])
                if a.get("name", "").lower().endswith((".apk", ".apkm", ".xapk"))
                and _asset_matches_version(a.get("name", ""), version)
            ]
            if not assets:
                continue
            for a in assets:
                lower = a.get("name", "").lower()
                if arch in ("all", "both") or any(tok in lower for tok in arch_tokens):
                    logging.info(f"Found GitHub download link for {app_name} {version}")
                    return a.get("browser_download_url")
            # Accept standalone universal asset whose filename has no explicit arch marker.
            for a in assets:
                lower = a.get("name", "").lower()
                if not any(m in lower for m in ("armeabi-v7a", "arm64-v8a", "x86_64", "x86")) or arch == "universal":
                    logging.info(f"Found GitHub asset for {app_name} {version}: {a.get('name')}")
                    return a.get("browser_download_url")
    except Exception as e:
        logging.error(f"Failed to get GitHub download link for {app_name}: {e}")

    return None
