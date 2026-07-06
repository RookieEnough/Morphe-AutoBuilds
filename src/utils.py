import json
import os
import re
import shutil
import time
import logging
from typing import List, Optional
from github.GithubException import BadCredentialsException
from src import gh
from sys import exit
import subprocess
from pathlib import Path
from urllib.parse import urlparse, unquote, parse_qs, quote
from src import session

def _parseparam(s):
    while s[:1] == ";":
        s = s[1:]
        end = s.find(";")
        while end > 0 and (s.count('"', 0, end) - s.count('\\"', 0, end)) % 2:
            end = s.find(";", end + 1)
        if end < 0:
            end = len(s)
        f = s[:end]
        yield f.strip()
        s = s[end:]


def parse_header(line):
    """Parse a Content-type like header.
    Return the main content-type and a dictionary of options.
    """
    parts = _parseparam(";" + line)
    key = parts.__next__()
    pdict = {}
    for p in parts:
        i = p.find("=")
        if i >= 0:
            name = p[:i].strip().lower()
            value = p[i + 1 :].strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
                value = value.replace("\\\\", "\\").replace('\\"', '"')
            pdict[name] = value
    return key, pdict

def find_file(files: list[Path], prefix: str = None, suffix: str = None, contains: str = None, exclude: list = None) -> Path | None:
    """Find a file with various matching criteria"""
    if exclude is None:
        exclude = []
    
    for file in files:
        # Skip excluded patterns
        if any(excl.lower() in file.name.lower() for excl in exclude):
            continue
            
        # Check all criteria
        matches = True
        
        if prefix and not file.name.startswith(prefix):
            matches = False
            
        if suffix and not file.name.endswith(suffix):
            matches = False
            
        if contains and contains.lower() not in file.name.lower():
            matches = False
            
        if matches:
            return file
    
    # If not found with exclude, try without exclude (for fallback)
    if exclude:
        for file in files:
            matches = True
            
            if prefix and not file.name.startswith(prefix):
                matches = False
                
            if suffix and not file.name.endswith(suffix):
                matches = False
                
            if contains and contains.lower() not in file.name.lower():
                matches = False
                
            if matches:
                return file
    
    return None

def find_apksigner() -> str | None:
    on_path = shutil.which("apksigner")
    if on_path:
        return on_path

    sdk_roots = [
        "/usr/local/lib/android/sdk",  # GitHub Actions runner default
        os.environ.get("ANDROID_HOME"),
        os.environ.get("ANDROID_SDK_ROOT"),
    ]

    for root in sdk_roots:
        if not root:
            continue
        build_tools_dir = Path(root) / "build-tools"
        if not build_tools_dir.exists():
            continue
        versions = sorted(build_tools_dir.iterdir(), reverse=True)
        for version_dir in versions:
            apksigner_path = version_dir / "apksigner"
            if apksigner_path.exists() and apksigner_path.is_file():
                return str(apksigner_path)

    logging.error(
        "No apksigner found. Install Android SDK build-tools and either put "
        "apksigner on PATH or set ANDROID_HOME/ANDROID_SDK_ROOT."
    )
    return None

def run_process(
    command: List[str],
    cwd: Optional[Path] = None,
    capture: bool = False,
    stream: bool = False,
    silent: bool = False,
    check: bool = True,
    shell: bool = False
) -> Optional[str]:
    process = subprocess.Popen(
        command,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        shell=shell
    )

    output_lines = []

    try:
        for line in iter(process.stdout.readline, ''):
            if line:
                if not silent:
                    print(line.rstrip(), flush=True)
                if capture:
                    output_lines.append(line)
        process.stdout.close()
        return_code = process.wait()

        output = ''.join(output_lines).strip() if capture else None

        if check and return_code != 0:
            # Include captured output so callers can diagnose and optionally retry.
            raise subprocess.CalledProcessError(return_code, command, output=output)

        return output

    except FileNotFoundError as e:
        # Let callers handle this (e.g., fallback to another tool).
        raise e
    except Exception as e:
        # Do not exit() here; callers (workflow) may want to retry with a different
        # version/source or emit a clearer error message.
        raise e

