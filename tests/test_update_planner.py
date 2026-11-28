"""Offline tests for the incremental update planner's rebuild triggers.

The planner must rebuild when:
1. the patch source publishes anything new (source signature changes);
2. the patches declare support for a newer app version (patches-list target);
3. the patches are version-agnostic and the app itself got a newer release
   (recorded on the manifest entry as ``version_agnostic``).

And it must NOT rebuild version-pinned apps merely because the store moved on.

Run with: python tests/test_update_planner.py
"""

import importlib.util
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def _load_planner():
    spec = importlib.util.spec_from_file_location(
        "check_app_updates_for_tests", ROOT / "scripts" / "check_app_updates.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


planner = _load_planner()


def _entry(apk="app-universal-src-v1.0.apk", built="1.0", sig="sig-1",
           agnostic=False, config_version=""):
    return {
        "app_name": "app",
        "source": "src",
        "arch": "universal",
        "config_version": config_version,
        "source_sig": sig,
        "apk": apk,
        "built_version": built,
        "version_agnostic": agnostic,
    }


def _plan(entry, store_latest="2.0", recommended="", current_sig="sig-1"):
    matrix = [{"app_name": "app", "source": "src", "arch": "universal"}]
    manifest = {"entries": {"app|src|universal": entry}}
    with mock.patch.object(planner, "load_app_config_version", lambda app: entry["config_version"]), \
         mock.patch.object(planner, "get_source_signature", lambda src: current_sig), \
         mock.patch.object(planner, "fetch_recommended_version", lambda app, src: recommended), \
         mock.patch.object(planner, "fetch_latest_app_version", lambda app: store_latest):
        return planner.plan_incremental(matrix, manifest, [entry["apk"]])


def test_version_agnostic_app_rebuilds_when_store_releases_newer_version():
    build, carry, entries = _plan(_entry(agnostic=True), store_latest="2.0")
    assert build == [{"app_name": "app", "source": "src"}]
    assert carry == []
    assert entries["app|src|universal"]["apk"] == "app-universal-src-v1.0.apk"


def test_version_agnostic_app_carries_when_store_version_unchanged():
    build, carry, _ = _plan(_entry(agnostic=True), store_latest="1.0")
    assert build == []
    assert carry == ["app-universal-src-v1.0.apk"]


def test_pinned_patch_app_does_not_rebuild_on_store_bump():
    build, carry, _ = _plan(_entry(agnostic=False), store_latest="9.9")
    assert build == [], "version-specific patches must not rebuild just because the store moved"
    assert carry == ["app-universal-src-v1.0.apk"]


def test_pinned_config_version_does_not_rebuild_on_store_bump():
    entry = _entry(agnostic=True, config_version="1.0")
    build, carry, _ = _plan(entry, store_latest="9.9")
    assert build == [], "an explicitly pinned config version must win"
    assert carry == ["app-universal-src-v1.0.apk"]


def test_patches_declaring_a_newer_version_trigger_rebuild():
    build, _, _ = _plan(_entry(agnostic=False), store_latest="1.0", recommended="2.0")
    assert build == [{"app_name": "app", "source": "src"}]


def test_patch_source_change_triggers_rebuild():
    build, _, _ = _plan(_entry(agnostic=False), store_latest="1.0", current_sig="sig-2")
    assert build == [{"app_name": "app", "source": "src"}]


def test_new_app_entry_triggers_rebuild():
    build, _, _ = _plan(_entry(apk="", built=""), store_latest="1.0", current_sig="sig-1")
    assert build == [{"app_name": "app", "source": "src"}]


def main():
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    main()
