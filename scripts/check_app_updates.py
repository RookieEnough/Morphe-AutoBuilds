#!/usr/bin/env python3
"""
Incremental update checker for Morphe AutoBuilds.

Strategy:
1. Read patch-config.json + arch-config.json -> full expected matrix.
2. Fetch existing 'latest' release manifest (manifest.json asset, if present).
3. For each (app, source, arch):
   - Determine current configured app version (from apps/<platform>/<app>.json).
   - Determine current patch-source signature (latest GitHub release tag(s) of
     repos listed in sources/<source>.json).
   - Compare to manifest.json -> if changed OR APK missing -> needs build.
4. Output:
   - GitHub Actions outputs: build_matrix (JSON), has_updates, total/update counts.
   - File: build_matrix.json    (matrix entries that need rebuild).
   - File: carry_over.json      (existing APK names to re-upload unchanged).
   - File: new_manifest.json    (manifest to upload with the new release).
   - File: stale_assets.json    (release APK assets no longer in the manifest).

Force full rebuild: env FORCE_FULL_REBUILD=true (also: any app missing from the
old manifest is rebuilt automatically).

Fail-safe: any unexpected error -> full rebuild matrix is emitted (preserves the
previous always-build behavior so nothing breaks).
"""
import os
import sys
import json
import logging
import subprocess
import traceback
from pathlib import Path
from typing import Dict, List, Tuple, Optional

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PATCH_CONFIG = REPO_ROOT / "patch-config.json"
ARCH_CONFIG = REPO_ROOT / "arch-config.json"
SOURCES_DIR = REPO_ROOT / "sources"
APPS_DIR = REPO_ROOT / "apps"

MANIFEST_NAME = "manifest.json"
RELEASE_TAG = "latest"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
FORCE_FULL = os.environ.get("FORCE_FULL_REBUILD", "false").lower() in ("true", "1", "yes")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def write_gh_output(key: str, value: str) -> None:
    """Append key=value (multiline-safe) to GITHUB_OUTPUT, or print locally."""
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        preview = value if len(value) < 200 else value[:200] + "..."
        print(f"[gh-output] {key}={preview}")
        return
    with open(out, "a", encoding="utf-8") as f:
        if "\n" in value:
            f.write(f"{key}<<EOF_GH\n{value}\nEOF_GH\n")
        else:
            f.write(f"{key}={value}\n")


def run_gh(args: List[str], timeout: int = 120) -> Tuple[int, str, str]:
    """Run `gh ...`; returns (rc, stdout, stderr). Never raises."""
    env = os.environ.copy()
    if GITHUB_TOKEN and "GH_TOKEN" not in env:
        env["GH_TOKEN"] = GITHUB_TOKEN
    try:
        p = subprocess.run(
            ["gh", *args],
            capture_output=True, text=True, env=env, timeout=timeout,
        )
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", "gh CLI not found"
    except Exception as e:
        return 1, "", f"{e}"


def load_patch_config() -> List[dict]:
    with PATCH_CONFIG.open("r", encoding="utf-8") as f:
        return json.load(f).get("patch_list", [])


def load_arch_config() -> Dict[Tuple[str, str], List[str]]:
    if not ARCH_CONFIG.exists():
        return {}
    with ARCH_CONFIG.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return {
        (e["app_name"], e["source"]): e.get("arches", ["universal"])
        for e in data
    }


def load_app_config_version(app_name: str) -> str:
    """Return the configured 'version' field from the first matching app config,
    or '' if none is pinned (means 'latest at build time')."""
    for platform in ("apkmirror", "apkpure", "uptodown", "aptoide"):
        fp = APPS_DIR / platform / f"{app_name}.json"
        if fp.exists():
            try:
                with fp.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                return (data.get("version") or "").strip()
            except Exception:
                continue
    return ""


# ---------------------------------------------------------------------------
# Source-signature: detect when patch repos publish new releases
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import utils as provider_utils

_repo_sig_cache: Dict[Tuple[str, str, str, str], str] = {}