def normalize_version(version: str) -> list[int]:
    parts = version.split('.')
    normalized = []
    for part in parts:
        match = re.match(r'(\d+)', part)
        if match:
            normalized.append(int(match.group(1)))
        else:
            normalized.append(0)
    
    # Include build number in comparison for versions like "6.6 build 002"
    build_match = re.search(r'build\s+(\d+)', version, re.IGNORECASE)
    if build_match:
        normalized.append(int(build_match.group(1)))
    
    # Also check for parentheses format like "32.30.0(1575420)"
    paren_match = re.search(r'\((\d+)\)$', version)
    if paren_match:
        normalized.append(int(paren_match.group(1)))
    
    return normalized

def get_highest_version(versions: list[str]) -> str | None:
    if not versions:
        return None
    highest_version = versions[0]
    for v in versions[1:]:
        if normalize_version(v) > normalize_version(highest_version):
            highest_version = v
    return highest_version

def get_supported_versions(package_name: str, cli: str, patches: str) -> list[str]:
    # Morphe CLI and ReVanced CLI have different list-versions syntax
    cli_name = Path(cli).name.lower()
    is_morphe_cli = 'morphe' in cli_name
    is_revanced_v6_or_newer = 'revanced-cli-6' in cli_name or 'revanced-cli-7' in cli_name or 'revanced-cli-8' in cli_name

    if is_morphe_cli:
        # Morphe CLI docs officially describe `list-patches --with-packages --with-versions`
        # (and the output tends to include more complete version information than
        # `list-versions`, which may only show "most common" compatible versions).
        #
        # We still try `list-versions` first because it's lighter, but if it
        # yields too little info we fall back to parsing `list-patches`.
        cmd = [
            'java', '-jar', cli,
            'list-versions',
            '-f', package_name,
            '--patches', patches
        ]
    elif is_revanced_v6_or_newer:
        cmd = [
            'java', '-jar', cli,
            'list-versions',
            '-p', patches, '-b',
            '-f', package_name
        ]
    else:
        # ReVanced CLI: pass patches as positional arg
        cmd = [
            'java', '-jar', cli,
            'list-versions',
            '-f', package_name,
            patches
        ]

    # We want the raw output even if the CLI returns a non-zero exit code (bad
    # args, missing patches, etc.) so we can decide what to do.
    output = run_process(cmd, capture=True, silent=True, check=False)

    if not output:
        logging.warning("No output returned from list-versions command")
        return []

    lines = output.splitlines()
    logging.info(f"CLI raw output lines: {lines}")

    # Detect CLI error/usage output (wrong syntax, unrecognized args, etc.)
    first_line = lines[0].strip().lower()
    if 'usage:' in first_line or 'unmatched argument' in first_line or 'error' in first_line:
        logging.warning(f"CLI returned error/usage output, cannot determine version")
        return []

    if len(lines) <= 2:
        logging.warning("Output has no version lines")
        return []

    versions = []
    for line in lines[2:]:
        line = line.strip()
        if line and 'Any' not in line:
            # Parse version - may include "build XXX" suffix
            # Format: "6.6 build 002" or "32.30.0(1575420)" or just "6.6"
            parts = line.split()
            if parts:
                version = parts[0]
                # Validate it looks like a version (starts with a digit)
                if not version[0].isdigit():
                    continue
                # Check if next parts are "build XXX"
                if len(parts) >= 3 and parts[1].lower() == 'build':
                    version = f"{parts[0]} build {parts[2]}"
                versions.append(version)

    # If Morphe CLI only returned a tiny "most common" list (or nothing),
    # attempt to derive a fuller candidate set from `list-patches`.
    if is_morphe_cli and len(versions) <= 1:
        try:
            alt_cmd = [
                "java", "-jar", cli,
                "list-patches",
                "--with-packages",
                "--with-versions",
                patches,
            ]
            alt_out = run_process(alt_cmd, capture=True, silent=True, check=False) or ""
            derived: list[str] = []
            for ln in alt_out.splitlines():
                if package_name not in ln:
                    continue
                # Grab any versions mentioned on the same line as the package name.
                for m in re.finditer(r"\d+(?:\.\d+)+(?:\(\d+\))?", ln):
                    derived.append(m.group(0))
            if derived:
                versions.extend(derived)
        except Exception:
            pass

    if not versions:
        logging.warning("No supported versions found")
        return []

    # Sort highest -> lowest.
    versions = sorted(set(versions), key=normalize_version, reverse=True)
    logging.info(f"CLI parsed versions: {versions}")
    return versions


def get_supported_version(package_name: str, cli: str, patches: str) -> Optional[str]:
    """Backwards compatible helper: returns the highest compatible version, if any."""
    versions = get_supported_versions(package_name, cli, patches)
    return versions[0] if versions else None

