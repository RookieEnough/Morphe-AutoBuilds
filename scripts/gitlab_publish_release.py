#!/usr/bin/env python3
"""Publish built APKs to the GitLab 'latest' release.

Mirrors the GitHub patch.yml release flow:
1. Uploads every *.apk (+ manifest.json if present) in --apks-dir to the
   generic package registry (re-uploading the same filename overwrites).
2. Moves the 'latest' tag to the current commit and creates/updates the
   'latest' release.
3. Replaces the release's asset links with the current file set.
4. Deletes superseded older-version APKs (same identity prefix, not kept).

Usage:
    python scripts/gitlab_publish_release.py --apks-dir ./release-apks [--tag latest]
"""
import argparse
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import importlib.util as _ilu
def _load_gitlab_api():
    _spec = _ilu.spec_from_file_location(
        "gitlab_api", Path(__file__).resolve().parent.parent / "src" / "gitlab_api.py")
    _mod = _ilu.module_from_spec(_spec)
    sys.modules["gitlab_api"] = _mod
    _spec.loader.exec_module(_mod)
    return _mod
gitlab_api = _load_gitlab_api()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")

# Same identity rule as scripts/cleanup_old_apks.py: {app}-{arch}- before -v{version}.apk
VERSION_MARKER = re.compile(r"-v\d[\d.()+\-]*\.apk$", re.IGNORECASE)


def identity_prefix(filename: str) -> str | None:
    m = VERSION_MARKER.search(filename)
    return filename[:m.start() + 1] if m else None


def build_release_notes(asset_names: list[str]) -> tuple[str, str]:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    lines = ["# Morphe APKs - Auto Built (GitLab)", "", "## Available Apps", ""]
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
    ]
    return f"Morphe APKs - {now}", "\n".join(lines)


