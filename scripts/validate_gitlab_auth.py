#!/usr/bin/env python3
"""Validate GitLab API auth for the Morphe-AutoBuilds mirror CI."""
import os
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from src import gitlab_api


def main() -> int:
    try:
        r = gitlab_api.api("GET", "/user")
    except RuntimeError as e:
        print(f"GitLab auth validation failed: {e}", file=sys.stderr)
        return 1
    if r.status_code != 200:
        print(f"GitLab auth validation failed: HTTP {r.status_code}: {r.text[:200]}",
              file=sys.stderr)
        return 1
    user = r.json().get("username", "?")
    pid = os.environ.get("CI_PROJECT_ID", "?")
    print(f"GitLab auth OK: user={user} project={pid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