def extract_filename(response, fallback_url=None) -> str:
    cd = response.headers.get('content-disposition')
    if cd:
        _, params = parse_header(cd)
        filename = params.get('filename') or params.get('filename*')
        if filename:
            return unquote(filename)

    parsed = urlparse(response.url)
    query_params = parse_qs(parsed.query)
    rcd = query_params.get('response-content-disposition')
    if rcd:
        _, params = parse_header(unquote(rcd[0]))
        filename = params.get('filename') or params.get('filename*')
        if filename:
            return unquote(filename)

    path = urlparse(fallback_url or response.url).path
    return unquote(Path(path).name)

def gh_api_request(endpoint: str) -> dict:
    """Make a GitHub API request using the 'gh' CLI as it handles tokens more robustly in Actions"""
    env = os.environ.copy()
    # Ensure GH_TOKEN is set for the gh cli
    if "GITHUB_TOKEN" in env and "GH_TOKEN" not in env:
        env["GH_TOKEN"] = env["GITHUB_TOKEN"]
        
    try:
        result = subprocess.run(
            ["gh", "api", endpoint],
            capture_output=True,
            text=True,
            env=env,
            check=True
        )
        return json.loads(result.stdout)
    except Exception as e:
        logging.debug(f"gh api {endpoint} failed: {e}")
        raise


def fetch_json(url: str, headers: dict | None = None) -> dict | list:
    response = session.get(url, headers=headers or {})
    response.raise_for_status()
    return response.json()


def normalize_source_entry(entry: dict) -> dict:
    provider = (entry.get("provider") or "github").lower().strip()
    tag = (entry.get("tag") or "latest").strip() or "latest"

    if provider in ("github", "codeberg"):
        user = (entry.get("user") or "").strip()
        repo = (entry.get("repo") or "").strip()
        if not user or not repo:
            raise ValueError(f"{provider} source entries require user and repo")
        return {
            "provider": provider,
            "tag": tag,
            "user": user,
            "repo": repo,
            "identity": f"{user}/{repo}",
        }

    if provider == "gitlab":
        project = (entry.get("project") or "").strip()
        if not project:
            raise ValueError("gitlab source entries require project")
        return {
            "provider": provider,
            "tag": tag,
            "project": project,
            "identity": project,
        }

    raise ValueError(f"Unsupported source provider: {provider}")


def normalize_release(tag_name: str, published_at: str, assets: list[dict]) -> dict:
    return {
        "tag_name": tag_name or "?",
        "published_at": published_at or "?",
        "assets": [
            {
                "name": asset.get("name", ""),
                "browser_download_url": asset.get("browser_download_url")
                or asset.get("direct_asset_url")
                or asset.get("url", ""),
            }
            for asset in assets
            if asset.get("name")
        ],
    }


def detect_release(entry: dict) -> dict:
    normalized = normalize_source_entry(entry)
    provider = normalized["provider"]

    if provider == "github":
        release = detect_github_release(normalized["user"], normalized["repo"], normalized["tag"])
        return normalize_release(
            release.get("tag_name"),
            release.get("published_at") or release.get("created_at"),
            release.get("assets") or [],
        )

    if provider == "gitlab":
        return detect_gitlab_release(normalized["project"], normalized["tag"])

    if provider == "codeberg":
        return detect_codeberg_release(normalized["user"], normalized["repo"], normalized["tag"])

    raise ValueError(f"Unsupported source provider: {provider}")


def detect_gitlab_release(project: str, tag: str) -> dict:
    encoded = quote(project, safe="")
    if tag == "latest":
        data = fetch_json(f"https://gitlab.com/api/v4/projects/{encoded}/releases/permalink/latest")
    elif tag in ("", "dev", "prerelease"):
        releases = fetch_json(f"https://gitlab.com/api/v4/projects/{encoded}/releases")
        if not isinstance(releases, list) or not releases:
            raise ValueError(f"No releases found for GitLab project {project}")
        data = releases[0]
    else:
        data = fetch_json(f"https://gitlab.com/api/v4/projects/{encoded}/releases/{quote(tag, safe='')}")

    if not isinstance(data, dict):
        raise ValueError(f"Unexpected GitLab release response shape for {project}/{tag}")
    assets = (data.get("assets") or {}).get("links") or []
    return normalize_release(data.get("tag_name"), data.get("released_at"), assets)


