"""Offline regression tests for the Uptodown mobile-API download path.

The public Uptodown website download flow is Cloudflare Turnstile protected
as of mid/late 2026, so the pipeline resolves downloads through the anonymous
mobile API instead (see ``src/uptodown_api.py``).  Both the API client and the
wiring in ``src/uptodown.py`` are covered here with the network replaced by
fakes.

Run with: python tests/test_uptodown_api.py
Requires the packages from requirements.txt (bs4 is used for the wiring test).
"""

import base64
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _install_optional_dependency_stubs():
    """Let these offline tests run where only bs4 and requests are installed.

    ``src/__init__`` imports curl_cffi (transport) and PyGithub (auth); both are
    unrelated to the code under test, and the scraper's HTTP is patched below.
    CI installs the real packages, in which case these stubs are not used.
    """
    import types

    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        curl_cffi = types.ModuleType("curl_cffi")
        requests_mod = types.ModuleType("curl_cffi.requests")

        class _Session:
            def __init__(self, *args, **kwargs):
                pass

            def get(self, *args, **kwargs):
                raise RuntimeError("curl_cffi is stubbed in this test run")

        requests_mod.Session = _Session
        impersonate = types.ModuleType("curl_cffi.requests.impersonate")
        impersonate.DEFAULT_CHROME = "chrome"
        requests_mod.impersonate = impersonate
        curl_cffi.requests = requests_mod
        sys.modules.setdefault("curl_cffi", curl_cffi)
        sys.modules.setdefault("curl_cffi.requests", requests_mod)
        sys.modules.setdefault("curl_cffi.requests.impersonate", impersonate)

    try:
        import github  # noqa: F401
    except ImportError:
        github = types.ModuleType("github")
        exceptions = types.ModuleType("github.GithubException")

        class BadCredentialsException(Exception):
            pass

        class Github:
            def __init__(self, *args, **kwargs):
                pass

        exceptions.BadCredentialsException = BadCredentialsException
        github.Github = Github
        github.GithubException = exceptions
        sys.modules.setdefault("github", github)
        sys.modules.setdefault("github.GithubException", exceptions)


_install_optional_dependency_stubs()

from src import uptodown, uptodown_api  # noqa: E402


class FakeResponse:
    """Response-shaped object for both the API client and the scraper."""

    def __init__(self, status_code=200, body=None, html=None, url="https://example.test/"):
        self.status_code = status_code
        self.url = url
        if html is not None:
            self.content = html.encode("utf-8")
            self.text = html
        else:
            self.text = json.dumps(body if body is not None else {})
            self.content = self.text.encode("utf-8")
        self.headers = {}

    def json(self):
        return json.loads(self.text)


def _jwt(exp):
    def encode(payload):
        raw = json.dumps(payload).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'HS256', 'typ': 'JWT'})}.{encode({'exp': exp})}.signature"


