#!/usr/bin/env python3
"""Publish built APKs to a Codeberg 'latest' release.

Uploads every *.apk (+ manifest.json if present) in --apk-dir to the
specified Codeberg repository's release. Creates the release if missing,
uploads assets, and deletes superseded older-version APKs.

Usage:
    python scripts/codeberg_publish.py \
        --token "$CODEBERG_TOKEN" \
        --owner RookieZ \
        --repo Community-Builds \
        --tag latest \
        --apk-dir ./all-apks
"""
import argparse
import json
import logging
import os
import re
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")

API_BASE = "https://codeberg.org/api/v1"

# Same identity rule as scripts/cleanup_old_apks.py: {app}-{arch}- before -v{version}.apk
VERSION_MARKER = re.compile(r"-v\d[\d.()+\-]*\.apk$", re.IGNORECASE)


def identity_prefix(filename: str) -> str | None:
    m = VERSION_MARKER.search(filename)
    return filename[:m.start() + 1] if m else None


def api(method: str, path: str, token: str, data=None, headers=None):
    url = API_BASE + path
    body = json.dumps(data).encode() if data else None
    h = {"Authorization": f"token {token}", "Accept": "application/json"}
    if data:
        h["Content-Type"] = "application/json"
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            ct = r.headers.get("Content-Type", "")
            if "json" in ct:
                return json.load(r), r.status
            return r.read(), r.status
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")[:500]
        logging.error(f"API {method} {path} -> HTTP {e.code}: {err_body}")
        raise


def ensure_releases_enabled(owner: str, repo: str, token: str):
    """Forgejo repos can be created with the releases feature disabled
    (has_releases=false), which makes every /releases endpoint 404. The
    RookieZ/Community-Builds repo was created that way, so enable it via
    the edit-repo API before touching releases."""
    try:
        r, _ = api("GET", f"/repos/{owner}/{repo}", token)
    except Exception as e:
        logging.warning(f"could not read repo settings, skipping releases check: {e}")
        return
    if r.get("has_releases"):
        return
    logging.info("Releases are disabled on this repo; enabling via repo settings...")
    api("PATCH", f"/repos/{owner}/{repo}", token, {"has_releases": True})
    logging.info("Releases enabled on the repo.")


def get_or_create_release(owner: str, repo: str, tag: str, token: str) -> dict:
    """Get the release by tag, or create it if missing."""
    ensure_releases_enabled(owner, repo, token)
    try:
        rel, _ = api("GET", f"/repos/{owner}/{repo}/releases/tags/{tag}", token)
        logging.info(f"Found existing release '{tag}' (id={rel['id']})")
        return rel
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        logging.info(f"Release '{tag}' not found, creating...")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    rel, _ = api("POST", f"/repos/{owner}/{repo}/releases", token, {
        "tag_name": tag,
        "name": f"Morphe APKs - {now} UTC",
        "body": "Auto-built patched APKs. See assets below.",
        "draft": False,
        "prerelease": False,
    })
    logging.info(f"Created release '{tag}' (id={rel['id']})")
    return rel


def upload_asset(owner: str, repo: str, release_id: int, token: str, filepath: Path):
    """Upload a file as a release asset (multipart)."""
    import uuid
    filename = filepath.name
    url = (f"{API_BASE}/repos/{owner}/{repo}/releases/{release_id}/assets"
           f"?name={urllib.parse.quote(filename)}")

    boundary = uuid.uuid4().hex
    with open(filepath, "rb") as f:
        file_data = f.read()

    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="attachment"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + file_data + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"token {token}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=900) as r:
        result = json.load(r)
    logging.info(f"Uploaded {filename} ({result.get('size', '?')} bytes)")
    return result


def delete_asset(owner: str, repo: str, release_id: int, token: str, asset_id: int, name: str):
    api("DELETE", f"/repos/{owner}/{repo}/releases/{release_id}/assets/{asset_id}", token)
    logging.info(f"Deleted old asset {name}")


def build_release_notes(asset_names: list[str]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    lines = ["# Morphe APKs - Auto Built", "", "## Available Apps", ""]
    for n in sorted(set(asset_names)):
        lines.append(f"- {n}")
    lines += [
        "", "---", "",
        "## Build Information",
        f"- **Auto-built {now} UTC**",
        "- **Multiple architecture support** (arm64-v8a, armeabi-v7a, universal)",
        "- **Latest Morphe patches**",
        "", "## Architecture Guide",
        "- **arm64-v8a**: Modern ARM devices (2014+)",
        "- **armeabi-v7a**: Older ARM devices",
        "- **universal**: All ARM devices (larger file)",
        "", "## Disclaimer",
        "These APKs are built automatically. Use at your own risk.",
    ]
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--token", required=True)
    p.add_argument("--owner", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--tag", default="latest")
    p.add_argument("--apk-dir", required=True)
    p.add_argument("--clobber", action="store_true",
                   help="delete an existing asset with the same name before uploading")
    args = p.parse_args()

    apk_dir = Path(args.apk_dir)
    apks = sorted(apk_dir.rglob("*.apk"))
    # Also pick up manifest.json if present
    manifests = sorted(apk_dir.rglob("manifest.json"))

    if not apks:
        logging.error("No APKs found to upload. Failing.")
        sys.exit(1)

    logging.info(f"Found {len(apks)} APKs to upload")

    rel = get_or_create_release(args.owner, args.repo, args.tag, args.token)
    release_id = rel["id"]

    # Clobber: remove same-name assets first so re-uploads replace cleanly
    if args.clobber:
        existing = {a["name"]: a["id"] for a in rel.get("assets", [])}
        for f in list(apks) + list(manifests):
            dup_id = existing.get(f.name)
            if dup_id:
                delete_asset(args.owner, args.repo, release_id, args.token, dup_id, f.name)

    # Upload new APKs first (never delete before upload succeeds)
    uploaded_names = []
    for apk in apks:
        upload_asset(args.owner, args.repo, release_id, args.token, apk)
        uploaded_names.append(apk.name)

    for m in manifests:
        upload_asset(args.owner, args.repo, release_id, args.token, m)
        uploaded_names.append(m.name)

    # Delete superseded older versions (same identity prefix, different version)
    keep_prefixes = set()
    for name in uploaded_names:
        prefix = identity_prefix(name)
        if prefix:
            keep_prefixes.add(prefix)

    # Refresh asset list
    rel, _ = api("GET", f"/repos/{args.owner}/{args.repo}/releases/{release_id}", args.token)
    for asset in rel.get("assets", []):
        name = asset["name"]
        if name in uploaded_names:
            continue
        prefix = identity_prefix(name)
        if prefix and prefix in keep_prefixes:
            delete_asset(args.owner, args.repo, release_id, args.token, asset["id"], name)

    # Update release notes
    all_names = [a["name"] for a in rel.get("assets", [])] + uploaded_names
    notes = build_release_notes(all_names)
    api("PATCH", f"/repos/{args.owner}/{args.repo}/releases/{release_id}", args.token, {
        "body": notes,
        "name": f"Morphe APKs - {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC",
    })

    logging.info(f"Done. Release: https://codeberg.org/{args.owner}/{args.repo}/releases/tag/{args.tag}")


if __name__ == "__main__":
    main()
