"""Offline regression for APKMirror variant selection (Facebook 588 case).

CI renders the APKMirror release page through FlareSolverr, but the variant
rows for split bundles often omit the configured dpi range (they say "nodpi"),
which used to make the selector report "No variant found" and fall through to a
lower-quality mirror. These tests pin the graded matching behaviour.

Run with: python tests/test_apkmirror_variants.py
"""

import re
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import apkmirror  # noqa: E402

RELEASE = "https://www.apkmirror.com/apk/facebook-2/facebook/facebook-588-0-0-42-55-release/"
ARM64_PAGE = (
    "https://www.apkmirror.com/apk/facebook-2/facebook/facebook-588-0-0-42-55-arm64-download/"
)
V7_PAGE = (
    "https://www.apkmirror.com/apk/facebook-2/facebook/facebook-588-0-0-42-55-v7-download/"
)
ARM64_FILE = "https://www.apkmirror.com/final/facebook-arm64.apk"
V7_FILE = "https://www.apkmirror.com/final/facebook-v7.apk"

CONFIG = {
    "org": "facebook-2",
    "name": "facebook",
    "package": "com.facebook.katana",
    "type": "BUNDLE",
    "arch": "arm64-v8a",
    "dpi": "120-480dpi",
}


class FakeResponse:
    def __init__(self, status_code=200, html="", url=""):
        self.status_code = status_code
        self.content = html.encode("utf-8")
        self.text = html
        self.url = url
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _row(href, *cells):
    cells_html = "".join(f'<div class="table-cell">{cell}</div>' for cell in cells)
    return (
        f'<div class="table-row">{cells_html}'
        f'<a class="accent_color" href="{href}">Download</a></div>'
    )


def _release_html(*rows):
    header = _row("#", "Variant", "Arch", "Version")
    return (
        '<html><body><h1>Facebook 588.0.0.42.55</h1>'
        f'<div class="variants-table">{header}{"".join(rows)}</div>'
        "</body></html>"
    )


def _fake_cf_get(pages):
    def fake_get(url, **kwargs):
        if url in pages:
            return FakeResponse(html=pages[url], url=url)
        if url.endswith("-download/"):
            lowered = url.lower()
            code = re.search(r"code(\d+)", url)
            if code:
                tag = "code" + code.group(1)
            elif "a11" in lowered:
                tag = "a11"
            elif "a9" in lowered:
                tag = "a9"
            elif "arm64" in lowered:
                tag = "arm64"
            elif "v7" in lowered:
                tag = "v7"
            else:
                tag = "other"
            return FakeResponse(
                html=f'<html><body><a class="downloadButton" href="/go/{tag}">go</a></body></html>',
                url=url,
            )
        if "/go/" in url:
            tag = url.rstrip("/").rsplit("/", 1)[-1]
            return FakeResponse(
                html=(
                    '<html><body><a id="download-link" '
                    f'href="/final/facebook-{tag}.apk">get</a></body></html>'
                ),
                url=url,
            )
        return FakeResponse(status_code=404, url=url)

    return fake_get


def _resolve(config, release_html):
    with mock.patch.object(apkmirror, "get_build_number_for_version", lambda *a, **k: (None, None)), \
         mock.patch.object(apkmirror, "find_release_page_from_main", lambda *a, **k: RELEASE), \
         mock.patch.object(apkmirror, "_cf_get", _fake_cf_get({RELEASE: release_html})):
        return apkmirror.get_download_link("588.0.0.42.55", "facebook", dict(config))


def test_nodpi_bundle_rows_are_selected_on_type_and_arch():
    html = _release_html(
        _row("/apk/facebook-2/facebook/fb-588-arm64-download/", "Bundle", "arm64-v8a + armeabi-v7a", "nodpi", "Android 11+"),
        _row("/apk/facebook-2/facebook/fb-588-v7-download/", "Bundle", "armeabi-v7a", "nodpi", "Android 9+"),
    )
    link = _resolve(CONFIG, html)
    assert link and "arm64" in link, f"expected the arm64 variant, got {link}"


def test_relaxation_never_picks_a_lower_abi():
    # Only a 32-bit row exists; an arm64 build request must fail instead of
    # silently shipping the v7 variant.
    html = _release_html(
        _row("/apk/facebook-2/facebook/fb-588-v7-download/", "Bundle", "armeabi-v7a", "nodpi", "Android 9+"),
    )
    assert _resolve(CONFIG, html) is None


def test_type_mismatch_is_relaxed_as_a_last_resort():
    html = _release_html(
        _row("/apk/facebook-2/facebook/fb-588-arm64-download/", "APK", "arm64-v8a", "nodpi", "Android 11+"),
    )
    link = _resolve(CONFIG, html)
    assert link and "arm64" in link


def test_strict_dpi_match_is_still_preferred():
    html = _release_html(
        _row("/apk/facebook-2/facebook/fb-588-v7-download/", "Bundle", "armeabi-v7a", "120-480dpi", "Android 9+"),
        _row("/apk/facebook-2/facebook/fb-588-arm64-download/", "Bundle", "arm64-v8a", "120-480dpi", "Android 11+"),
    )
    link = _resolve(CONFIG, html)
    assert link and "arm64" in link


def test_newest_android_baseline_wins_for_the_same_abi():
    # Facebook ships one arm64 bundle per minimum Android version; the newer
    # packaging (which HushFacebook supports) must win even if listed second.
    html = _release_html(
        _row("/apk/facebook-2/facebook/fb-588-arm64-a9-download/", "Bundle", "arm64-v8a", "nodpi", "Android 9+"),
        _row("/apk/facebook-2/facebook/fb-588-arm64-a11-download/", "Bundle", "arm64-v8a", "nodpi", "Android 11+"),
    )
    link = _resolve(CONFIG, html)
    assert link and "facebook-a11.apk" in link, f"expected the Android 11+ bundle, got {link}"


def test_patch_declared_version_code_wins_over_row_order():
    # APKMirror lists several uploads for one version string; the patches were
    # built against 707615136, so that row must win over the first-listed one.
    from src import utils

    utils.remember_version_codes(
        "Most common compatible versions:\n\t588.0.0.42.55 "
        "[versionCodes: ARM64_V8A=707615136, ARMEABI_V7A=707615135] (123 patches)"
    )
    try:
        html = _release_html(
            _row(
                "/apk/facebook-2/facebook/fb-588-code707615019-download/",
                "Bundle", "arm64-v8a", "480dpi", "Android 11+", "707615019",
            ),
            _row(
                "/apk/facebook-2/facebook/fb-588-code707615136-download/",
                "Bundle", "arm64-v8a", "nodpi", "Android 11+", "707615136",
            ),
        )
        link = _resolve(CONFIG, html)
        assert link and "code707615136" in link, f"expected the patch build, got {link}"
    finally:
        utils.remember_version_codes("")


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
