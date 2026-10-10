"""Codeberg (Forgejo) release asset downloader.

Used for hosting stock APKs that can't be reliably fetched from mirrors
due to Cloudflare protection (e.g. Facebook from APKMirror).
"""

import logging
import re
from src import session


def _api_base(config: dict) -> str:
    domain = config.get("domain", "codeberg.org").strip("/")
    return f"https://{domain}/api/v1"


def get_latest_version(app_name: str, config: dict) -> str | None:
    repo = config.get("repo")
    tag = config.get("tag")
    if not repo or not tag:
        logging.error(f"Missing 'repo' or 'tag' in codeberg config for {app_name}")
        return None

    url = f"{_api_base(config)}/repos/{repo}/releases/tags/{tag}"
    try:
        response = session.get(url, timeout=20)
        if response.status_code == 200:
            data = response.json()
            versions = []
            for asset in data.get("assets", []):
                name = asset.get("name", "")
                m = re.search(r"-([\d\.]+)-", name)
                if m:
                    versions.append(m.group(1).strip("."))
                else:
                    m = re.search(r"([\d\.]+)", name)
                    if m:
                        versions.append(m.group(1).strip("."))
            if versions:
                versions.sort(key=lambda x: [int(p) for p in x.split('.') if p.isdigit()])
                logging.info(f"Latest version found on Codeberg for {app_name}: {versions[-1]}")
                return versions[-1]
        elif response.status_code == 404:
            logging.debug(f"Codeberg release not found for {url}")
        else:
            response.raise_for_status()
    except Exception as e:
        logging.error(f"Failed to fetch Codeberg release for {app_name}: {e}")
    return None


def get_download_link(version: str, app_name: str, config: dict) -> str | None:
    repo = config.get("repo")
    tag = config.get("tag")
    if not repo or not tag:
        return None

    url = f"{_api_base(config)}/repos/{repo}/releases/tags/{tag}"
    try:
        response = session.get(url, timeout=20)
        if response.status_code == 200:
            data = response.json()
            keyword = (config.get("releaseKeyword") or "apk").lower()
            arch = config.get("arch", "arm64-v8a").lower()

            candidates = []
            for asset in data.get("assets", []):
                name = asset.get("name", "")
                if keyword not in name.lower():
                    continue
                # Prefer arch-matching assets
                score = 0
                if arch in name.lower():
                    score += 10
                candidates.append((score, name, asset.get("browser_download_url")))

            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                best = candidates[0]
                logging.info(f"Codeberg download for {app_name}: {best[1]}")
                return best[2]
            logging.warning(f"No Codeberg asset matching '{keyword}' for {app_name}")
    except Exception as e:
        logging.error(f"Failed to get Codeberg download link for {app_name}: {e}")
    return None
