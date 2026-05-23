#!/usr/bin/env python3
import json
import os
import subprocess
import sys


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repo_slug = os.environ.get("GITHUB_REPOSITORY")
    if not token:
        print(
            "GitHub auth validation failed: missing GITHUB_TOKEN/GH_TOKEN",
            file=sys.stderr,
        )
        return 1

    env = os.environ.copy()
    env["GH_TOKEN"] = token

    status = subprocess.run(
        ["gh", "auth", "status"],
        env=env,
        capture_output=True,
        text=True,
    )
    if status.returncode != 0:
        print("GitHub auth validation failed: gh auth status failed", file=sys.stderr)
        print(status.stderr.strip() or status.stdout.strip(), file=sys.stderr)
        return 1

    api_target = f"repos/{repo_slug}" if repo_slug else "rate_limit"
    api = subprocess.run(
        ["gh", "api", api_target],
        env=env,
        capture_output=True,
        text=True,
    )
    if api.returncode != 0:
        print(f"GitHub auth validation failed: gh api {api_target} failed", file=sys.stderr)
        print(api.stderr.strip() or api.stdout.strip(), file=sys.stderr)
        return 1

    try:
        payload = json.loads(api.stdout)
    except json.JSONDecodeError:
        print(
            f"GitHub auth validation failed: could not parse gh api {api_target} output",
            file=sys.stderr,
        )
        return 1

    identity = payload.get("full_name") or payload.get("resources") or repo_slug or "<unknown>"
    print(f"GitHub auth OK: {identity}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
