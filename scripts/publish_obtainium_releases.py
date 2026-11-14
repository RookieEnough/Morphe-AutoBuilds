#!/usr/bin/env python3
"""
Publish one GitHub release per built app/version for Obtainium tracking.

Naming:
  Release title: {app-name} {app-version}
  Tag:           {app-name}-{app-version}

Example:
  YouTube 19.10.39
  youtube-19.10.39
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Iterable

def run_cmd(args: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(args, capture_output=True, text=True, env=env)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()

def slugify_tag(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    cleaned = cleaned.strip("-._")
    return cleaned or "release"

ARCH_TOKENS = ("arm64-v8a", "armeabi-v7a", "x86_64", "x86", "universal")

def parse_app_name_and_version(apk_name: str) -> tuple[str, str] | None:
    """
    Parse filenames like:
      youtube-music-arm64-v8a-piko-v9.20.53.apk
      youtube-arm64-v8a-piko-v19.10.39.apk
      x-twitter-universal-piko-v1.2.3.apk

    Returns:
      ("youtube-music", "9.20.53")
      ("youtube", "19.10.39")
      ("x-twitter", "1.2.3")
    """
    name = Path(apk_name).name
    if not name.lower().endswith(".apk"):
        return None

    stem = name[:-4]

    # IMPORTANT:
    # match only a real version marker, not arch tokens like -v8a or -v7a
    match = re.search(r"-v(\d+\.[\w.+\-() ]*)$", stem)
    if not match:
        return None

    version = match.group(1).strip()
    prefix = stem[:match.start()].rstrip("-")

    # Strip architecture tokens from the app prefix
    for arch in ARCH_TOKENS:
        prefix = prefix.replace(f"-{arch}", "")

    app_name = prefix.strip("-")
    if not app_name:
        return None

    return app_name, version

def gh_env() -> dict[str, str]:
    env = os.environ.copy()
    token = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN")
    if token:
        env["GH_TOKEN"] = token
        env["GITHUB_TOKEN"] = token
    return env

def release_exists(tag: str, env: dict[str, str]) -> bool:
    rc, _, _ = run_cmd(["gh", "release", "view", tag, "--json", "tagName"], env=env)
    return rc == 0

def ensure_release(app_name: str, version: str, files: Iterable[str], env: dict[str, str]) -> None:
    release_name = f"{app_name} {version}"
    tag_name = f"{slugify_tag(app_name)}-{slugify_tag(version)}"

    file_list = list(files)
    if not file_list:
        return

    if release_exists(tag_name, env):
        print(f"Release already exists: {release_name} ({tag_name})")
        for apk in file_list:
            run_cmd(["gh", "release", "upload", tag_name, apk, "--clobber"], env=env)
        return

    notes = (
        "# Morphe build\n\n"
        f"App: {app_name}\n"
        f"Version: {version}\n\n"
        "Automated build from Morphe AutoBuilds.\n"
    )

    rc, out, err = run_cmd(
        [
            "gh",
            "release",
            "create",
            tag_name,
            *file_list,
            "--title",
            release_name,
            "--notes",
            notes,
            "--target",
            os.environ.get("GITHUB_SHA", "main"),
        ],
        env=env,
    )

    if rc != 0:
        raise RuntimeError(f"Failed to create release {release_name}: {err or out}")

    print(f"Created release: {release_name} ({tag_name})")

def main() -> int:
    apks_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("./release-apks")
    if not apks_dir.exists():
        print(f"APK directory not found: {apks_dir}", file=sys.stderr)
        return 0

    grouped: dict[tuple[str, str], list[str]] = defaultdict(list)

    for apk in sorted(apks_dir.glob("*.apk")):
        parsed = parse_app_name_and_version(apk.name)
        if parsed is None:
            print(f"Skipping unrecognized APK name: {apk.name}", file=sys.stderr)
            continue
        app_name, version = parsed
        grouped[(app_name, version)].append(str(apk))

    if not grouped:
        print("No valid APKs found for Obtainium release publication.")
        return 0

    env = gh_env()

    for (app_name, version), files in sorted(grouped.items()):
        try:
            ensure_release(app_name, version, files, env)
        except Exception as exc:
            print(f"Failed to publish release for {app_name} {version}: {exc}", file=sys.stderr)

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
