"""Set the same version-code ceiling as Morphe's Disable Play Store updates patch."""
import os
import struct
import zipfile
from pathlib import Path

MAX_VERSION_CODE = 0x7FFFFFFF


def _manifest_with_max_version_code(data: bytes) -> bytes:
    result = bytearray(data)
    if len(result) < 8 or struct.unpack_from("<HHI", result) != (3, 8, len(result)):
        raise ValueError("Invalid binary AndroidManifest.xml header")
    version_index = None
    root_seen = False
    changed = False
    offset = 8
    while offset < len(result):
        if offset + 8 > len(result):
            raise ValueError("Truncated XML chunk header")
        kind, header, size = struct.unpack_from("<HHI", result, offset)
        if header < 8 or size < header or offset + size > len(result):
            raise ValueError("Invalid XML chunk bounds")
        if kind == 0x0180:
            if header != 8 or (size - header) % 4 or root_seen:
                raise ValueError("Invalid XML resource map")
            for index in range((size - header) // 4):
                if struct.unpack_from("<I", result, offset + header + index * 4)[0] == 0x0101021B:
                    version_index = index
        elif kind == 0x0102 and not root_seen:
            root_seen = True
            if header != 16 or size < header + 20:
                raise ValueError("Invalid manifest root element")
            extension = offset + header
            start, width, count = struct.unpack_from("<HHH", result, extension + 8)
            if start < 20 or width != 20 or header + start + width * count > size:
                raise ValueError("Invalid manifest attributes")
            for index in range(count):
                attribute = extension + start + index * width
                namespace, name, raw, value_size, reserved, value_type, value = struct.unpack_from("<IIIHBBI", result, attribute)
                if version_index is not None and name == version_index:
                    if changed or namespace == 0xFFFFFFFF or value_size != 8 or reserved or value_type not in (0x10, 0x11):
                        raise ValueError("Invalid android:versionCode attribute")
                    # An integer value must not retain a conflicting raw string.
                    struct.pack_into("<I", result, attribute + 8, 0xFFFFFFFF)
                    struct.pack_into("<I", result, attribute + 16, MAX_VERSION_CODE)
                    changed = True
        offset += size
    if not changed:
        raise ValueError("Manifest has no integer android:versionCode")
    return bytes(result)


def disable_play_store_updates(apk: Path) -> None:
    """Rewrite only the manifest before signing; keep versionName and package ID."""
    temporary = apk.with_suffix(".no-play-updates.tmp")
    try:
        with zipfile.ZipFile(apk) as source:
            if source.namelist().count("AndroidManifest.xml") != 1:
                raise ValueError("APK must contain exactly one AndroidManifest.xml")
            manifest = _manifest_with_max_version_code(source.read("AndroidManifest.xml"))
            with zipfile.ZipFile(temporary, "w") as target:
                target.comment = source.comment
                for entry in source.infolist():
                    target.writestr(entry, manifest if entry.filename == "AndroidManifest.xml" else source.read(entry))
        os.replace(temporary, apk)
    finally:
        temporary.unlink(missing_ok=True)