def fetch_repo_signature(user: str, repo: str, tag: str, provider: str = "github") -> str:
    """Get a stable identifier for the current state of a repo's release.
    Returns 'tag_name@published_at' on success, else a short error sentinel."""
    key = (user, repo, tag, provider)
    if key in _repo_sig_cache:
        return _repo_sig_cache[key]

    try:
        if provider == "gitlab":
            # For GitLab we store the project path in the `repo` slot (see
            # get_source_signature), e.g. "Group/Subgroup/Project".
            sig = _fetch_gitlab_signature(repo, tag)
        elif provider == "codeberg":
            sig = _fetch_codeberg_signature(user, repo, tag)
        else:
            sig = _fetch_github_signature(user, repo, tag)
        _repo_sig_cache[key] = sig
        return sig
    except Exception as e:
        sig = f"err:{tag}"
        _repo_sig_cache[key] = sig
        logging.warning(f"  {provider} api failed for {user}/{repo}: {e}")
        return sig


def _fetch_github_signature(user: str, repo: str, tag: str) -> str:
    if tag == "latest":
        api = f"repos/{user}/{repo}/releases/latest"
    elif tag in ("", "dev", "prerelease"):
        api = f"repos/{user}/{repo}/releases?per_page=10"
    else:
        api = f"repos/{user}/{repo}/releases/tags/{tag}"

    rc, out, err = run_gh(["api", api])
    if rc != 0:
        raise RuntimeError(f"gh api {api} failed: {err.strip()[:80]}")

    data = json.loads(out)

    if isinstance(data, list):
        if tag == "dev":
            data = [r for r in data if "dev" in (r.get("tag_name") or "").lower()]
        elif tag == "prerelease":
            data = [r for r in data if r.get("prerelease")]
        if not data:
            raise RuntimeError(f"No releases found for {user}/{repo}")
        data.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        rel = data[0]
    else:
        rel = data

    tag_name = rel.get("tag_name") or rel.get("name") or "?"
    published = rel.get("published_at") or rel.get("created_at") or "?"
    updated = rel.get("updated_at") or ""

    # Some upstream projects update/re-upload release assets without bumping the
    # release tag. In that case, published_at/tag_name may stay the same, so we
    # also incorporate a deterministic "assets signature" to detect changes.
    assets = rel.get("assets") or []
    asset_parts: List[str] = []
    if isinstance(assets, list):
        for a in assets:
            if not isinstance(a, dict):
                continue
            name = (a.get("name") or "").strip()
            if not name:
                continue
            # Prefer strong identifiers when available.
            digest = (a.get("digest") or "").strip()
            a_updated = (a.get("updated_at") or "").strip()
            size = str(a.get("size") or "").strip()
            # Keep this compact but sensitive to real changes.
            token = digest or f"{size}@{a_updated}" or a_updated or size
            asset_parts.append(f"{name}:{token}")
    asset_parts.sort()
    assets_sig = ",".join(asset_parts)

    # Format: <tag>@<published>@<updated>|<assets_sig>
    return f"{tag_name}@{published}@{updated}|{assets_sig}"


def _fetch_gitlab_signature(project: str, tag: str) -> str:
    from urllib.parse import quote
    encoded = quote(project, safe="")
    if tag == "latest":
        api = f"https://gitlab.com/api/v4/projects/{encoded}/releases/permalink/latest"
    elif tag in ("", "dev", "prerelease"):
        api = f"https://gitlab.com/api/v4/projects/{encoded}/releases"
    else:
        api = f"https://gitlab.com/api/v4/projects/{encoded}/releases/{quote(tag, safe='')}"

    data = provider_utils.fetch_json(api)
    if isinstance(data, list):
        data = data[0] if data else {}
    tag_name = data.get("tag_name") or "?"
    published = data.get("released_at") or data.get("created_at") or "?"
    return f"{tag_name}@{published}"


def _fetch_codeberg_signature(user: str, repo: str, tag: str) -> str:
    from urllib.parse import quote
    base = f"https://codeberg.org/api/v1/repos/{user}/{repo}/releases"
    if tag == "latest":
        api = f"{base}/latest"
    elif tag in ("", "dev", "prerelease"):
        api = base
    else:
        api = f"{base}/tags/{quote(tag, safe='')}"

    data = provider_utils.fetch_json(api)
    tag_name = data.get("tag_name") or "?"
    published = data.get("published_at") or "?"
    return f"{tag_name}@{published}"


_source_sig_cache: Dict[str, str] = {}


