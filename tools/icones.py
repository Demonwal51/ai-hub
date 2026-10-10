"""Draw the app's icons (PNG) without any image library.

A hub: one central node linked to three others, white on the app's blue.
"Maskable" icons keep the drawing inside the central 80 % that Android may
crop to a circle.

    python -B tools/icones.py
"""

import math
import struct
import sys
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "aihub" / "web" / "static"
BLUE = (47, 91, 211)
WHITE = (255, 255, 255)
SAMPLES = 4  # per axis, for smooth edges


def _segment_distance(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def _shape(x, y, scale):
    """True if (x, y), in 0..1 units of the icon, is white."""
    cx = cy = 0.5
    # Coordinates relative to the centre, shrunk for maskable icons.
    u, v = (x - cx) / scale + cx, (y - cy) / scale + cy
    nodes = [(0.5, 0.21), (0.24, 0.70), (0.76, 0.70)]
    if math.hypot(u - cx, v - cy) <= 0.12:
        return True
    for nx, ny in nodes:
        if math.hypot(u - nx, v - ny) <= 0.085:
            return True
        if _segment_distance(u, v, cx, cy, nx, ny) <= 0.028:
            return True
    return False


def _rounded(x, y, radius):
    """Inside a square with rounded corners (radius in 0..0.5)."""
    qx, qy = abs(x - 0.5) - (0.5 - radius), abs(y - 0.5) - (0.5 - radius)
    return math.hypot(max(qx, 0), max(qy, 0)) <= radius


def draw(size, maskable):
    rows = []
    for j in range(size):
        row = bytearray([0])  # filter: none
        for i in range(size):
            white = inside = 0
            for sj in range(SAMPLES):
                for si in range(SAMPLES):
                    x = (i + (si + 0.5) / SAMPLES) / size
                    y = (j + (sj + 0.5) / SAMPLES) / size
                    if maskable or _rounded(x, y, 0.22):
                        inside += 1
                        if _shape(x, y, 0.72 if maskable else 1.0):
                            white += 1
            n = SAMPLES * SAMPLES
            a = inside / n
            w = white / inside if inside else 0
            color = [round(BLUE[k] * (1 - w) + WHITE[k] * w) for k in range(3)]
            row += bytes(color + [round(a * 255)])
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8 bits, RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


def main():
    targets = {"icon-192.png": (192, False), "icon-512.png": (512, False),
               "icon-maskable-512.png": (512, True), "apple-touch-icon.png": (180, True)}
    for name, (size, maskable) in targets.items():
        data = draw(size, maskable)
        (OUT / name).write_bytes(data)
        written = (OUT / name).read_bytes()
        if written != data or not written.startswith(b"\x89PNG"):
            print(f"écriture incorrecte : {name}")
            return 1
        print(f"{name} : {size}x{size}, {len(data)} octets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
