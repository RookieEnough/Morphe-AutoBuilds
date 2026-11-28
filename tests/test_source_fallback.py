"""Offline test: one mirror's unsupported package must not abort the build.

The four 2026-11-28 CI failures happened because the APKMirror app config named
a package the patches do not target, and download_platform() raised
UnknownPatchCompatibilityError on that first source, so the correct mirrors
(APKPure/Aptoide with the real package) were never tried.

Run with: python tests/test_source_fallback.py
"""

import shutil
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import __main__ as pipeline  # noqa: E402
from src import downloader  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".runbuild-test"

METHOD_NAMES = [
    "download_apkmirror",
    "download_apkpure",
    "download_aptoide",
    "download_uptodown",
    "download_apkfab",
    "download_androidapksbox",
    "download_github",
    "download_codeberg",
    "download_apkcombo",
]


def _fake_tools():
    WORK.mkdir(exist_ok=True)
    cli = WORK / "morphe-cli.jar"
    patches = WORK / "patches.mpp"
    cli.write_bytes(b"fake jar")
    patches.write_bytes(b"fake patches")
    return [cli, patches]


def _run(methods):
    tools = _fake_tools()
    try:
        with mock.patch.object(downloader, "download_required", lambda source: (tools, "src")), \
             mock.patch.multiple(downloader, **methods):
            return pipeline.run_build("app", "src", "universal")
    finally:
        shutil.rmtree(WORK, ignore_errors=True)


def test_all_sources_unsupported_raises_clear_error():
    calls = []

    def unsupported(*args, **kwargs):
        calls.append(1)
        raise downloader.UnknownPatchCompatibilityError("no supported versions")

    methods = {name: unsupported for name in METHOD_NAMES}
    try:
        _run(methods)
    except downloader.UnknownPatchCompatibilityError:
        pass
    else:
        raise AssertionError("expected UnknownPatchCompatibilityError")
    assert len(calls) == len(METHOD_NAMES), "every source should get a chance"


def test_one_unsupported_source_does_not_abort_the_others():
    calls = []

    def unsupported(*args, **kwargs):
        calls.append("apkmirror")
        raise downloader.UnknownPatchCompatibilityError("no supported versions")

    def no_download(*args, **kwargs):
        calls.append("other")
        return None, None, []

    methods = {name: no_download for name in METHOD_NAMES}
    methods[METHOD_NAMES[0]] = unsupported

    result = _run(methods)
    assert result is None, "no APK could be downloaded, but it must not raise the compat error"
    assert len(calls) == len(METHOD_NAMES), "the pipeline must try every source"


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
