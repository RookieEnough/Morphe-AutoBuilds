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

    # Check if we're in GitHub Actions environment
    is_github_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    
    if is_github_actions:
        # In GitHub Actions, GITHUB_TOKEN is an installation token that works for API access
        # but may not work with gh auth commands. Just verify we can make API calls.
        env = os.environ.copy()
        env["GH_TOKEN"] = token
        
        # Try to access the repository API or rate limit endpoint
        api_target = f"repos/{repo_slug}" if repo_slug else "rate_limit"
        api = subprocess.run(
            ["gh", "api", api_target],
            env=env,
            capture_output=True,
            text=True,
        )
        if api.returncode != 0:
            # If the repo API fails, try rate limit
            api = subprocess.run(
                ["gh", "api", "rate_limit"],
                env=env,
                capture_output=True,
                text=True,
            )
            if api.returncode != 0:
                print(f"GitHub auth validation failed: gh api {api_target} and rate_limit failed", file=sys.stderr)
                print(api.stderr.strip() or api.stdout.strip(), file=sys.stderr)
                return 1

        try:
            payload = json.loads(api.stdout)
        except json.JSONDecodeError:
            print(
                f"GitHub auth validation failed: could not parse gh api output",
                file=sys.stderr,
            )
            return 1

        # Extract meaningful identity from the response
        if "resources" in payload:
            identity = f"GitHub Actions (rate limit: {payload['resources']['core']['limit']} hourly)"
        elif "full_name" in payload:
            identity = payload["full_name"]
        else:
            identity = repo_slug or "<unknown>"
            
        print(f"GitHub auth OK: {identity}")
        return 0
    else:
        # For non-Actions environments (local testing), do full auth validation
        env = os.environ.copy()
        env["GH_TOKEN"] = token

        # Check gh auth status first
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

        # For GITHUB_TOKEN in Actions, check if we can access the repository API
        # instead of trying to access user info which may not work with installation tokens
        api_target = f"repos/{repo_slug}" if repo_slug else "rate_limit"
        api = subprocess.run(
            ["gh", "api", api_target],
            env=env,
            capture_output=True,
            text=True,
        )
        if api.returncode != 0:
            # If the repo API fails, try a simpler endpoint that should work with installation tokens
            api = subprocess.run(
                ["gh", "api", "rate_limit"],
                env=env,
                capture_output=True,
                text=True,
            )
            if api.returncode != 0:
                print(f"GitHub auth validation failed: gh api {api_target} and rate_limit failed", file=sys.stderr)
                print(api.stderr.strip() or api.stdout.strip(), file=sys.stderr)
                return 1

        try:
            payload = json.loads(api.stdout)
        except json.JSONDecodeError:
            print(
                f"GitHub auth validation failed: could not parse gh api output",
                file=sys.stderr,
            )
            return 1

        # Extract meaningful identity from the response
        if "resources" in payload:
            identity = f"GitHub Actions (rate limit: {payload['resources']['core']['limit']} hourly)"
        elif "full_name" in payload:
            identity = payload["full_name"]
        else:
            identity = repo_slug or "<unknown>"
            
        print(f"GitHub auth OK: {identity}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
