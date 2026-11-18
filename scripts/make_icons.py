"""Write the PWA icons: solid green squares with a white "M" (stdlib only). Run: python scripts/make_icons.py"""
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "app" / "static" / "icons"
BG, FG = (46, 125, 50), (255, 255, 255)


def _in_m(x: float, y: float) -> bool:
    """Is the unit-square point (x right, y down) part of the letter?"""
    if not 0.25 <= y <= 0.75:
        return False
    if 0.22 <= x <= 0.34 or 0.66 <= x <= 0.78:  # the two legs
        return True
    # two diagonals from the top corners down to the middle at y = 0.58
    for x0, x1 in ((0.28, 0.5), (0.72, 0.5)):
        t = (y - 0.25) / 0.33
        if 0 <= t <= 1 and abs(x - (x0 + (x1 - x0) * t)) <= 0.065:
            return True
    return False


def png(size: int) -> bytes:
    rows = b"".join(b"\x00" + b"".join(bytes(FG if _in_m((c + .5) / size, (r + .5) / size) else BG)
                                       for c in range(size)) for r in range(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, size in (("icon-192.png", 192), ("icon-512.png", 512), ("apple-touch-icon.png", 180)):
        (OUT / name).write_bytes(png(size))
        print(OUT / name)