def get_source_signature(source: str) -> str:
    """Combine release signatures of every repo declared in sources/<source>.json
    into a single deterministic string."""
    if source in _source_sig_cache:
        return _source_sig_cache[source]

    src_file = SOURCES_DIR / f"{source}.json"
    if not src_file.exists():
        # Case-insensitive fallback
        for f in SOURCES_DIR.glob("*.json"):
            if f.stem.lower() == source.lower():
                src_file = f
                break
    if not src_file.exists():
        sig = f"missing-source:{source}"
        _source_sig_cache[source] = sig
        return sig

    try:
        with src_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        sig = f"unparseable:{e}"
        _source_sig_cache[source] = sig
        return sig

    if isinstance(data, dict) and "bundle_url" in data:
        sig = f"bundle:{data['bundle_url']}"
        _source_sig_cache[source] = sig
        return sig

    parts: List[str] = []
    if isinstance(data, list):
        for entry in data:
            if not isinstance(entry, dict):
                continue
            provider = (entry.get("provider") or "github").lower().strip()
            tag = entry.get("tag", "latest")
            
            if provider == "gitlab":
                project = entry.get("project")
                if project:
                    parts.append(f"gitlab:{project}@{fetch_repo_signature('', project, tag, provider)}")
            elif provider == "codeberg":
                user = entry.get("user")
                repo = entry.get("repo")
                if user and repo:
                    parts.append(f"codeberg:{user}/{repo}@{fetch_repo_signature(user, repo, tag, provider)}")
            else:
                user = entry.get("user")
                repo = entry.get("repo")
                if user and repo:
                    parts.append(f"{user}/{repo}@{fetch_repo_signature(user, repo, tag, provider)}")

    sig = ";".join(parts) if parts else f"empty:{source}"
    _source_sig_cache[source] = sig
    return sig


# ---------------------------------------------------------------------------
# Existing release manifest + assets
# ---------------------------------------------------------------------------
def _get_repo_owner_name() -> Optional[Tuple[str, str]]:
    repo = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
    if "/" not in repo:
        return None
    owner, name = repo.split("/", 1)
    owner = owner.strip()
    name = name.strip()
    if not owner or not name:
        return None
    return owner, name


