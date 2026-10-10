#!/usr/bin/env python3
"""One-time migration: copy release assets from the GitHub 'latest' release
(already downloaded to --src-dir, e.g. via `gh release download latest`)
to the Codeberg Community-Builds 'latest' release, then verify.

Verification compares name -> size for every local file against the assets
now present on the Codeberg release and exits non-zero on any mismatch, so
the GitHub release is only ever deleted after a proven-complete migration.

Usage:
    python scripts/migrate_release_assets.py --src-dir ./gh-assets [--dry-run]
                                             [--owner RookieZ] [--repo Community-Builds]
                                             [--tag latest]
"""
import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codeberg_publish as cb

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--src-dir", required=True)
    p.add_argument("--owner", default="RookieZ")
    p.add_argument("--repo", default="Community-Builds")
    p.add_argument("--tag", default="latest")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    token = os.environ.get("CODEBERG_TOKEN", "")
    if not token and not args.dry_run:
        logging.error("CODEBERG_TOKEN env var is not set.")
        return 1

    src = Path(args.src_dir)
    files = sorted(f for f in src.iterdir() if f.is_file())
    if not files:
        logging.error(f"No files found in {src}. Nothing to migrate.")
        return 1

    total_bytes = sum(f.stat().st_size for f in files)
    logging.info(f"{len(files)} file(s), {total_bytes / 1e9:.2f} GB total")

    if args.dry_run:
        for f in files:
            print(f"  [dry-run] would upload: {f.name} ({f.stat().st_size} bytes)")
        # Validate the token with a cheap authenticated call so a bad/expired
        # token is caught before gigabytes are moved.
        if token:
            try:
                cb.api("GET", f"/repos/{args.owner}/{args.repo}", token)
                logging.info("Codeberg token OK (authenticated).")
            except Exception as e:
                logging.error(f"Codeberg token check failed: {e}")
                return 1
        else:
            logging.warning("No CODEBERG_TOKEN set; skipping token check.")
        return 0

    rel = cb.get_or_create_release(args.owner, args.repo, args.tag, token)
    release_id = rel["id"]

    # Upload with clobber so a re-run never creates duplicates.
    existing = {a["name"]: a["id"] for a in rel.get("assets", [])}
    for f in files:
        dup = existing.get(f.name)
        if dup:
            cb.delete_asset(args.owner, args.repo, release_id, token, dup, f.name)
        cb.upload_asset(args.owner, args.repo, release_id, token, f)

    # Verify: every local file must exist on Codeberg with the same size.
    rel, _ = cb.api("GET", f"/repos/{args.owner}/{args.repo}/releases/{release_id}", token)
    remote = {a["name"]: a.get("size", -1) for a in rel.get("assets", [])}
    problems = []
    for f in files:
        want = f.stat().st_size
        got = remote.get(f.name)
        if got is None:
            problems.append(f"MISSING on Codeberg: {f.name}")
        elif got != want:
            problems.append(f"SIZE MISMATCH: {f.name} (local {want}, remote {got})")

    if problems:
        logging.error("Verification FAILED:")
        for line in problems:
            logging.error(f"  {line}")
        return 1

    logging.info(f"Verification OK: all {len(files)} files present on Codeberg "
                 f"with matching sizes.")
    logging.info(f"Release: https://codeberg.org/{args.owner}/{args.repo}/releases/tag/{args.tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
