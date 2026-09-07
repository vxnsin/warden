"""Draw assets/icon.png from the same mascot the banner is made of.

Chat windows will not render an SVG next to a message, and a webhook that
points at an image nobody can see looks worse than one with no image at all.
So the icon is a PNG, and it is generated rather than drawn by hand: the
mascot changes in one place or it does not change.

    uv run python scripts/make-icon.py
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from warden import theme

SIDE = 128
CELL = 6

BACKGROUND = theme.SCULK_RAISED
BLOCK = theme.GLOW_DIM
HEART = theme.GLOW


def rgb(said: str) -> tuple[int, int, int]:
    said = said.lstrip("#")
    return int(said[0:2], 16), int(said[2:4], 16), int(said[4:6], 16)


def pixels() -> list[list[tuple[int, int, int]]]:
    """The mascot, centred, one cell per character."""
    rows = theme.MASCOT
    wide = max(len(row) for row in rows)
    left = (SIDE - wide * CELL) // 2
    top = (SIDE - len(rows) * CELL) // 2

    canvas = [[rgb(BACKGROUND)] * SIDE for _ in range(SIDE)]
    for y, row in enumerate(rows):
        for x, char in enumerate(row):
            if char == " ":
                continue
            colour = rgb(HEART if char == theme.HEART else BLOCK)
            for down in range(CELL):
                for across in range(CELL):
                    at_y, at_x = top + y * CELL + down, left + x * CELL + across
                    if 0 <= at_y < SIDE and 0 <= at_x < SIDE:
                        canvas[at_y][at_x] = colour
    return canvas


def chunk(kind: bytes, body: bytes) -> bytes:
    return (
        struct.pack(">I", len(body))
        + kind
        + body
        + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    )


def png(canvas: list[list[tuple[int, int, int]]]) -> bytes:
    raw = b"".join(
        b"\x00" + b"".join(bytes(pixel) for pixel in row) for row in canvas
    )
    header = struct.pack(">IIBBBBB", SIDE, SIDE, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def main() -> None:
    where = Path(__file__).resolve().parents[1] / "assets" / "icon.png"
    where.write_bytes(png(pixels()))
    print(f"{where} - {where.stat().st_size} bytes")


if __name__ == "__main__":
    main()
