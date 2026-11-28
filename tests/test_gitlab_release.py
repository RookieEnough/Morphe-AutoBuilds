"""Offline tests for the GitLab release publishing path.

Covers the failure modes seen in production:
- a publish must never delete/recreate the 'latest' tag (that wipes the release
  and every asset link);
- publishing one app must not remove other apps' assets;
- only superseded versions of the same app/arch/patch identity are pruned;
- repeated uploads must collapse to one package entry per filename;
- dead manifest references are cleared.

Run with: python tests/test_gitlab_release.py
"""

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}
        self.text = ""

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gitlab_api = _load(ROOT / "src" / "gitlab_api.py", "gitlab_api_for_tests")
publish = _load(ROOT / "scripts" / "gitlab_publish_release.py", "gitlab_publish_for_tests")


def test_existing_tag_and_release_are_not_deleted():
    calls = []

    def fake_api(method, path, **kwargs):
        calls.append((method, path))
        if method == "GET" and path.startswith("/repository/tags/"):
            return FakeResponse(200, {})
        if method == "GET" and path.startswith("/releases/"):
            return FakeResponse(200, {"name": "n", "description": "d"})
        if method == "PUT" and path.startswith("/releases/"):
            return FakeResponse(200, {})
        raise AssertionError(f"unexpected call {method} {path}")

    with mock.patch.object(gitlab_api, "api", fake_api):
        gitlab_api.ensure_release("latest", "name", "desc", "main")

    assert not any(
        method == "DELETE" or path == "/repository/tags" for method, path in calls
    ), f"publishing must not move/delete the tag, calls: {calls}"
    assert ("PUT", "/releases/latest") in calls


def test_missing_tag_and_release_are_created():
    calls = []

    def fake_api(method, path, **kwargs):
        calls.append((method, path))
        if method == "GET" and path.startswith("/repository/tags/"):
            return FakeResponse(404)
        if method == "POST" and path == "/repository/tags":
            return FakeResponse(201, {})
        if method == "GET" and path.startswith("/releases/"):
            return FakeResponse(404)
        if method == "POST" and path == "/releases":
            return FakeResponse(201, {})
        raise AssertionError(f"unexpected call {method} {path}")

    with mock.patch.object(gitlab_api, "api", fake_api):
        gitlab_api.ensure_release("latest", "name", "desc", "abc123")

    assert ("POST", "/repository/tags") in calls
    assert ("POST", "/releases") in calls


def test_duplicate_package_entries_collapse_to_newest():
    files = [
        {"id": 1, "file_name": "app-v1.apk"},
        {"id": 4, "file_name": "app-v1.apk"},
        {"id": 2, "file_name": "other-v1.apk"},
    ]
    deleted = []

    def fake_api(method, path, **kwargs):
        deleted.append(path)
        return FakeResponse(204)

    with mock.patch.object(gitlab_api, "list_package_files", lambda: files), \
         mock.patch.object(gitlab_api, "_package_id", lambda: 9), \
         mock.patch.object(gitlab_api, "api", fake_api):
        removed = gitlab_api.dedupe_package_files()

    assert removed == 1
    assert deleted == ["/packages/9/package_files/1"], "keep the newest copy, drop old ones"


def test_superseded_only_matches_same_app_arch_and_patch():
    keep = {"google-news-universal-src-v2.apk", "discord-arm64-v8a-src-v9.apk"}
    candidates = {
        "google-news-universal-src-v1.apk",      # superseded
        "google-news-arm64-v8a-src-v1.apk",      # different arch -> keep
        "other-app-universal-src-v1.apk",        # different app -> keep
        "discord-arm64-v8a-src-v8.apk",          # superseded
    }
    assert publish.superseded_apks(keep, candidates) == [
        "discord-arm64-v8a-src-v8.apk",
        "google-news-universal-src-v1.apk",
    ]