def test_auth_request_is_signed_and_identifies_the_android_client():
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return FakeResponse(body={"token": _jwt(int(time.time()) + 3600)})

    with mock.patch.object(uptodown_api, "_request", fake_request):
        uptodown_api.reset_session()
        token = uptodown_api.get_token()

    assert token and token.count(".") == 2, "expected a JWT bearer token"
    assert len(calls) == 1
    method, url, kwargs = calls[0]
    assert method == "POST"
    assert url.startswith(f"{uptodown_api._api_host()}/eapi/auth/token?identifier=")
    assert kwargs["headers"]["Identificador"] == "Uptodown_Android"
    assert kwargs["headers"]["Content-Type"] == "application/x-www-form-urlencoded"

    form = kwargs["data"]
    assert set(form) == {"identifier", "id_plataforma", "lang", "unixtime", "hmac"}
    assert form["id_plataforma"] == "13"
    assert form["lang"] == "en"
    assert len(form["identifier"]) == 16
    assert url.endswith("identifier=" + form["identifier"]), "identifier must match the query string"
    expected = hmac.new(
        uptodown_api._hmac_key().encode("utf-8"),
        form["unixtime"].encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert form["hmac"] == expected, "HMAC must sign the unix timestamp with the client key"


def test_token_is_cached_until_expiry_then_refreshed():
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append(url)
        return FakeResponse(body={"token": _jwt(int(time.time()) + 3600)})

    with mock.patch.object(uptodown_api, "_request", fake_request):
        uptodown_api.reset_session()
        first = uptodown_api.get_token()
        second = uptodown_api.get_token()
        assert first == second
        assert len(calls) == 1, "a valid cached token must not re-authenticate"

        uptodown_api._token_expiry = time.time() - 1
        third = uptodown_api.get_token()
        assert len(calls) == 2, "an expired token must be refreshed"
        assert third is not None


def test_jwt_expiry_parsing_rejects_unusable_tokens():
    assert uptodown_api._jwt_expiry(_jwt(1893456000)) == 1893456000
    assert uptodown_api._jwt_expiry("not-a-jwt") is None
    bad_payload = "header." + base64.urlsafe_b64encode(b"not json").decode().rstrip("=") + ".sig"
    assert uptodown_api._jwt_expiry(bad_payload) is None
    no_exp = _jwt(0).split(".")
    no_exp[1] = base64.urlsafe_b64encode(b'{"sub": 1}').decode().rstrip("=")
    assert uptodown_api._jwt_expiry(".".join(no_exp)) is None


def test_resolve_download_url_returns_signed_url():
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return FakeResponse(
            body={"success": 1, "data": {"downloadURL": "https://dw.uptodown.com/dwn/token123"}}
        )

    with mock.patch.object(uptodown_api, "get_token", lambda force_refresh=False: "tok"):
        with mock.patch.object(uptodown_api, "_request", fake_request):
            link = uptodown_api.resolve_download_url(16906, 1241932605)

    assert link == "https://dw.uptodown.com/dwn/token123"
    method, url, kwargs = calls[0]
    assert method == "GET"
    assert url == f"{uptodown_api._api_host()}/eapi/apps/16906/file/1241932605/downloadUrl"
    assert kwargs["headers"]["Authorization"] == "Bearer tok"
    assert kwargs["headers"]["Identificador"] == "Uptodown_Android"


def test_expired_bearer_token_is_refreshed_once():
    responses = [
        FakeResponse(status_code=401, body={}),
        FakeResponse(body={"success": 1, "data": {"downloadURL": "https://dw.uptodown.com/dwn/x"}}),
    ]
    tokens = []

    def fake_token(force_refresh=False):
        tokens.append(force_refresh)
        return "tok"

    with mock.patch.object(uptodown_api, "get_token", fake_token):
        with mock.patch.object(uptodown_api, "_request", lambda *a, **k: responses.pop(0)):
            link = uptodown_api.resolve_download_url("16906", "555")

    assert link == "https://dw.uptodown.com/dwn/x"
    assert tokens == [False, True], "the retry must force a fresh session"


def test_resolve_returns_none_on_api_failure():
    with mock.patch.object(uptodown_api, "get_token", lambda force_refresh=False: "tok"):
        with mock.patch.object(
            uptodown_api, "_request", lambda *a, **k: FakeResponse(status_code=500, body={})
        ):
            assert uptodown_api.resolve_download_url("16906", "555") is None

        with mock.patch.object(
            uptodown_api, "_request", lambda *a, **k: FakeResponse(body={"success": 0})
        ):
            assert uptodown_api.resolve_download_url("16906", "555") is None

        with mock.patch.object(
            uptodown_api,
            "_request",
            lambda *a, **k: FakeResponse(body={"success": 1, "data": {"downloadURL": "ftp://nope"}}),
        ):
            assert uptodown_api.resolve_download_url("16906", "555") is None

    # Missing identifiers must not cause network traffic at all.
    with mock.patch.object(uptodown_api, "_request", lambda *a, **k: pytest_fail("no request")):
        assert uptodown_api.resolve_download_url(None, "555") is None
        assert uptodown_api.resolve_download_url("16906", "") is None


def test_auth_failure_and_unreachable_api_return_none():
    with mock.patch.object(uptodown_api, "_request", lambda *a, **k: FakeResponse(status_code=403)):
        uptodown_api.reset_session()
        assert uptodown_api.get_token() is None

    with mock.patch.object(uptodown_api, "_request", lambda *a, **k: None):
        uptodown_api.reset_session()
        assert uptodown_api.get_token() is None

    with mock.patch.object(
        uptodown_api, "_request", lambda *a, **k: FakeResponse(body={"nope": True})
    ):
        uptodown_api.reset_session()
        assert uptodown_api.get_token() is None


def pytest_fail(message):
    raise AssertionError(message)


# --- Wiring: src/uptodown.py must prefer the mobile API over web scraping ---

LISTING_HTML = '<html><body><h1 id="detail-app-name" data-code="16906">YouTube</h1></body></html>'
VERSIONS_JSON = {
    "data": [
        {
            "version": "21.10.5",
            "versionURL": {
                "url": "https://youtube.en.uptodown.com/android",
                "extraURL": "app/youtube",
                "versionID": "991122",
            },
        }
    ]
}
VERSION_PAGE_HTML = (
    '<html><body><h1 id="detail-app-name" data-code="16906"></h1>'
    '<button id="detail-download-button" data-url="legacy-file-id" data-file-id="555"></button>'
    "</body></html>"
)


def _fake_scraper():
    """Patch src.uptodown._get so the version listing looks like real Uptodown."""

    def fake_get(url):
        if url.endswith("/versions"):
            return FakeResponse(html=LISTING_HTML, url=url)
        if "/versions/" in url:
            return FakeResponse(body=VERSIONS_JSON, url=url)
        if "991122" in url:
            return FakeResponse(html=VERSION_PAGE_HTML, url=url)
        raise AssertionError(f"unexpected scraper URL {url}")

    return fake_get


def test_get_download_link_prefers_mobile_api():
    calls = []

    def fake_resolve(app_code, file_id):
        calls.append((str(app_code), str(file_id)))
        return "https://dw.uptodown.com/dwn/API"

    with mock.patch.object(uptodown, "_get", _fake_scraper()):
        with mock.patch.object(uptodown.uptodown_api, "resolve_download_url", fake_resolve):
            link = uptodown.get_download_link("21.10.5", "youtube", {"package": "com.google.android.youtube"})

    assert link == "https://dw.uptodown.com/dwn/API"
    assert calls[0] == ("16906", "555"), "the numeric app code and page file id must be used"


def test_get_download_link_falls_back_to_web_flow():
    with mock.patch.object(uptodown, "_get", _fake_scraper()):
        with mock.patch.object(
            uptodown.uptodown_api, "resolve_download_url", lambda app, file: None
        ):
            link = uptodown.get_download_link("21.10.5", "youtube", {"package": "com.google.android.youtube"})
    assert link == "https://dw.uptodown.com/dwn/legacy-file-id", "legacy data-url must still work"


def test_get_download_link_survives_api_exceptions():
    def boom(app_code, file_id):
        raise RuntimeError("api exploded")

    with mock.patch.object(uptodown, "_get", _fake_scraper()):
        with mock.patch.object(uptodown.uptodown_api, "resolve_download_url", boom):
            link = uptodown.get_download_link("21.10.5", "youtube", {"package": "com.google.android.youtube"})
    assert link == "https://dw.uptodown.com/dwn/legacy-file-id"


def main():
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        uptodown_api.reset_session()
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    main()
