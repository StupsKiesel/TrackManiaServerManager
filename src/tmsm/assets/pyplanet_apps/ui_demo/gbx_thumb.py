"""Extract the embedded JPEG thumbnail from a TM(2020) .Map.Gbx file.

The thumbnail JPEG is stored uncompressed inside the GBX header, wrapped
between the literal markers `<Thumbnail.jpg>` and `</Thumbnail.jpg>`. The
markers haven't changed across TM Forever / TMNF / TM2020, so a byte scan
works without parsing the full GBX structure.
"""
from __future__ import annotations

from pathlib import Path

_BEGIN = b"<Thumbnail.jpg>"
_END = b"</Thumbnail.jpg>"


def read_thumbnail(gbx_path: str | Path) -> bytes | None:
    try:
        # The header is at the top of the file; reading the first ~256 KiB
        # is always enough and avoids loading huge maps in full.
        with open(gbx_path, "rb") as fh:
            data = fh.read(512 * 1024)
    except OSError:
        return None
    i = data.find(_BEGIN)
    if i < 0:
        return None
    start = i + len(_BEGIN)
    end = data.find(_END, start)
    if end < 0:
        return None
    blob = data[start:end]
    return blob or None
