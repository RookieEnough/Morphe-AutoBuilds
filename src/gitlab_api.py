"""GitLab API helpers for the Morphe-AutoBuilds mirror.

Uses the GitLab Releases API + Generic Package Registry to replicate the
GitHub 'latest' release flow:

- Built APKs are uploaded to the generic package ``morphe-apks`` (version
  ``latest``); re-uploading the same filename overwrites it.
- The ``latest`` Git tag is moved to the current commit each publish and the
  ``latest`` Release is created/updated with asset links pointing at the
  package files.

Auth: ``GITLAB_RELEASE_TOKEN`` (project access token with ``api`` scope).
Project: ``CI_PROJECT_ID`` (auto-provided by GitLab CI).
"""
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

PACKAGE_NAME = "morphe-apks"
PACKAGE_VERSION = "latest"
RELEASE_TAG = "latest"

_log = logging.getLogger(__name__)

# Retry transient failures (5xx, 429, network errors)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 3


def _base() -> str:
    return os.environ.get("CI_API_V4_URL", "https://gitlab.com/api/v4").rstrip("/")


def _project() -> str:
    pid = os.environ.get("GITLAB_PROJECT_ID") or os.environ.get("CI_PROJECT_ID")
    if not pid:
        raise RuntimeError("GITLAB_PROJECT_ID (or CI_PROJECT_ID) is not set")
    return pid


def _headers() -> Dict[str, str]:
    token = os.environ.get("GITLAB_RELEASE_TOKEN") or os.environ.get("GITLAB_TOKEN")
    if not token:
        raise RuntimeError(
            "No GitLab token: set GITLAB_RELEASE_TOKEN (project access token, api scope)"
        )
    return {"PRIVATE-TOKEN": token}


def api(method: str, path: str, **kwargs: Any) -> requests.Response:
    url = f"{_base()}/projects/{_project()}{path}"
    headers = _headers()
    headers.update(kwargs.pop("headers", {}))
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.request(method, url, headers=headers, timeout=120, **kwargs)
            if r.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES - 1:
                wait = (attempt + 1) * 10
                _log.warning(f"GitLab {r.status_code} on {method} {path}, retrying in {wait}s (attempt {attempt+1})")
                time.sleep(wait)
                continue
            return r
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_exc = e
            if attempt < MAX_RETRIES - 1:
                wait = (attempt + 1) * 10
                _log.warning(f"GitLab network error on {method} {path}: {e}, retrying in {wait}s")
                time.sleep(wait)
                continue
            raise
    # Should not reach here, but just in case
    if last_exc:
        raise last_exc
    return r


# ---------------------------------------------------------------------------
# Generic package registry
# ---------------------------------------------------------------------------

def package_file_url(filename: str,
                     package: str = PACKAGE_NAME,
                     version: str = PACKAGE_VERSION) -> str:
    return (f"{_base()}/projects/{_project()}/packages/generic/"
            f"{package}/{version}/{filename}")


def safe_filename(name: str) -> str:
    """Make a filename acceptable to the GitLab generic package registry.

    GitLab rejects names containing characters like parentheses
    (HTTP 400 "file_name is invalid"). Keep names readable: turn '('
    into '-', drop ')', and strip anything outside [A-Za-z0-9._-].
    """
    name = name.replace("(", "-").replace(")", "")
    safe = "".join(c for c in name if c.isascii() and (c.isalnum() or c in "._-"))
    return safe or "file"


def upload_package_file(apk_path: str | Path,
                        package: str = PACKAGE_NAME,
                        version: str = PACKAGE_VERSION) -> str:
    """Upload an APK to the generic package registry. Returns the download URL."""
    apk_path = Path(apk_path)
    # GitLab rejects some filename characters (400 file_name is invalid);
    # upload under the sanitized name so the PUT succeeds.
    safe_name = safe_filename(apk_path.name)
    url = package_file_url(safe_name, package, version)
    _log.info(f"Uploading {apk_path.name} to GitLab package registry...")
    with apk_path.open("rb") as f:
        r = api("PUT", f"/packages/generic/{package}/{version}/{safe_name}",
                data=f)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Package upload failed ({r.status_code}): {r.text[:300]}")
    _log.info(f"Uploaded: {url}")
    return url


def list_package_files() -> List[Dict[str, Any]]:
    """List all files in the morphe-apks generic package."""
    files: List[Dict[str, Any]] = []
    page = 1
    while True:
        r = api("GET", f"/packages/{_package_id()}/package_files?per_page=100&page={page}")
        r.raise_for_status()
        batch = r.json()
        files.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return files


