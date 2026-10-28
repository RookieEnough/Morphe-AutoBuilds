#!/usr/bin/env python3
"""Publish one GitHub release per built app/version for Obtainium tracking.

The repo's existing patch workflow can aggregate many APKs into a single release,
which is awkward for release-based trackers like Obtainium. This script creates a
separate GitHub release for each distinct app/version pair using the naming
pattern:

    {app-name} {app-version}

with a tag that is Git-safe and stable:

    {app-name}-{app-version}

This makes release discovery easy for regex-based trackers while preserving the
full build history.
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


def parse_app_name_and_version(apk_name: str) -> tuple[str, str] | None:
    """Return (app_name, version) from a filename like:
    youtube-music-arm64-v8a-piko-v6.39.0.apk
    x-twitter-universal-yt-android-v1.2.3.apk
    """
    name = Path(apk_name).name
    if not name.lower().endswith(".apk"):
        return None
    stem = name[:-4]
    version = ""
    match = re.search(r"-v([0-9][\w.+\-() ]*)$", stem)
    if not match:
        return None
    version = match.group(1).strip()
    before_version = stem[: match.start()]
    arch_tokens = (
        "arm64-v8a",
        "armeabi-v7a",
        "x86_64",
        "x86",
        "universal",
    )
    # Split on the first recognized architecture token to isolate app name.
    arch_index = None
    for token in arch_tokens:
        idx = before_version.rfind(f"-{token}")
        if idx != -1:
            arch_index = idx
            break
    if arch_index is None:
        # Fallback: take everything before the final "-v<version>" marker and
        # assume the app is the left-most chunk.
        prefix = before_version.rsplit("-", 1)[0] if "-" in before_version else before_version
        return prefix, version
    app_name = before_version[:arch_index].rstrip("-")
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
            # Continue with the rest of the builds instead of failing the whole CI job.

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
