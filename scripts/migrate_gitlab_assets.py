#!/usr/bin/env python3
"""One-time migration: copy every asset from the GitHub 'latest' release
(already downloaded to --src-dir, e.g. via `gh release download latest`)
to the GitLab generic package registry and set them as asset links on the
GitLab 'latest' release, then verify.

Verification HEADs every package download URL and compares Content-Length
against the local file size; exits non-zero on any mismatch, so the GitHub
release is only deleted after a proven-complete migration.

Usage:
    python scripts/migrate_gitlab_assets.py --src-dir ./gh-assets [--dry-run]

Env: GITLAB_TOKEN (or GITLAB_RELEASE_TOKEN), GITLAB_PROJECT_ID.
"""
import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import importlib.util

def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

_HERE = Path(__file__).resolve().parent
# Load the two modules by path so importing `src/__init__.py`
# (which pulls the whole downloader dependency tree) is avoided.
gitlab_api = _load("gitlab_api", _HERE.parent / "src" / "gitlab_api.py")
_glpub = _load("gitlab_publish_release", _HERE / "gitlab_publish_release.py")
build_release_notes = _glpub.build_release_notes
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--src-dir", required=True)
    p.add_argument("--tag", default="latest")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    try:
        pid = gitlab_api._project()
        gitlab_api._headers()
    except RuntimeError as e:
        if args.dry_run:
            logging.warning(f"{e}; skipping token check.")
            pid = None
        else:
            logging.error(str(e))
            return 1

    src = Path(args.src_dir)
    files = sorted(f for f in src.iterdir() if f.is_file())
    if not files:
        logging.error(f"No files found in {src}. Nothing to migrate.")
        return 1

    total = sum(f.stat().st_size for f in files)
    logging.info(f"{len(files)} file(s), {total / 1e9:.2f} GB total")

    # Pre-check: local files must match the GitHub release asset sizes, so a
    # truncated download can never be uploaded as if it were complete.
    try:
        gr = requests.get(
            "https://api.github.com/repos/RookieEnough/Morphe-AutoBuilds"
            "/releases/tags/latest", timeout=30)
        gr.raise_for_status()
        expected = {a["name"]: a["size"] for a in gr.json().get("assets", [])}
    except Exception as e:
        logging.error(f"Could not read GitHub release asset list: {e}")
        return 1
    bad = []
    for f in files:
        if f.name not in expected:
            bad.append(f"NOT IN GITHUB RELEASE: {f.name}")
        elif f.stat().st_size != expected[f.name]:
            bad.append(f"SIZE MISMATCH vs GitHub: {f.name} "
                       f"(local {f.stat().st_size}, github {expected[f.name]})")
    for name in expected:
        if name not in {f.name for f in files}:
            bad.append(f"MISSING LOCALLY: {name}")
    if bad:
        logging.error("Local files do not match the GitHub release:")
        for line in bad:
            logging.error(f"  {line}")
        return 1
    logging.info("Local files match the GitHub release (names + sizes).")

    if args.dry_run:
        for f in files:
            print(f"  [dry-run] would upload: {f.name} ({f.stat().st_size} bytes)")
        if pid:
            r = gitlab_api.api("GET", "")
            if r.status_code == 200:
                logging.info(f"GitLab token OK (project {pid}).")
            else:
                logging.error(f"GitLab token check failed: HTTP {r.status_code}: {r.text[:200]}")
                return 1
        return 0

    # 1. Upload everything first (never touch the release before uploads land).
    urls = {}
    for f in files:
        urls[f.name] = gitlab_api.upload_package_file(f)

    # 2. Point the release at the full migrated set.
    title, notes = build_release_notes(list(urls))
    ref = os.environ.get("CI_COMMIT_SHA", "main")
    gitlab_api.ensure_release(args.tag, title, notes, ref)
    gitlab_api.replace_asset_links(args.tag, [(n, u) for n, u in urls.items()])

    # 3. Verify: every local file must be downloadable at the same size.
    problems = []
    for f in files:
        want = f.stat().st_size
        try:
            r = requests.head(urls[f.name], timeout=30, allow_redirects=True)
            got = r.headers.get("Content-Length")
            got = int(got) if got and got.isdigit() else None
        except Exception as e:
            problems.append(f"HEAD failed for {f.name}: {e}")
            continue
        if got is None:
            problems.append(f"NO SIZE HEADER for {f.name} (HTTP {r.status_code})")
        elif got != want:
            problems.append(f"SIZE MISMATCH: {f.name} (local {want}, remote {got})")

    if problems:
        logging.error("Verification FAILED:")
        for line in problems:
            logging.error(f"  {line}")
        return 1

    logging.info(f"Verification OK: all {len(files)} files downloadable from GitLab "
                 f"with matching sizes.")
    logging.info("Release: https://gitlab.com/sarthaksinha00/Community-builds/-/releases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