def _package_id() -> int:
    """Get the package ID for morphe-apks."""
    r = api("GET", "/packages?package_name=morphe-apks&per_page=1")
    r.raise_for_status()
    pkgs = r.json()
    if pkgs:
        return pkgs[0]["id"]
    raise RuntimeError("morphe-apks package not found")


def delete_package_file(filename: str,
                        package: str = PACKAGE_NAME,
                        version: str = PACKAGE_VERSION) -> bool:
    r = api("DELETE", f"/packages/generic/{package}/{version}/{filename}")
    if r.status_code in (200, 204):
        _log.info(f"Deleted package file {filename}")
        return True
    if r.status_code == 404:
        _log.info(f"Package file {filename} not found (already gone)")
        return True
    _log.warning(f"Could not delete package file {filename}: {r.status_code} {r.text[:200]}")
    return False


def download_package_file(filename: str, dest: str | Path,
                          package: str = PACKAGE_NAME,
                          version: str = PACKAGE_VERSION) -> Optional[Path]:
    url = package_file_url(filename, package, version)
    r = api("GET", f"/packages/generic/{package}/{version}/{filename}", stream=True)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    dest = Path(dest)
    with dest.open("wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 256):
            f.write(chunk)
    return dest


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------

def move_tag(tag: str, ref: str) -> None:
    """Point (or create) a tag at ref, deleting the old one first."""
    r = api("DELETE", f"/repository/tags/{tag}")
    if r.status_code not in (200, 204, 404):
        raise RuntimeError(f"Could not delete tag {tag}: {r.status_code} {r.text[:200]}")
    r = api("POST", "/repository/tags", json={"tag_name": tag, "ref": ref})
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Could not create tag {tag}: {r.status_code} {r.text[:200]}")
    _log.info(f"Tag '{tag}' now points at {ref[:8]}")


def ensure_release(tag: str, name: str, description: str, ref: str) -> Dict[str, Any]:
    """Create the release if missing, else update its name/description."""
    move_tag(tag, ref)
    r = api("GET", f"/releases/{tag}")
    if r.status_code == 200:
        r = api("PUT", f"/releases/{tag}",
                json={"name": name, "description": description})
        r.raise_for_status()
        _log.info(f"Updated release '{tag}'")
        return r.json()
    r = api("POST", "/releases",
            json={"name": name, "tag_name": tag, "description": description, "ref": ref})
    r.raise_for_status()
    _log.info(f"Created release '{tag}'")
    return r.json()


def list_asset_links(tag: str, retries: int = 3) -> List[Dict[str, Any]]:
    # The /releases/{tag}/assets/links endpoint is broken (returns []).
    # Get links from the release object instead, which includes them.
    # GitLab's API has consistency lag: right after a release is updated,
    # GET /releases/{tag} may return stale data with 0 links. Retry with
    # backoff before falling back, to avoid acting on incomplete data.
    for attempt in range(retries):
        try:
            r = api("GET", f"/releases/{tag}")
            if r.status_code == 404:
                return []
            r.raise_for_status()
            release = r.json()
            links = release.get("assets", {}).get("links", [])
            if links:
                return links
            # Empty links: might be consistency lag, retry unless last attempt
            if attempt < retries - 1:
                _log.warning(
                    f"Got 0 asset links for '{tag}' (attempt {attempt + 1}/{retries}), "
                    f"retrying after delay (possible API consistency lag)"
                )
                time.sleep(2 * (attempt + 1))
        except Exception as e:
            if attempt < retries - 1:
                _log.warning(f"list_asset_links attempt {attempt + 1} failed: {e}, retrying")
                time.sleep(2 * (attempt + 1))
            pass
    # Fallback to the direct endpoint
    links = []
    page = 1
    while True:
        r = api("GET", f"/releases/{tag}/assets/links?per_page=100&page={page}")
        if r.status_code == 404:
            return links
        r.raise_for_status()
        batch = r.json()
        links.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return links


def replace_asset_links(tag: str, assets: List[tuple]) -> None:
    """Replace all asset links on a release. assets = [(name, url), ...]."""
    for link in list_asset_links(tag):
        r = api("DELETE", f"/releases/{tag}/assets/links/{link['id']}")
        if r.status_code not in (200, 204, 404):
            _log.warning(f"Could not delete asset link {link['name']}: {r.status_code}")
    for name, url in assets:
        r = api("POST", f"/releases/{tag}/assets/links",
                json={"name": name, "url": url, "link_type": "package"})
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Could not create asset link {name}: {r.status_code} {r.text[:200]}")
    _log.info(f"Set {len(assets)} asset links on release '{tag}'")


def delete_asset_link(tag: str, link_id: int) -> bool:
    r = api("DELETE", f"/releases/{tag}/assets/links/{link_id}")
    return r.status_code in (200, 204, 404)
