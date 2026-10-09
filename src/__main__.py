import json
import logging
import re
import os
from sys import exit
from pathlib import Path
from os import getenv
import subprocess
from src import (
    r2,
    utils,
    release,
    downloader
)

def _should_retry_with_older_version(output: str | None) -> bool:
    """Detect common patterns that indicate the chosen app version is not
    actually compatible with the selected patches (fingerprint mismatch, etc.)."""
    if not output:
        return False
    t = output.lower()
    return (
        "failed to match the fingerprint" in t
        or "patch.patchexception" in t
        or ("fingerprint" in t and "failed" in t)
        or "patching aborted" in t
    )

_APPLYING_ZERO_PATCHES = re.compile(r"applying\s+0\s+patches\b", re.IGNORECASE)
_FILTERING_PATCHES = re.compile(
    r"Filtering patches for\s+(\S+)\s+v(\S+(?:\s+build\s+\d+|\s*\(\d+\))?)",
    re.IGNORECASE,
)
_BUILD_SUFFIX = re.compile(r"(?:\s+build\s+\d+|\s*\(\d+\))\s*$", re.IGNORECASE)

def _version_without_build(version: str) -> str:
    """Drop a trailing `` build <digits>`` or parenthetical build number."""
    return _BUILD_SUFFIX.sub("", version).strip()

def _unpatched_build_reason(output: str | None, downloaded_version: str) -> str | None:
    """Return why a zero-exit patch CLI run must not be signed, or None to accept.

    An explicit ``Applying 0 patches`` count is a failed build. A
    ``Filtering patches for <package> v<version>`` line is also a failure when
    that version is not the APK just downloaded. A skipped patch with a
    positive apply count and a matching version is a normal build. Missing
    apply-count text is not evidence of zero; a non-zero CLI exit is handled
    by the caller and must not be passed here.
    """
    if not output:
        return None

    if _APPLYING_ZERO_PATCHES.search(output):
        return (
            f"Patch CLI applied 0 patches to v{downloaded_version}; "
            "refusing to sign an unpatched APK"
        )

    downloaded = utils.normalize_version(_version_without_build(downloaded_version))
    for match in _FILTERING_PATCHES.finditer(output):
        package, filtered = match.group(1), match.group(2).strip()
        # The patch CLI truncates long versions with a trailing '...' in its
        # 'Filtering patches' line (e.g. v18.0.3.954559732-release-arm64-v8a...).
        # Drop it so the version comparison below is not a false mismatch.
        filtered = filtered.rstrip('.')
        if utils.normalize_version(_version_without_build(filtered)) != downloaded:
            return (
                f"Patch CLI filtered {package} v{filtered}, "
                f"which is not the downloaded v{downloaded_version}"
            )
    return None

