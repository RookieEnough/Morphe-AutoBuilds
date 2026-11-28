#!/usr/bin/env python3
"""Publish built APKs to the GitLab 'latest' release.

1. Uploads every *.apk (+ manifest.json if present) in --apks-dir to the
   generic package registry, replacing same-named entries instead of letting
   GitLab accumulate duplicates.
2. Creates the 'latest' release when missing. An existing tag/release is never
   deleted or moved: that wipes every asset link (and the release itself).
3. Adds/replaces asset links for the uploaded files and repairs links that
   earlier failed runs lost, leaving other apps untouched.
4. Deletes superseded older-version APKs (same app/arch/patch identity) and the
   duplicate package rows older publishes left behind.

Usage:
    python scripts/gitlab_publish_release.py --apks-dir ./release-apks [--tag latest]
"""
import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

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


def manifest_apk_names(manifest: Path) -> set[str]:
    """Return the APK filenames the manifest says are the current builds."""
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        return set()
    names = set()
    for entry in (data.get("entries") or {}).values():
        apk = str(entry.get("apk") or "")
        if apk.endswith(".apk"):
            names.add(apk)
    return names


def superseded_apks(keep: set[str], candidates: Iterable[str]) -> list[str]:
    """APKs in ``candidates`` that a kept build of the same identity replaces.

    Identity is ``{app}-{arch}-{patch-name}-`` (everything before ``-v...apk``),
    so an older version of the same app/arch/patch source is superseded, while
    other apps and other architectures are never touched.
    """
    prefixes = {p for name in keep if (p := identity_prefix(name))}
    return sorted(
        name for name in candidates
        if name.endswith(".apk")
        and name not in keep
        and identity_prefix(name) in prefixes
    )


def prune_manifest_entries(manifest: Path, available: set[str]) -> int:
    """Clear manifest APK references that are not in the registry.

    A failed or partial publish leaves entries pointing at files that no longer
    exist (for example a tool APK that was published once and then removed).
    Clearing those references makes the planner rebuild the app instead of
    forever carrying a dead asset.
    """
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        return 0
    cleared = 0
    for entry in (data.get("entries") or {}).values():
        apk = str(entry.get("apk") or "")
        if apk.endswith(".apk") and apk not in available:
            logging.warning(f"Manifest references missing APK {apk!r}; clearing it")
            entry["apk"] = ""
            entry["built_version"] = ""
            cleared += 1
    if cleared:
        manifest.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return cleared


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


def link_map(tag: str) -> dict[str, dict]:
    """Return ``{name: link}`` for a release, collapsing duplicate link names.

    GitLab permits creating a second asset link with the same name (for example
    when a flaky empty listing made an older publish re-create links). Keep the
    newest id per name and delete the duplicates so the release does not list
    the same app twice.
    """
    links: dict[str, dict] = {}
    duplicates: list[dict] = []
    for link in gitlab_api.list_asset_links(tag):
        name = link.get("name", "")
        current = links.get(name)
        if current is None:
            links[name] = link
        elif int(link.get("id") or 0) > int(current.get("id") or 0):
            duplicates.append(current)
            links[name] = link
        else:
            duplicates.append(link)
    for link in duplicates:
        logging.info(f"Removing duplicate asset link: {link.get('name')}")
        gitlab_api.delete_asset_link(tag, link["id"])
    return links


def rebuild_all_links(tag: str) -> int:
    """Recovery: ensure every APK in the package registry has a release asset link.

    Non-destructive: the tag is never moved, the release notes are preserved and
    existing links are kept, so a recovery run cannot lose assets that are
    already correct.
    """
    ref = os.environ.get("CI_COMMIT_SHA", "main")
    logging.info(f"Rebuilding asset links for release '{tag}' from package files...")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    try:
        cur = gitlab_api.api("GET", f"/releases/{tag}").json()
        title = cur.get("name") or f"Morphe APKs - {now}"
        notes = cur.get("description") or ""
    except Exception:
        title, notes = f"Morphe APKs - {now}", ""
    gitlab_api.ensure_release(tag, title, notes, ref)

    links = link_map(tag)
    pkg_files = gitlab_api.list_package_files()
    apk_names = {f.get("file_name", "") for f in pkg_files
                 if f.get("file_name", "").endswith(".apk")}
    logging.info(f"Found {len(apk_names)} APKs in package registry, "
                 f"{len(links)} existing links")

    created = 0
    for name in sorted(apk_names - set(links)):
        url = gitlab_api.package_file_url(name)
        r = gitlab_api.api("POST", f"/releases/{tag}/assets/links",
                           json={"name": name, "url": url, "link_type": "package"})
        if r.status_code in (200, 201):
            created += 1
        else:
            logging.warning(f"Could not create link for {name}: {r.status_code} {r.text[:120]}")

    deduped = gitlab_api.dedupe_package_files()
    logging.info(f"Rebuild complete: {created} links created, "
                 f"{deduped} duplicate package entries removed.")
    return 0