def detect_codeberg_release(user: str, repo: str, tag: str) -> dict:
    base = f"https://codeberg.org/api/v1/repos/{user}/{repo}/releases"
    if tag == "latest":
        data = fetch_json(f"{base}/latest")
    elif tag in ("", "dev", "prerelease"):
        releases = fetch_json(base)
        if not isinstance(releases, list) or not releases:
            raise ValueError(f"No releases found for Codeberg repo {user}/{repo}")
        data = releases[0]
    else:
        data = fetch_json(f"{base}/tags/{quote(tag, safe='')}")

    if not isinstance(data, dict):
        raise ValueError(f"Unexpected Codeberg release response shape for {user}/{repo}/{tag}")
    return normalize_release(data.get("tag_name"), data.get("published_at"), data.get("assets") or [])

def detect_github_release(user: str, repo: str, tag: str) -> dict:
    if tag == "latest":
        release_lookup = "latest"
    elif tag in ["", "dev", "prerelease"]:
        release_lookup = tag or "most recent"
    else:
        release_lookup = tag

    # Small sleep to avoid hammering the API and mitigate transient 401s
    time.sleep(1)

    max_retries = 3
    for attempt in range(max_retries):
        try:
            # Prefer 'gh' CLI as it handles tokens more robustly in Actions environment
            if attempt < 2:
                logging.info(f"Fetching release {tag} for {user}/{repo} (attempt {attempt + 1})...")
                
                if tag == "latest":
                    data = gh_api_request(f"repos/{user}/{repo}/releases/latest")
                    return data
                elif tag in ["", "dev", "prerelease"]:
                    data = gh_api_request(f"repos/{user}/{repo}/releases")
                    if not isinstance(data, list):
                        # Handle case where API might return a single object (unlikely for /releases)
                        data = [data]
                        
                    if not data:
                        raise ValueError(f"No releases found for {user}/{repo}")
                    
                    if tag == "":
                        release = max(data, key=lambda x: x['created_at'])
                    elif tag == "dev":
                        devs = [r for r in data if 'dev' in r['tag_name'].lower()]
                        if not devs:
                            raise ValueError(f"No dev release found for {user}/{repo}")
                        release = max(devs, key=lambda x: x['created_at'])
                    else:
                        pres = [r for r in data if r['prerelease']]
                        if not pres:
                            raise ValueError(f"No prerelease found for {user}/{repo}")
                        release = max(pres, key=lambda x: x['created_at'])
                    return release
                else:
                    data = gh_api_request(f"repos/{user}/{repo}/releases/tags/{tag}")
                    return data
            else:
                # Last ditch effort with PyGithub if gh CLI fails
                logging.warning(f"Falling back to PyGithub for {user}/{repo}...")
                repo_obj = gh.get_repo(f"{user}/{repo}")
                if tag == "latest":
                    release = repo_obj.get_latest_release()
                    return release.raw_data
                # ... other cases omitted for brevity as gh CLI is preferred ...
                return repo_obj.get_release(tag).raw_data
                    
        except Exception as e:
            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 3
                logging.warning(f"Attempt {attempt + 1} failed for {user}/{repo}: {e}. Retrying in {wait_time}s...")
                time.sleep(wait_time)
                continue
            
            # Special message for 401 on external repos
            err_msg = str(e).lower()
            if "401" in err_msg or "unauthorized" in err_msg or "bad credentials" in err_msg:
                is_external = user.lower() not in (os.environ.get("GITHUB_REPOSITORY", "").lower())
                if is_external:
                    logging.error(
                        "❌ 401 Unauthorized for external repository %s/%s. "
                        "The default GITHUB_TOKEN in Actions cannot access private external repositories. "
                        "If this repo is private, please use a Personal Access Token (PAT) with 'repo' scope "
                        "stored as a secret (e.g., CUSTOM_GH_TOKEN) and update your workflow.",
                        user, repo
                    )
                raise RuntimeError("Bad GitHub credentials for release lookup") from e
            
            logging.error(f"Error fetching release {tag} for {user}/{repo} after {max_retries} attempts: {e}")
            raise

def detect_source_type(cli_file: Path, patches_file: Path) -> str:
    """Detect if we're using Morphe or ReVanced based on downloaded files"""
    if cli_file and "morphe" in cli_file.name.lower() and patches_file and patches_file.suffix == ".mpp":
        return "morphe"
    elif cli_file and "revanced" in cli_file.name.lower() and patches_file and patches_file.suffix in [".jar", ".rvp"]:
        return "revanced"
    return "unknown"