def run_build(app_name: str, source: str, arch: str = "universal") -> str:
    """Build APK for specific architecture"""
    download_files, name = downloader.download_required(source)

    # Log downloaded files for debugging
    logging.info(f"📦 Downloaded {len(download_files)} files for {source}:")
    for file in download_files:
        logging.info(f"  - {file.name} ({file.stat().st_size} bytes)")

    # DETECT SOURCE TYPE BASED ON DOWNLOADED FILES
    is_morphe = False
    is_revanced = False

    # Check file contents to determine source type
    for file in download_files:
        if "morphe-cli" in file.name.lower():
            is_morphe = True
            break
        elif "revanced-cli" in file.name.lower():
            is_revanced = True
            break

    # If not detected by CLI name, check patch file extension
    if not is_morphe and not is_revanced:
        for file in download_files:
            if file.suffix == ".mpp":
                is_morphe = True
                break
            elif file.suffix in [".rvp", ".jar"] and "patches" in file.name.lower():
                is_revanced = True
                break

    # If still not detected, fallback to source name
    if not is_morphe and not is_revanced:
        is_morphe = "morphe" in source.lower() or "custom" in source.lower()
        is_revanced = not is_morphe  # Default to ReVanced if not Morphe

    logging.info(f"🔍 Detected: {'Morphe' if is_morphe else 'ReVanced'} source type")

    # FIND FILES BASED ON DETECTED TYPE
    if is_morphe:
        # Find Morphe files - prefer non-dev version
        cli = utils.find_file(download_files, contains="morphe-cli", suffix=".jar", exclude=["dev"])
        if not cli:
            # Fallback to any Morphe CLI
            cli = utils.find_file(download_files, contains="morphe", suffix=".jar")
        
        if not cli:
            cli = utils.find_file(download_files, suffix=".jar")
        patches = utils.find_file(download_files, contains="patches", suffix=".mpp")
        if not patches:
            # Fallback to any .mpp file
            patches = utils.find_file(download_files, suffix=".mpp")
    else:
        # Find ReVanced files
        cli = utils.find_file(download_files, contains="revanced-cli", suffix=".jar")
        patches = utils.find_file(download_files, contains="patches", suffix=".rvp")
        
        if not patches:
            # Try .jar extension for patches
            patches = utils.find_file(download_files, contains="patches", suffix=".jar")

    # Validate tools
    if not cli:
        logging.error(f"❌ CLI not found for source: {source}")
        logging.error(f"Available files: {[f.name for f in download_files]}")
        return None
    if not patches:
        logging.error(f"❌ Patches not found for source: {source}")
        logging.error(f"Available files: {[f.name for f in download_files]}")
        return None

    logging.info(f"✅ Using CLI: {cli.name}")
    logging.info(f"✅ Using patches: {patches.name}")

    download_methods = [
        downloader.download_apkmirror,
        downloader.download_apkpure,
        downloader.download_aptoide,
        downloader.download_uptodown,
        downloader.download_apkfab,
        downloader.download_github,
        downloader.download_codeberg,
        downloader.download_apkcombo,
    ]

    # Facebook: Codeberg only. No mirror fallbacks, fail if Codeberg fails.
    if app_name == "facebook":
        download_methods = [downloader.download_codeberg]
        logging.info("Facebook: using Codeberg only (no mirror fallbacks)")

    input_apk = None
    version = None
    candidates: list[str] = []
    used_method = None
    for method in download_methods:
        apk_path, ver, cands = method(app_name, str(cli), str(patches), arch)
        if not apk_path:
            continue
        # A corrupt download must never reach the patcher: repair it, and if
        # it is still unusable, discard it and try the next source.
        apk_path = utils.ensure_usable_apk(apk_path, app_name, ver or "")
        if apk_path is None:
            logging.warning(f"Discarding unusable download from {method.__name__}; trying next source")
            continue
        input_apk, version, candidates = apk_path, ver, cands
        used_method = method
        break

    if input_apk is None or not used_method or not version:
        logging.error(f"❌ Failed to download APK for {app_name}")
        logging.error("All download sources failed. Skipping this app.")
        return None

    # Try the downloaded version first, then (if available) older compatible
    # versions from the patch set. This prevents a single bad/overstated
    # compatibility entry from breaking the whole build.
    versions_to_try: list[str] = [version]
    if candidates and version in candidates:
        versions_to_try += [v for v in candidates if v != version]

    exclude_patches = []
    include_patches = []

    patches_path = Path("patches") / f"{app_name}-{source}.txt"
    if patches_path.exists():
        with patches_path.open('r') as patches_file:
            for line in patches_file:
                line = line.strip()
                if line.startswith('-'):
                    exclude_patches.extend(["-d", line[1:].strip()])
                elif line.startswith('+'):
                    # Inline patch options: + Patch name {key=value, key2=value2}
                    # become: -e "Patch name" -Okey=value -Okey2=value2
                    name_opts = line[1:].strip()
                    opts: list[str] = []
                    if "{" in name_opts and name_opts.rstrip().endswith("}"):
                        name_part, opts_part = name_opts.split("{", 1)
                        name_opts = name_part.strip()
                        for opt in opts_part.rstrip("}").split(","):
                            opt = opt.strip()
                            if opt:
                                opts.append(f"-O{opt}")
                    include_patches.extend(["-e", name_opts, *opts])

    for attempt_idx, ver in enumerate(versions_to_try):
        if attempt_idx > 0:
            logging.warning(
                f"Retrying {app_name}/{source}/{arch} with older version {ver} due to patch failure..."
            )
            # Cleanup any previous attempt artifacts.
            try:
                input_apk.unlink(missing_ok=True)
            except Exception:
                pass

            input_apk, version, _ = used_method(app_name, str(cli), str(patches), arch, override_version=ver)
            if input_apk is None:
                continue
            input_apk = utils.ensure_usable_apk(input_apk, app_name, ver)
            if input_apk is None:
                logging.warning(f"Re-downloaded APK for {ver} is unusable; trying next version")
                continue
            version = ver

        # --- Normalize/merge input into .apk when needed ---
        if input_apk.suffix != ".apk":
            # Check if it is a split bundle (contains multiple .apk files or is .apkm/.xapk/.apks)
            is_bundle = False
            try:
                import zipfile
                if zipfile.is_zipfile(input_apk):
                    with zipfile.ZipFile(input_apk, "r") as z:
                        namelist = z.namelist()
                        has_split_apks = any(n.endswith(".apk") for n in namelist)
                        is_bundle = has_split_apks or input_apk.suffix.lower() in [".apkm", ".xapk", ".apks", ".zip"]
            except Exception as e:
                logging.debug(f"Zip inspection failed for {input_apk}: {e}")

            target_apk = input_apk.with_name(f"{input_apk.stem}.apk" if not input_apk.name.endswith(".apk") else input_apk.name)

            if is_bundle:
                logging.info(f"Input file is a bundle ({input_apk.name}), using APKEditor to merge")
                apk_editor = downloader.download_apkeditor()
                merged_apk = input_apk.with_suffix(".apk")
                merged_apk.unlink(missing_ok=True)

                try:
                    utils.run_process([
                        "java", "-jar", str(apk_editor), "m",
                        "-f",
                        "-i", str(input_apk),
                        "-o", str(merged_apk)
                    ], silent=True, check=True)
                    input_apk.unlink(missing_ok=True)
                    input_apk = merged_apk
                except Exception as e:
                    logging.warning(f"APKEditor merge failed ({e}); checking if file can be used as standalone APK")
                    if input_apk.exists():
                        target_apk.unlink(missing_ok=True)
                        os.replace(input_apk, target_apk)
                        input_apk = target_apk
            else:
                logging.info(f"Normalizing standalone APK filename to {target_apk.name}")
                if input_apk != target_apk:
                    target_apk.unlink(missing_ok=True)
                    os.replace(input_apk, target_apk)
                    input_apk = target_apk

            if not input_apk.exists():
                logging.error("Processed APK file not found")
                raise RuntimeError("Processed APK file not found")

            # Clean up filename: remove build number like (1575420) and -1575420.
            # Only strip 6+ digit build-number tokens so legitimate short version
            # segments (e.g. "app-2_0") are not mangled.
            clean_name = re.sub(r'\(\d+\)', '', input_apk.name)  # Remove (1575420)
            clean_name = re.sub(r'-\d{6,}_', '_', clean_name)  # Remove -1575420_ -> _
            if clean_name != input_apk.name:
                clean_apk = input_apk.with_name(clean_name)
                clean_apk.unlink(missing_ok=True)
                os.replace(input_apk, clean_apk)
                input_apk = clean_apk

            logging.info(f"Normalized APK file: {input_apk}")

        # --- ARCHITECTURE-SPECIFIC PROCESSING ---
        if arch != "universal":
            logging.info(f"Processing APK for {arch} architecture...")
            if arch == "arm64-v8a":
                utils.strip_zip_entries(input_apk, ["lib/x86/*", "lib/x86_64/*", "lib/armeabi-v7a/*"])
            elif arch == "armeabi-v7a":
                utils.strip_zip_entries(input_apk, ["lib/x86/*", "lib/x86_64/*", "lib/arm64-v8a/*"])
        else:
            utils.strip_zip_entries(input_apk, ["lib/x86/*", "lib/x86_64/*"])

        # Validate APK integrity (safety net: downloads were already validated,
        # but bundle merging / arch stripping can corrupt the file).
        # Note: Only check integrity here, NOT signature. Merged bundles are
        # unsigned (APKEditor doesn't sign); the APK gets signed after patching.
        logging.info("Checking APK integrity...")
        if not utils.check_apk_integrity(input_apk):
            logging.error(f"APK for {app_name} v{version} is corrupt and could not be repaired; trying next version")
            continue

        # Include architecture in output filename
        output_apk = Path(f"{app_name}-{arch}-patch-v{version}.apk")

        cli_output = None
        try:
            # USE DIFFERENT COMMANDS BASED ON SOURCE TYPE
            if is_morphe:
                logging.info("🔧 Using Morphe patching system...")
                morphe_cmd = [
                    "java", "-jar", str(cli),
                    "patch", "--patches", str(patches),
                    "--out", str(output_apk), str(input_apk),
                    *exclude_patches, *include_patches
                ]
                cli_output = utils.run_process(morphe_cmd, capture=True, stream=True)
            else:
                logging.info("🔧 Using ReVanced patching system...")
                cli_name = Path(cli).name.lower()
                is_revanced_v6_or_newer = (
                    'revanced-cli-6' in cli_name or 'revanced-cli-7' in cli_name or 'revanced-cli-8' in cli_name
                )

                if is_revanced_v6_or_newer:
                    cli_output = utils.run_process([
                        "java", "-jar", str(cli),
                        "patch", "-p", str(patches), "-b",
                        "--out", str(output_apk), str(input_apk),
                        *exclude_patches, *include_patches
                    ], capture=True, stream=True)
                else:
                    cli_output = utils.run_process([
                        "java", "-jar", str(cli),
                        "patch", "--patches", str(patches),
                        "--out", str(output_apk), str(input_apk),
                        *exclude_patches, *include_patches
                    ], capture=True, stream=True)

        except subprocess.CalledProcessError as e:
            # Remove temp input apk; we'll re-download if retrying.
            input_apk.unlink(missing_ok=True)
            output_apk.unlink(missing_ok=True)

            if attempt_idx < len(versions_to_try) - 1 and _should_retry_with_older_version(getattr(e, "output", None)):
                continue
            raise

        rejection = _unpatched_build_reason(cli_output, version)
        if rejection:
            logging.error(f"❌ {rejection}")
            input_apk.unlink(missing_ok=True)
            output_apk.unlink(missing_ok=True)
            if attempt_idx < len(versions_to_try) - 1:
                continue
            return None

        # Patch succeeded -> cleanup input and sign.
        input_apk.unlink(missing_ok=True)

        signed_apk = Path(f"{app_name}-{arch}-{name}-v{version}.apk")

        apksigner = utils.find_apksigner()
        if not apksigner:
            raise RuntimeError("apksigner not found")

        try:
            utils.run_process([
                str(apksigner), "sign", "--verbose",
                "--ks", "keystore/public.jks",
                "--ks-pass", "pass:public",
                "--key-pass", "pass:public",
                "--ks-key-alias", "public",
                "--in", str(output_apk), "--out", str(signed_apk)
            ], capture=True, stream=True)
        except Exception as e:
            logging.warning(f"Standard signing failed: {e}")
            logging.info("Trying alternative signing method...")

            utils.run_process([
                str(apksigner), "sign", "--verbose",
                "--min-sdk-version", "21",
                "--ks", "keystore/public.jks",
                "--ks-pass", "pass:public",
                "--key-pass", "pass:public",
                "--ks-key-alias", "public",
                "--in", str(output_apk), "--out", str(signed_apk)
            ], capture=True, stream=True)

        output_apk.unlink(missing_ok=True)
        print(f"✅ APK built: {signed_apk.name}")
        return str(signed_apk)

    # If we got here, every candidate version failed.
    return None