def test_dead_manifest_references_are_cleared():
    path = ROOT / ".manifest-clear-test.json"
    path.write_text(json.dumps({
        "entries": {
            "a|src|universal": {"apk": "ok-app-universal-src-v1.apk", "built_version": "1"},
            "b|src|universal": {"apk": "hush-unpacker-0.25.0.apk", "built_version": ""},
        }
    }), encoding="utf-8")
    try:
        assert publish.manifest_apk_names(path) == {
            "ok-app-universal-src-v1.apk", "hush-unpacker-0.25.0.apk"
        }
        cleared = publish.prune_manifest_entries(path, {"ok-app-universal-src-v1.apk"})
        data = json.loads(path.read_text(encoding="utf-8"))
        assert cleared == 1
        assert data["entries"]["b|src|universal"]["apk"] == ""
        assert publish.manifest_apk_names(path) == {"ok-app-universal-src-v1.apk"}
    finally:
        path.unlink(missing_ok=True)


def test_publish_is_incremental_and_prunes_only_superseded():
    api = publish.gitlab_api
    work = ROOT / ".publish-test"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    try:
        (work / "keep-app-universal-src-v2.apk").write_bytes(b"new apk")
        (work / "manifest.json").write_text(json.dumps({
            "entries": {
                "keep-app|src|universal": {"apk": "keep-app-universal-src-v2.apk"},
                "other-app|src|universal": {"apk": "other-app-universal-src-v9.apk"},
                "tool|src|universal": {"apk": "hush-unpacker-0.25.0.apk"},
            }
        }), encoding="utf-8")

        links = [
            {"id": 11, "name": "keep-app-universal-src-v1.apk", "url": "url/v1"},
            {"id": 12, "name": "other-app-universal-src-v9.apk", "url": "url/v9"},
        ]
        package = [
            {"id": 1, "file_name": "keep-app-universal-src-v1.apk"},
            {"id": 2, "file_name": "other-app-universal-src-v9.apk"},
        ]
        created, deleted_links, deleted_files = [], [], []

        def fake_upload(path):
            name = Path(path).name
            package.append({"id": 90 + len(package), "file_name": name})
            return f"url/{name}"

        def fake_api(method, path, **kwargs):
            if method in ("POST", "PUT") and path.endswith("/assets/links"):
                created.append(kwargs["json"]["name"])
                return FakeResponse(201, {"id": 100 + len(created)})
            if method == "GET" and path.startswith("/releases/"):
                return FakeResponse(200, {"name": "n", "description": "d"})
            if method == "PUT" and path.startswith("/releases/"):
                return FakeResponse(200, {})
            raise AssertionError(f"unexpected call {method} {path}")

        with mock.patch.object(api, "list_package_files", lambda: [dict(f) for f in package]), \
             mock.patch.object(api, "list_asset_links", lambda tag: [dict(l) for l in links]), \
             mock.patch.object(api, "upload_package_file", fake_upload), \
             mock.patch.object(api, "delete_package_file", lambda n: deleted_files.append(n) or True), \
             mock.patch.object(api, "delete_asset_link", lambda tag, i: deleted_links.append(i) or True), \
             mock.patch.object(api, "dedupe_package_files", lambda: 0), \
             mock.patch.object(api, "ensure_release", lambda *a, **k: {}), \
             mock.patch.object(api, "package_file_url", lambda n: f"url/{n}"), \
             mock.patch.object(api, "api", fake_api), \
             mock.patch.object(sys, "argv", ["publish", "--apks-dir", str(work), "--merge"]):
            rc = publish.main()

        assert rc == 0
        assert "keep-app-universal-src-v2.apk" in created
        assert 11 in deleted_links, "the previous version's link must be replaced"
        assert "keep-app-universal-src-v1.apk" in deleted_files
        assert 12 not in deleted_links, "other apps must never lose their link"
        assert "other-app-universal-src-v9.apk" not in deleted_files
        assert "hush-unpacker-0.25.0.apk" not in created
    finally:
        shutil.rmtree(work, ignore_errors=True)


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
