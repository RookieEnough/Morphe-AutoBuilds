"""Offline tests for the global ABI preference chain and patch-support detection.

Run with: python tests/test_arch_fallback.py
Requires the packages from requirements.txt (imports the real src package).
"""

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import __main__ as pipeline  # noqa: E402
from src import downloader, utils  # noqa: E402


def test_fallback_order_is_universal_then_arm64_then_armv7():
    assert downloader.ARCH_PRIORITY == ("universal", "arm64-v8a", "armeabi-v7a")
    assert downloader.fallback_arches("universal") == ["arm64-v8a", "armeabi-v7a"]
    assert downloader.fallback_arches("arm64-v8a") == ["armeabi-v7a"]
    assert downloader.fallback_arches("armeabi-v7a") == []
    # An explicitly requested target outside the chain stays explicit.
    assert downloader.fallback_arches("x86") == []


def test_arch_aliases_are_canonicalised():
    assert downloader.normalize_arch(None) == "universal"
    assert downloader.normalize_arch("") == "universal"
    assert downloader.normalize_arch("arm-v7a") == "armeabi-v7a"
    assert downloader.normalize_arch("arm64") == "arm64-v8a"
    assert downloader.normalize_arch("ARM64-V8A") == "arm64-v8a"


def _run_chain(arches, succeed_on):
    """Run the build pipeline with run_build faked; returns (attempts, built)."""
    attempts = []

    def fake_run_build(app_name, source, arch):
        attempts.append(arch)
        if arch in succeed_on:
            return Path(f"{app_name}-{arch}.apk")
        return None

    with mock.patch.object(pipeline, "run_build", fake_run_build):
        built = pipeline._build_arches_with_fallback("app", "source", arches)
    return attempts, [Path(p).name for p in built]


def test_universal_falls_back_to_arm64_and_stops():
    attempts, built = _run_chain(["universal"], succeed_on={"arm64-v8a"})
    assert attempts == ["universal", "arm64-v8a"]
    assert built == ["app-arm64-v8a.apk"], "a successful arm64 fallback must not also build v7"


def test_universal_falls_back_to_v7_only_when_arm64_also_fails():
    attempts, built = _run_chain(["universal"], succeed_on={"armeabi-v7a"})
    assert attempts == ["universal", "arm64-v8a", "armeabi-v7a"]
    assert built == ["app-armeabi-v7a.apk"]


def test_no_apk_at_all_when_every_target_fails():
    attempts, built = _run_chain(["universal"], succeed_on=set())
    assert attempts == ["universal", "arm64-v8a", "armeabi-v7a"]
    assert built == []


def test_explicit_multi_arch_configuration_still_builds_every_target():
    attempts, built = _run_chain(
        ["arm64-v8a", "armeabi-v7a"], succeed_on={"arm64-v8a", "armeabi-v7a"}
    )
    assert attempts == ["arm64-v8a", "armeabi-v7a"]
    assert built == ["app-arm64-v8a.apk", "app-armeabi-v7a.apk"]


def test_explicit_arm64_falls_back_to_v7_once():
    attempts, built = _run_chain(["arm64-v8a"], succeed_on={"armeabi-v7a"})
    assert attempts == ["arm64-v8a", "armeabi-v7a"], "the fallback must not duplicate the request"
    assert built == ["app-armeabi-v7a.apk"]


def test_unknown_target_has_no_fallback():
    attempts, built = _run_chain(["x86"], succeed_on=set())
    assert attempts == ["x86"]
    assert built == []


def test_cli_reporting_no_compatible_versions_means_unsupported():
    output = (
        "WARN: Morphe data root falling back\n"
        "WARN: No compatible versions found in: lab-build.mpp"
    )
    with mock.patch.object(utils, "run_process", lambda *a, **k: output):
        assert utils.get_supported_versions("all.in.one.calculator", "morphe-cli.jar", "lab-build.mpp") is None


def test_cli_without_version_lines_still_means_version_agnostic():
    output = "WARN: header line\nINFO: another line"
    with mock.patch.object(utils, "run_process", lambda *a, **k: output):
        assert utils.get_supported_versions("com.example.app", "morphe-cli.jar", "patches.mpp") == []


def test_cli_versions_are_parsed_normally():
    output = (
        "INFO: Package name: com.example.app\n"
        "Most common compatible versions:\n"
        "\t1.2.3 (2 patches)\n"
        "\t1.2.2 (1 patch)"
    )
    with mock.patch.object(utils, "run_process", lambda *a, **k: output):
        versions = utils.get_supported_versions("com.example.app", "morphe-cli.jar", "patches.mpp")
    assert versions == ["1.2.3", "1.2.2"]


def test_patch_failure_for_one_abi_falls_back_to_the_next():
    attempts = []

    def fake_run_build(app_name, source, arch):
        attempts.append(arch)
        if arch == "universal":
            raise RuntimeError("patch exploded")
        return Path(f"{app_name}-{arch}.apk")

    with mock.patch.object(pipeline, "run_build", fake_run_build):
        built = pipeline._build_arches_with_fallback("app", "source", ["universal"])

    assert attempts == ["universal", "arm64-v8a"]
    assert [Path(p).name for p in built] == ["app-arm64-v8a.apk"]


def test_unsupported_patch_bundle_is_not_retried_for_other_abis():
    attempts = []

    def fake_run_build(app_name, source, arch):
        attempts.append(arch)
        raise downloader.UnknownPatchCompatibilityError("no supported versions")

    with mock.patch.object(pipeline, "run_build", fake_run_build):
        try:
            pipeline._build_arches_with_fallback("app", "source", ["universal"])
        except downloader.UnknownPatchCompatibilityError:
            pass
        else:
            raise AssertionError("an unsupported patch bundle must fail immediately")

    assert attempts == ["universal"], "another ABI cannot fix missing patch support"


def test_failure_is_reraised_when_every_abi_fails():
    with mock.patch.object(pipeline, "run_build", side_effect=RuntimeError("boom")):
        try:
            pipeline._build_arches_with_fallback("app", "source", ["universal"])
        except RuntimeError as exc:
            assert "boom" in str(exc)
        else:
            raise AssertionError("expected the underlying build error to surface")


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