def main():
    app_name = getenv("APP_NAME")
    source = getenv("SOURCE")

    if not app_name or not source:
        logging.error("APP_NAME and SOURCE environment variables must be set")
        exit(1)

    # Read arch-config.json
    arch_config_path = Path("arch-config.json")
    if arch_config_path.exists():
        with open(arch_config_path) as f:
            arch_config = json.load(f)
        
        # Find arches for this app
        arches = [(getenv("ARCH") or "universal").strip()]
        for config in arch_config:
            if not getenv("ARCH") and config["app_name"] == app_name and config["source"] == source:
                arches = config["arches"]
                break
        
        # Build for each architecture
        built_apks = []
        for arch in arches:
            logging.info(f"🔨 Building {app_name} for {arch} architecture...")
            apk_path = run_build(app_name, source, arch)
            if apk_path:
                built_apks.append(apk_path)
                print(f"✅ Built {arch} version: {Path(apk_path).name}")
        
        # Summary
        print(f"\n🎯 Built {len(built_apks)} APK(s) for {app_name}:")
        for apk in built_apks:
            print(f"  📱 {Path(apk).name}")
        if not built_apks:
            logging.error(f"❌ No APKs built for {app_name}; failing the job.")
            exit(1)
        
    else:
        # Fallback to single universal build
        logging.warning("arch-config.json not found, building universal only")
        apk_path = run_build(app_name, source, "universal")
        if apk_path:
            print(f"🎯 Final APK path: {apk_path}")
        else:
            logging.error(f"❌ No APK built for {app_name}; failing the job.")
            exit(1)

if __name__ == "__main__":
    main()