def fetch_existing_manifest() -> Optional[dict]:
    args = ["release", "download", RELEASE_TAG, "--pattern", MANIFEST_NAME, "--clobber"]
    repo = _get_repo_owner_name()
    if repo:
        owner, name = repo
        args.extend(["--repo", f"{owner}/{name}"])

    rc, _, err = run_gh(args)
    if rc != 0:
        msg = err.strip()[:120]
        logging.info(f"No existing '{MANIFEST_NAME}' on '{RELEASE_TAG}' ({msg})")
        return None
    try:
        with open(MANIFEST_NAME, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.warning(f"Bad manifest.json: {e}")
        return None


def fetch_existing_apk_names() -> List[str]:
    repo = _get_repo_owner_name()
    if repo:
        owner, name = repo
        rc, out, _ = run_gh(
            ["api", f"repos/{owner}/{name}/releases/tags/{RELEASE_TAG}", "--jq", ".id"]
        )
        rel_id = out.strip() if rc == 0 else ""
        if rel_id:
            rc, out, _ = run_gh(
                [
                    "api",
                    "--paginate",
                    f"repos/{owner}/{name}/releases/{rel_id}/assets?per_page=100",
                    "--jq",
                    ".[].name",
                ],
                timeout=300,
            )
            if rc == 0:
                names = [ln.strip() for ln in out.splitlines() if ln.strip()]
                return [n for n in names if n.endswith(".apk")]

    rc, out, _ = run_gh(["release", "view", RELEASE_TAG, "--json", "assets"])
    if rc != 0:
        return []
    try:
        return [
            a.get("name", "")
            for a in json.loads(out).get("assets", [])
            if a.get("name", "").endswith(".apk")
        ]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Matrix planning
# ---------------------------------------------------------------------------
def build_full_matrix() -> List[dict]:
    """Expand patch-config + arch-config into the full per-arch matrix."""
    patch_list = load_patch_config()
    arch_map = load_arch_config()
    matrix: List[dict] = []
    seen = set()
    for entry in patch_list:
        app = entry.get("app_name")
        src = entry.get("source")
        if not app or not src:
            continue
        arches = arch_map.get((app, src), ["universal"])
        for arch in arches:
            key = (app, src, arch)
            if key in seen:
                continue
            seen.add(key)
            matrix.append({"app_name": app, "source": src, "arch": arch})
    return matrix


def make_manifest_key(app: str, source: str, arch: str) -> str:
    return f"{app}|{source}|{arch}"


def _is_unreliable_source_sig(sig: str) -> bool:
    s = (sig or "").lower()
    return (
        "@err:" in s
        or "@badjson:" in s
        or s.startswith("missing-source:")
        or s.startswith("unparseable:")
    )


def _recover_apk_from_release(app: str, arch: str, existing_apks: List[str]) -> str:
    a = (app or "").lower()
    rarch = (arch or "").lower()
    candidates: List[str] = []
    for n in existing_apks:
        nl = (n or "").lower()
        if not nl.endswith(".apk"):
            continue
        if not nl.startswith(f"{a}-{rarch}-"):
            continue
        candidates.append(n)
    candidates.sort()
    return candidates[-1] if candidates else ""




def plan_incremental(full_matrix: List[dict], old_manifest: Optional[dict],
                     existing_apks: List[str]) -> Tuple[List[dict], List[str], dict, List[str]]:
    """Decide which entries need rebuilding.
    Returns (build_matrix, carry_over_apks, new_manifest_entries, stale_apks)."""
    old_entries = (old_manifest or {}).get("entries", {}) if isinstance(old_manifest, dict) else {}
    existing_apk_set = set(existing_apks)

    build_matrix: List[dict] = []
    carry_over: List[str] = []
    new_entries: dict = {}

    for entry in full_matrix:
        app = entry["app_name"]
        src = entry["source"]
        arch = entry["arch"]
        mkey = make_manifest_key(app, src, arch)

        cur_app_ver = load_app_config_version(app)            # '' if 'latest'
        cur_src_sig = get_source_signature(src)
        old = old_entries.get(mkey)
        old_src_sig = (old or {}).get("source_sig", "")
        if old and old_src_sig and _is_unreliable_source_sig(cur_src_sig):
            cur_src_sig = old_src_sig
        carried_apk = (old or {}).get("apk", "")
        if old:
            if not carried_apk or carried_apk not in existing_apk_set:
                recovered = _recover_apk_from_release(app, arch, existing_apks)
                if recovered:
                    carried_apk = recovered

        new_entries[mkey] = {
            "app_name": app,
            "source": src,
            "arch": arch,
            "config_version": cur_app_ver,
            "source_sig": cur_src_sig,
            # apk filename is filled in *after* build by the workflow; for now
            # carry over whatever the old manifest had so we know what to keep.
            "apk": carried_apk,
        }

        reasons: List[str] = []
        if FORCE_FULL:
            reasons.append("force-rebuild")
        if not old:
            reasons.append("new-entry")
        else:
            if old.get("config_version", "") != cur_app_ver:
                reasons.append(f"app-version: {old.get('config_version','')!r}->{cur_app_ver!r}")
            if old.get("source_sig", "") != cur_src_sig:
                reasons.append("patch-source-updated")
            old_apk = carried_apk
            if old_apk and old_apk not in existing_apk_set:
                reasons.append("apk-missing-from-release")
            if not old_apk:
                reasons.append("no-apk-recorded")

        if reasons:
            logging.info(f"  REBUILD {app}/{src}/{arch}: {'; '.join(reasons)}")
            build_matrix.append(entry)
        else:
            old_apk = carried_apk
            if old_apk and old_apk in existing_apk_set:
                carry_over.append(old_apk)
                logging.info(f"  carry  {app}/{src}/{arch}: {old_apk}")
            else:
                # Defensive: if we can't carry it, we must rebuild.
                logging.info(f"  REBUILD {app}/{src}/{arch}: no carry-over apk")
                build_matrix.append(entry)

    # The build job in src/__main__.py builds ALL arches for an (app, source) in
    # one matrix run (it iterates arches from arch-config.json itself). To avoid
    # duplicate work and to keep the workflow contract unchanged, we deduplicate
    # the build matrix on (app, source) -- if ANY arch needs rebuild, the whole
    # (app, source) gets rebuilt and the resulting APKs replace those arches in
    # the carry-over set.
    deduped: List[dict] = []
    seen_pairs = set()
    for e in build_matrix:
        pair = (e["app_name"], e["source"])
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        # Strip 'arch' from the matrix entry to match the original schema that
        # the existing build-apps job expects.
        deduped.append({"app_name": e["app_name"], "source": e["source"]})

    # Drop carry-overs whose (app, source) is being rebuilt -- the rebuild will
    # produce fresh APKs for ALL arches of that pair, so the old ones are stale.
    rebuilding_pairs = seen_pairs
    filtered_carry: List[str] = []
    for apk in carry_over:
        # Determine the (app, source) of this APK by looking up the manifest entry.
        owner_pair = None
        for ekey, eval_ in new_entries.items():
            if eval_.get("apk") == apk:
                owner_pair = (eval_["app_name"], eval_["source"])
                break
        if owner_pair is None or owner_pair not in rebuilding_pairs:
            filtered_carry.append(apk)
        else:
            logging.info(f"  drop carry {apk}: its (app,source) is rebuilding")

    desired_apks = {
        e.get("apk", "")
        for e in new_entries.values()
        if e.get("apk", "")
    }
    stale_apks = sorted(
        n for n in existing_apks
        if n.endswith(".apk") and n not in desired_apks
    )
    for apk in stale_apks:
        logging.info(f"  stale release asset: {apk}")

    return deduped, filtered_carry, new_entries, stale_apks


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def emit_full_rebuild(reason: str) -> None:
    """Emergency fallback: build everything (preserves the previous behavior)."""
    logging.warning(f"Falling back to FULL rebuild: {reason}")
    full = build_full_matrix()
    Path("build_matrix.json").write_text(json.dumps(full), encoding="utf-8")
    Path("carry_over.json").write_text(json.dumps([]), encoding="utf-8")
    Path("stale_assets.json").write_text(json.dumps([]), encoding="utf-8")
    # Empty manifest -> next run will treat everything as 'new-entry' until a
    # successful build writes a fresh manifest.
    Path("new_manifest.json").write_text(
        json.dumps({"entries": {}}, indent=2), encoding="utf-8")
    write_gh_output("build_matrix", json.dumps(full))
    write_gh_output("has_updates", "true" if full else "false")
    write_gh_output("update_count", str(len(full)))
    write_gh_output("total_count", str(len(full)))
    write_gh_output("carry_count", "0")
    write_gh_output("stale_count", "0")
    write_gh_output("incremental", "false")


def main() -> int:
    try:
        full = build_full_matrix()
        logging.info(f"Full matrix: {len(full)} (app, source, arch) entries")

        if FORCE_FULL:
            logging.info("FORCE_FULL_REBUILD=true -> rebuilding everything")
            old_manifest = None
        else:
            old_manifest = fetch_existing_manifest()

        existing_apks = fetch_existing_apk_names()
        logging.info(f"Existing release has {len(existing_apks)} APK assets")

        if old_manifest is None and not FORCE_FULL:
            # No manifest yet -> first incremental run; rebuild everything once
            # to populate it. (Future runs will be incremental.)
            emit_full_rebuild("no manifest in existing release (first incremental run)")
            return 0

        build_mx, carry_over, new_entries, stale_apks = plan_incremental(
            full, old_manifest, existing_apks)

        Path("build_matrix.json").write_text(json.dumps(build_mx), encoding="utf-8")
        Path("carry_over.json").write_text(json.dumps(carry_over), encoding="utf-8")
        Path("new_manifest.json").write_text(
            json.dumps({"entries": new_entries}, indent=2), encoding="utf-8")
        Path("stale_assets.json").write_text(json.dumps(stale_apks), encoding="utf-8")

        write_gh_output("build_matrix", json.dumps(build_mx))
        write_gh_output("has_updates", "true" if build_mx or stale_apks else "false")
        write_gh_output("update_count", str(len(build_mx)))
        write_gh_output("total_count", str(len(full)))
        write_gh_output("carry_count", str(len(carry_over)))
        write_gh_output("stale_count", str(len(stale_apks)))
        write_gh_output("incremental", "true")

        logging.info("=" * 60)
        logging.info(f"  Total entries:     {len(full)}")
        logging.info(f"  Need rebuild:      {len(build_mx)}")
        logging.info(f"  Carry over:        {len(carry_over)}")
        logging.info(f"  Stale APK assets:  {len(stale_apks)}")
        logging.info("=" * 60)

        return 0

    except Exception as e:
        logging.error(f"check_app_updates failed: {e}")
        traceback.print_exc()
        emit_full_rebuild(f"unexpected error: {e}")
        return 0  # Never fail the workflow over a planning error.


if __name__ == "__main__":
    sys.exit(main())