def rebuild_all_links(tag: str) -> int:
    """Recovery: ensure every APK in the package registry has a release asset link."""
    ref = os.environ.get("CI_COMMIT_SHA", "main")
    logging.info(f"Rebuilding asset links for release '{tag}' from package files...")

    pkg_files = gitlab_api.list_package_files()
    apk_files = [f for f in pkg_files if f.get("file_name", "").endswith(".apk")]
    logging.info(f"Found {len(apk_files)} APKs in package registry")

    existing = {l["name"]: l for l in gitlab_api.list_asset_links(tag)}
    logging.info(f"Found {len(existing)} existing asset links")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    gitlab_api.ensure_release(tag, f"Morphe APKs - {now}", "", ref)

    created = 0
    for pf in apk_files:
        name = pf["file_name"]
        if name in existing:
            continue
        url = gitlab_api.package_file_url(name)
        try:
            r = gitlab_api.api("POST", f"/releases/{tag}/assets/links",
                                json={"name": name, "url": url, "link_type": "package"})
            r.raise_for_status()
        except Exception as e:
            # Link may already exist (duplicate filename in package); skip
            logging.warning(f"Could not create link for {name}: {e}")
            existing[name] = True  # Mark as handled to avoid retry loop
            continue
        existing[name] = True  # Track newly created to handle duplicates
        created += 1
        logging.info(f"Created asset link: {name}")

    logging.info(f"Rebuild complete: {created} links created, {len(existing)} already existed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apks-dir", required=False, default=None)
    ap.add_argument("--tag", default="latest")
    ap.add_argument("--merge", action="store_true",
                    help="Add/replace only these assets, keep existing links "
                         "(for single-app manual builds). Default replaces all.")
    ap.add_argument("--rebuild-links", action="store_true",
                    help="Recovery mode: rebuild all release asset links from "
                         "package files. Ignores --apks-dir.")
    args = ap.parse_args()

    if args.rebuild_links:
        return rebuild_all_links(args.tag)

    if not args.apks_dir:
        ap.error("--apks-dir is required unless --rebuild-links is used")
    apks_dir = Path(args.apks_dir)
    apks = sorted(apks_dir.glob("*.apk"))
    manifest = apks_dir / "manifest.json"
    if not apks:
        logging.info("No APKs to publish; skipping release.")
        return 0

    # 1. Upload everything first (never delete before the new files are up).
    # GitLab rejects some filename characters (e.g. parentheses -> HTTP 400
    # "file_name is invalid"). Rename on disk first so build records,
    # manifest.json, asset links and package files all agree on the name.
    renames: dict[str, str] = {}
    for apk in apks:
        safe = gitlab_api.safe_filename(apk.name)
        if safe != apk.name:
            target = apk.with_name(safe)
            if target.exists():
                target.unlink()
            apk.rename(target)
            renames[apk.name] = safe
            logging.info(f"Renamed {apk.name} -> {safe} for GitLab")
            apk = target
    if renames and manifest.exists():
        try:
            import json as _json
            data = _json.loads(manifest.read_text(encoding="utf-8"))
            for entry in data.get("entries", {}).values():
                if entry.get("apk", "") in renames:
                    entry["apk"] = renames[entry["apk"]]
            manifest.write_text(_json.dumps(data, indent=2), encoding="utf-8")
            logging.info(f"Patched manifest.json for {len(renames)} renamed files")
        except Exception as e:
            logging.warning(f"Could not patch manifest.json filenames: {e}")
    apks = sorted(apks_dir.glob("*.apk"))

    urls: dict[str, str] = {}
    for apk in apks:
        urls[apk.name] = gitlab_api.upload_package_file(apk)
    if manifest.exists():
        urls[manifest.name] = gitlab_api.upload_package_file(manifest)

    # 2. Ensure release + set asset links.
    ref = os.environ.get("CI_COMMIT_SHA", "main")
    if args.merge:
        # Single-app manual build: keep existing assets, add/replace ours.
        # Preserve the existing release title/notes when the release exists.
        try:
            cur = gitlab_api.api("GET", f"/releases/{args.tag}").json()
            title, notes = cur.get("name") or "Morphe APKs", cur.get("description") or ""
        except Exception:
            title, notes = build_release_notes(list(urls))
        existing = {l["name"]: l for l in gitlab_api.list_asset_links(args.tag)}
        gitlab_api.ensure_release(args.tag, title, notes, ref)
        for name, url in urls.items():
            if name in existing:
                gitlab_api.delete_asset_link(args.tag, existing[name]["id"])
            r = gitlab_api.api("POST", f"/releases/{args.tag}/assets/links",
                               json={"name": name, "url": url, "link_type": "package"})
            r.raise_for_status()
        # Build keep set from package files (reliable source of truth).
        # The asset-links API can return stale/empty data due to consistency
        # lag; never let a flaky listing cause mass deletion of good links.
        try:
            _pkg_files = gitlab_api.list_package_files()
            _pkg_apks = {f.get("file_name", "") for f in _pkg_files
                         if f.get("file_name", "").endswith(".apk")}
        except Exception as e:
            logging.warning(f"Could not list package files for keep-set: {e}")
            _pkg_apks = set()
        keep = set(urls) | _pkg_apks | (set(existing) - set(urls))
    else:
        title, notes = build_release_notes(list(urls))
        gitlab_api.ensure_release(args.tag, title, notes, ref)
        gitlab_api.replace_asset_links(args.tag, [(n, u) for n, u in urls.items()])
        keep = set(urls)
    # Clean up superseded APKs from the package registry.
    # The asset-links API is unreliable, so we list package files directly.
    # For each app prefix, keep only the files in 'keep' (current versions).
    try:
        all_files = gitlab_api.list_package_files()
        for f in all_files:
            name = f.get("file_name", "")
            if not name.endswith(".apk") or name in keep:
                continue
            prefix = identity_prefix(name)
            if not prefix:
                continue
            if any(k != name and k.startswith(prefix) for k in keep):
                logging.info(f"Deleting superseded package file: {name}")
                gitlab_api.delete_package_file(name)
    except Exception as e:
        logging.warning(f"Package cleanup failed: {e}")
    # Also clean up stale asset links if any exist
    all_links = gitlab_api.list_asset_links(args.tag)
    to_delete = []
    for link in all_links:
        name = link.get("name", "")
        if not name.endswith(".apk") or name in keep:
            continue
        prefix = identity_prefix(name)
        if not prefix:
            continue
        if any(k != name and (k.startswith(prefix)) for k in keep):
            to_delete.append(link)
    # Safety guard: refuse mass deletion (likely API inconsistency, not staleness)
    if all_links and len(to_delete) > len(all_links) * 0.3:
        raise RuntimeError(
            f"Refusing to delete {len(to_delete)}/{len(all_links)} asset links "
            f"(>30%). This likely indicates GitLab API inconsistency. Aborting."
        )
    for link in to_delete:
        logging.info(f"Deleting superseded asset link: {link.get('name')}")
        gitlab_api.delete_asset_link(args.tag, link["id"])

    if args.merge:
        # Refresh release notes from the final asset set: merge mode otherwise
        # preserves the old notes, which would list stale filenames.
        final_names = [l["name"] for l in gitlab_api.list_asset_links(args.tag)]
        if final_names:
            mtitle, mnotes = build_release_notes(final_names)
            gitlab_api.ensure_release(args.tag, mtitle, mnotes, ref)

    logging.info("GitLab release publish complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
