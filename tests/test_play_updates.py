"""Offline regression: python tests/test_play_updates.py (stdlib only)."""
import runpy
import struct
import tempfile
import zipfile
from pathlib import Path

helpers = runpy.run_path(str(Path(__file__).resolve().parents[1] / "src/play_updates.py"))
rewrite = helpers["_manifest_with_max_version_code"]
protect = helpers["disable_play_store_updates"]

# Minimal resource map + first-element chunks exercise the binary rewrite.
resource_map = struct.pack("<HHII", 0x0180, 8, 12, 0x0101021B)
attribute = struct.pack("<IIIHBBI", 1, 0, 2, 8, 0, 0x10, 123)
root = (struct.pack("<HHIII", 0x0102, 16, 56, 1, 0xFFFFFFFF)
        + struct.pack("<IIHHHHHH", 0xFFFFFFFF, 3, 20, 20, 1, 0, 0, 0)
        + attribute)
manifest = struct.pack("<HHI", 3, 8, 8 + len(resource_map) + len(root)) + resource_map + root
changed = rewrite(manifest)
expected = bytearray(manifest)
struct.pack_into("<I", expected, len(manifest) - 12, 0xFFFFFFFF)
struct.pack_into("<I", expected, len(manifest) - 4, 0x7FFFFFFF)
assert changed == expected, "Only versionCode and its raw string should change"
assert rewrite(changed) == changed, "Protected builds must remain idempotent"

wrong_type = bytearray(manifest)
wrong_type[-5] = 3
bad_chunk = bytearray(manifest)
struct.pack_into("<I", bad_chunk, 12, 0)
wrong_id = bytearray(manifest)
struct.pack_into("<I", wrong_id, 16, 0x0101021C)
for bad in (b"", manifest[:-1], bytes(wrong_type), bytes(bad_chunk), bytes(wrong_id)):
    try:
        rewrite(bad)
    except ValueError:
        pass
    else:
        raise AssertionError("Malformed/unsupported manifest was accepted")

with tempfile.TemporaryDirectory() as directory:
    apk = Path(directory) / "test.apk"
    for xml in (manifest, b"invalid"):
        with zipfile.ZipFile(apk, "w") as archive:
            archive.comment = b"keep comment"
            archive.writestr("AndroidManifest.xml", xml)
            archive.writestr("classes.dex", b"keep code", compress_type=zipfile.ZIP_DEFLATED)
        original = apk.read_bytes()
        if xml == manifest:
            protect(apk)
            with zipfile.ZipFile(apk) as archive:
                assert archive.read("AndroidManifest.xml") == changed
                assert archive.read("classes.dex") == b"keep code"
                assert archive.getinfo("classes.dex").compress_type == zipfile.ZIP_DEFLATED
                assert archive.comment == b"keep comment"
        else:
            try:
                protect(apk)
            except ValueError:
                pass
            else:
                raise AssertionError("Invalid APK was accepted")
            assert apk.read_bytes() == original, "Failure must not overwrite the input"
        assert not apk.with_suffix(".no-play-updates.tmp").exists()
print("PASS: versionCode ceiling, idempotence, malformed input, ZIP preservation, atomic failure")
