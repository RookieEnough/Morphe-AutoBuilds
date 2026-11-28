"""Offline tests for build-output detection in scripts/record_build.py.

Run with: python tests/test_record_build.py
"""

import importlib.util
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
helpers = runpy.run_path(str(ROOT / "scripts" / "record_build.py"))
is_app_build_output = helpers["is_app_build_output"]
extract_version = helpers["extract_version_from_filename"]


def _load_check_app_updates():
    spec = importlib.util.spec_from_file_location(
        "check_app_updates", ROOT / "scripts" / "check_app_updates.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_build_outputs_are_recognised():
    assert is_app_build_output("facebook-universal-morphe-patches-v588.0.0.42.55.apk", "facebook")
    assert is_app_build_output("discord-armeabi-v7a-morphe-patches-v351.11.apk", "discord")
    assert is_app_build_output(
        "instagram-arm64-v8a-piko-v430.0.0.53.80.apk", "instagram"
    )
    assert is_app_build_output("google-news-universal-morphe-patches-v5.163.0.947799485.apk", "google-news")


def test_tool_and_mirror_downloads_are_rejected():
    # APK shipped inside a patch-source bundle (this was published as Facebook's build).
    assert not is_app_build_output("hush-unpacker-0.25.0.apk", "facebook")
    # APK downloaded from a mirror during the build: right prefix, no -v version marker.
    assert not is_app_build_output("google-news-5-172-0-1024046478.apk", "google-news")
    # Another app's output must never be attributed to this app.
    assert not is_app_build_output("discord-armeabi-v7a-morphe-patches-v351.11.apk", "facebook")


def test_empty_inputs_are_rejected():
    assert not is_app_build_output("", "facebook")
    assert not is_app_build_output("facebook-universal-morphe-patches-v1.2.3.apk", "")


def test_version_extraction_still_works_for_scoped_names():
    assert extract_version("discord-armeabi-v7a-morphe-patches-v351.11.apk") == "351.11"
    assert extract_version("hush-unpacker-0.25.0.apk") == ""


def test_planner_rejects_foreign_manifest_apks():
    planner = _load_check_app_updates()
    belongs = planner._apk_belongs_to_app
    assert belongs("discord-armeabi-v7a-morphe-patches-v351.11.apk", "discord")
    assert not belongs("hush-unpacker-0.25.0.apk", "facebook")
    assert not belongs("google-news-5-172-0-1024046478.apk", "google-news")
    assert not belongs("discord-armeabi-v7a-morphe-patches-v351.11.apk", "facebook")


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