def verify_release_healthy(tag: str) -> tuple[bool, str]:
    """Post-publish health check for the GitLab release.

    Returns (healthy, detail). The release must exist, every APK in the package
    registry must have an asset link (a missing link means the store shows that
    app as unavailable), and the manifest must not reference APKs that no
    longer exist. list_asset_links() already retries through GitLab's
    consistency lag.
    """
    try:
        r = gitlab_api.api("GET", f"/releases/{tag}")
        if r.status_code == 404:
            return False, "release object is missing"
        r.raise_for_status()
    except Exception as e:
        return False, f"release check failed: {e}"[:200]

    links = {l.get("name", "") for l in gitlab_api.list_asset_links(tag)}
    try:
        pkg_files = gitlab_api.list_package_files()
    except Exception as e:
        return False, f"could not list package files: {e}"[:200]
    pkg_apks = {f.get("file_name", "") for f in pkg_files
                if f.get("file_name", "").endswith(".apk")}

    missing_links = sorted(pkg_apks - links)
    if missing_links:
        sample = ", ".join(missing_links[:3])
        return False, (f"{len(missing_links)} package APK(s) have no release link "
                       f"(e.g. {sample})")

    # Manifest cross-check (best effort): every referenced APK must exist.
    try:
        dest = Path("manifest.verify.json")
        if gitlab_api.download_package_file("manifest.json", dest) is not None:
            referenced = manifest_apk_names(dest)
            dest.unlink(missing_ok=True)
            stale = sorted(referenced - pkg_apks)
            if stale:
                sample = ", ".join(stale[:3])
                return False, (f"manifest references {len(stale)} APK(s) missing "
                               f"from the registry (e.g. {sample})")
    except Exception as e:
        logging.warning(f"verify: manifest cross-check skipped: {e}")

    return True, f"release OK ({len(links)} asset links, {len(pkg_apks)} package APKs)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apks-dir", required=False, default=None)
    ap.add_argument("--tag", default="latest")
    ap.add_argument("--merge", action="store_true",
                    help="Kept for compatibility: publishing is always "
                         "incremental now, so other apps' assets are never "
                         "removed.")
    ap.add_argument("--rebuild-links", action="store_true",
                    help="Recovery mode: rebuild all release asset links from "
                         "package files. Ignores --apks-dir.")
    ap.add_argument("--verify-only", action="store_true",
                    help="Check that the release exists and serves asset "
                         "links; exit 0 if healthy, 1 otherwise. "
                         "Ignores --apks-dir.")
    args = ap.parse_args()

    if args.rebuild_links:
        return rebuild_all_links(args.tag)

    if args.verify_only:
        healthy, detail = verify_release_healthy(args.tag)
        logging.info(f"Verify release '{args.tag}': {detail}")
        return 0 if healthy else 1

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
            data = json.loads(manifest.read_text(encoding="utf-8"))
            for entry in data.get("entries", {}).values():
                if entry.get("apk", "") in renames:
                    entry["apk"] = renames[entry["apk"]]
            manifest.write_text(json.dumps(data, indent=2), encoding="utf-8")
            logging.info(f"Patched manifest.json for {len(renames)} renamed files")
        except Exception as e:
            logging.warning(f"Could not patch manifest.json filenames: {e}")
    apks = sorted(apks_dir.glob("*.apk"))

    # 2. Upload. GitLab keeps repeated uploads of one filename as separate
    # package rows, so delete the old entries first: the new bytes replace them
    # instead of accumulating duplicates on every publish.
    try:
        existing_files = gitlab_api.list_package_files()
    except Exception as e:
        logging.warning(f"Could not list package files before upload: {e}")
        existing_files = []
    existing_names = {
        f.get("file_name", "") for f in existing_files if f.get("file_name")
    }
    if manifest.exists():
        cleared = prune_manifest_entries(manifest, existing_names | {a.name for a in apks})
        if cleared:
            logging.info(f"Cleared {cleared} dead APK reference(s) from manifest")

    urls: dict[str, str] = {}
    for apk in apks:
        if apk.name in existing_names:
            gitlab_api.delete_package_file(apk.name)
        urls[apk.name] = gitlab_api.upload_package_file(apk)
    if manifest.exists():
        if manifest.name in existing_names:
            gitlab_api.delete_package_file(manifest.name)
        urls[manifest.name] = gitlab_api.upload_package_file(manifest)

    # 3. Ensure the release exists without ever moving/deleting its tag.
    # In GitLab, deleting a tag deletes the release and every asset link on it,
    # so the old "move tag on every publish" step could wipe the whole release.
    ref = os.environ.get("CI_COMMIT_SHA", "main")
    try:
        cur = gitlab_api.api("GET", f"/releases/{args.tag}").json()
        title = cur.get("name") or "Morphe APKs"
        notes = cur.get("description") or ""
    except Exception:
        title, notes = build_release_notes(list(urls))
    gitlab_api.ensure_release(args.tag, title, notes, ref)

    # 4. Add/replace links for the files uploaded by this run. Every other app
    # on the release stays exactly as it was.
    links = link_map(args.tag)
    for name, url in urls.items():
        current = links.get(name)
        if current is None:
            r = gitlab_api.api("POST", f"/releases/{args.tag}/assets/links",
                               json={"name": name, "url": url, "link_type": "package"})
            r.raise_for_status()
            links[name] = {"name": name, "url": url,
                           "id": (r.json() or {}).get("id")}
        elif current.get("url") != url:
            r = gitlab_api.api(
                "PUT", f"/releases/{args.tag}/assets/links/{current['id']}",
                json={"name": name, "url": url})
            r.raise_for_status()

    # 5. Registry contents + the APKs that must stay: this run's APKs plus
    # everything the manifest still points at (carried-over apps included).
    try:
        pkg_files = gitlab_api.list_package_files()
    except Exception as e:
        logging.warning(f"Could not list package files: {e}")
        pkg_files = []
    pkg_apks = {f.get("file_name", "") for f in pkg_files
                if f.get("file_name", "").endswith(".apk")}
    keep_apks = {a.name for a in apks}
    if manifest.exists():
        keep_apks |= manifest_apk_names(manifest)

    # 6. Repair links lost by earlier failed publishes (package file exists but
    # no asset link). Otherwise one bad run hides an app until it is rebuilt.
    repaired = 0
    for name in sorted((keep_apks & pkg_apks) - set(links)):
        r = gitlab_api.api("POST", f"/releases/{args.tag}/assets/links",
                           json={"name": name,
                                 "url": gitlab_api.package_file_url(name),
                                 "link_type": "package"})
        if r.status_code in (200, 201):
            repaired += 1
            links[name] = r.json()
        else:
            logging.warning(f"Could not repair link for {name}: {r.status_code}")
    if repaired:
        logging.info(f"Repaired {repaired} missing asset link(s)")

    # 7. Remove superseded versions (same app/arch/patch identity only) from
    # both the release and the package registry.
    stale_links = superseded_apks(keep_apks, set(links))
    for name in stale_links:
        link = links.pop(name)
        logging.info(f"Removing superseded asset link: {name}")
        gitlab_api.delete_asset_link(args.tag, link["id"])
    stale_files = superseded_apks(keep_apks, pkg_apks)
    for name in stale_files:
        logging.info(f"Deleting superseded package file: {name}")
        gitlab_api.delete_package_file(name)

    # 8. Collapse duplicate package rows left behind by older publishes.
    gitlab_api.dedupe_package_files()

    # 9. Refresh notes from the final link set, without touching the tag.
    final_names = [l["name"] for l in gitlab_api.list_asset_links(args.tag)]
    if final_names:
        mtitle, mnotes = build_release_notes(final_names)
        r = gitlab_api.api("PUT", f"/releases/{args.tag}",
                           json={"name": mtitle, "description": mnotes})
        r.raise_for_status()

    logging.info("GitLab release publish complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
