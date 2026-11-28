#!/usr/bin/env python3
"""Merge per-build records into the final manifest.json before uploading.

Inputs:
  - new_manifest.json      (planning-time manifest, has all entries with old apk
                            filenames as fallback for carry-overs)
  - build_records/*.json   (one record per built APK, written by record_build.py)

Output:
  - manifest.json          (final manifest to attach to the release)
"""
import json
import sys
from pathlib import Path


def main() -> int:
    new_manifest_path = Path("new_manifest.json")
    if not new_manifest_path.exists():
        print("No new_manifest.json found; nothing to merge")
        return 0

    with new_manifest_path.open("r", encoding="utf-8") as f:
        manifest = json.load(f)

    entries = manifest.setdefault("entries", {})

    rec_dir = Path("build_records")
    if rec_dir.exists():
        for rec_file in sorted(rec_dir.rglob("*.json")):
            try:
                with rec_file.open("r", encoding="utf-8") as f:
                    rec = json.load(f)
            except Exception as e:
                print(f"  skip bad record {rec_file}: {e}")
                continue
            key = rec.get("key")
            apk = rec.get("apk", "")
            # resolved_version is the version actually embedded in the built APK
            # filename (extracted by record_build.py). Propagate it as
            # 'built_version' so check_app_updates.py can detect on the next run
            # when a newer app version becomes available, even for apps whose
            # config 'version' is empty (meaning "latest at build time").
            resolved_version = (rec.get("resolved_version") or "").strip()
            if not key:
                continue
            entry = entries.get(key)
            if not entry:
                # Record exists but planning didn't list this combo; create it.
                entry = {
                    "app_name": rec.get("app_name", ""),
                    "source": rec.get("source", ""),
                    "arch": rec.get("arch", "universal"),
                    "config_version": "",
                    "source_sig": "",
                    "apk": "",
                    "built_version": "",
                }
                entries[key] = entry
            if apk:
                entry["apk"] = apk
            if resolved_version:
                entry["built_version"] = resolved_version
            if "version_agnostic" in rec:
                entry["version_agnostic"] = bool(rec.get("version_agnostic"))
            if rec.get("version_mode"):
                entry["version_mode"] = rec["version_mode"]
            # Promote pending_source_sig -> source_sig now that the build
            # succeeded.  The planner deliberately keeps the OLD source_sig
            # for rebuild entries so that a failed build doesn't "consume"
            # the signature change.  Only a successful build (= this code
            # path) finalises the new signature.
            pending_sig = entry.get("pending_source_sig", "")
            if pending_sig:
                entry["source_sig"] = pending_sig
                del entry["pending_source_sig"]
            print(f"  merged {key} -> apk={apk!r} built_version={resolved_version!r}")
    # Clean up leftover pending_source_sig for entries whose build never
    # completed (no build record).  The OLD source_sig stays in place so the
    # next planner run will detect the difference and retry.
    for entry in entries.values():
        entry.pop("pending_source_sig", None)

    with open("manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote manifest.json with {len(entries)} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
