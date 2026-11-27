#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate crisp cross-platform app icons (SVG, PNG, ICO, ICNS) using stdlib Python."""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path
from typing import Callable, List, Tuple

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"

SVG_SOURCE = """<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">
  <defs>
    <linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#0f2942"/>
      <stop offset="55%" stop-color="#174c78"/>
      <stop offset="100%" stop-color="#1e6b8f"/>
    </linearGradient>
    <linearGradient id="bar" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#38bdf8"/>
      <stop offset="100%" stop-color="#0ea5e9"/>
    </linearGradient>
  </defs>
  <rect x="64" y="64" width="896" height="896" rx="200" fill="url(#bg)"/>
  <rect x="120" y="120" width="784" height="784" rx="160" fill="none" stroke="#38bdf8" stroke-opacity="0.25" stroke-width="8"/>
  <rect x="240" y="540" width="110" height="220" rx="24" fill="url(#bar)"/>
  <rect x="395" y="420" width="110" height="340" rx="24" fill="url(#bar)"/>
  <rect x="550" y="300" width="110" height="460" rx="24" fill="url(#bar)"/>
  <path d="M235 450 L395 340 L550 245 L755 215" fill="none" stroke="#f8fafc" stroke-width="26" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="755" cy="215" r="36" fill="#22c55e" stroke="#f8fafc" stroke-width="12"/>
  <rect x="705" y="360" width="90" height="400" rx="24" fill="#e2e8f0" fill-opacity="0.92"/>
</svg>
"""


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def _encode_png(width: int, height: int, rgba: bytearray) -> bytes:
    raw_rows = bytearray()
    stride = width * 4
    for y in range(height):
        raw_rows.append(0)  # filter type 0
        raw_rows.extend(rgba[y * stride : (y + 1) * stride])
    compressed = zlib.compress(bytes(raw_rows), level=6)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", compressed)
        + _png_chunk(b"IEND", b"")
    )


def _blend(dst: bytearray, idx: int, r: int, g: int, b: int, a: int) -> None:
    if a <= 0:
        return
    if a >= 255:
        dst[idx] = r
        dst[idx + 1] = g
        dst[idx + 2] = b
        dst[idx + 3] = 255
        return
    da = dst[idx + 3]
    sa = a / 255.0
    inv = 1.0 - sa
    out_a = sa + (da / 255.0) * inv
    if out_a <= 0:
        return
    dst[idx] = int((r * sa + dst[idx] * (da / 255.0) * inv) / out_a)
    dst[idx + 1] = int((g * sa + dst[idx + 1] * (da / 255.0) * inv) / out_a)
    dst[idx + 2] = int((b * sa + dst[idx + 2] * (da / 255.0) * inv) / out_a)
    dst[idx + 3] = int(out_a * 255)


def _in_rounded_rect(x: float, y: float, x0: float, y0: float, x1: float, y1: float, rad: float) -> bool:
    if x < x0 or x > x1 or y < y0 or y > y1:
        return False
    cx = min(max(x, x0 + rad), x1 - rad)
    cy = min(max(y, y0 + rad), y1 - rad)
    dx = x - cx
    dy = y - cy
    return (dx * dx + dy * dy) <= rad * rad


def _dist_seg(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    c1 = vx * wx + vy * wy
    if c1 <= 0:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    c2 = vx * vx + vy * vy
    if c2 <= c1:
        return ((px - bx) ** 2 + (py - by) ** 2) ** 0.5
    t = c1 / c2
    qx, qy = ax + t * vx, ay + t * vy
    return ((px - qx) ** 2 + (py - qy) ** 2) ** 0.5


def render_icon_rgba(size: int) -> bytearray:
    buf = bytearray(size * size * 4)
    scale = size / 1024.0
    segs = [
        (240.0, 445.0, 420.0, 330.0),
        (420.0, 330.0, 585.0, 235.0),
        (585.0, 235.0, 760.0, 205.0),
    ]
    bars = [
        (220.0, 540.0, 330.0, 765.0, 20.0, (56, 189, 248, 255)),
        (375.0, 420.0, 485.0, 765.0, 20.0, (14, 165, 233, 255)),
        (530.0, 300.0, 640.0, 765.0, 20.0, (56, 189, 248, 255)),
        (695.0, 355.0, 795.0, 765.0, 20.0, (226, 232, 240, 240)),
    ]

    for py in range(size):
        y = (py + 0.5) / scale
        for px in range(size):
            x = (px + 0.5) / scale
            idx = (py * size + px) * 4

            if not _in_rounded_rect(x, y, 64.0, 64.0, 960.0, 960.0, 200.0):
                continue

            # Diagonal deep-ocean gradient
            t = min(1.0, max(0.0, (x + y - 128.0) / 1792.0))
            r = int(15 + (30 - 15) * t)
            g = int(41 + (107 - 41) * t)
            b = int(66 + (143 - 66) * t)
            _blend(buf, idx, r, g, b, 255)

            # Subtle inner border
            if _in_rounded_rect(x, y, 114.0, 114.0, 910.0, 910.0, 158.0) and not _in_rounded_rect(
                x, y, 124.0, 124.0, 900.0, 900.0, 150.0
            ):
                _blend(buf, idx, 56, 189, 248, 70)

            # Bars
            for bx0, by0, bx1, by1, brad, col in bars:
                if _in_rounded_rect(x, y, bx0, by0, bx1, by1, brad):
                    _blend(buf, idx, *col)

            # Trend line
            min_d = min(_dist_seg(x, y, ax, ay, bx, by) for ax, ay, bx, by in segs)
            if min_d <= 15.0:
                alpha = 255 if min_d <= 12.0 else int(255 * max(0.0, 1.0 - (min_d - 12.0) / 3.0))
                _blend(buf, idx, 248, 250, 252, alpha)

            # Green status node at top-right of trend line
            dist_c = ((x - 760.0) ** 2 + (y - 205.0) ** 2) ** 0.5
            if dist_c <= 42.0:
                if dist_c <= 28.0:
                    _blend(buf, idx, 34, 197, 94, 255)
                else:
                    _blend(buf, idx, 248, 250, 252, 255)
    return buf


def _write_ico(path: Path, png_entries: List[Tuple[int, bytes]]) -> None:
    header = struct.pack("<HHH", 0, 1, len(png_entries))
    offset = 6 + 16 * len(png_entries)
    dir_entries = bytearray()
    payload = bytearray()
    for sz, png_bytes in png_entries:
        dim = 0 if sz >= 256 else sz
        dir_entries.extend(struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png_bytes), offset))
        payload.extend(png_bytes)
        offset += len(png_bytes)
    path.write_bytes(header + bytes(dir_entries) + bytes(payload))


def main() -> None:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    (ASSETS_DIR / "icon.svg").write_text(SVG_SOURCE, encoding="utf-8")

    # Render 1024x1024 master PNG
    master_rgba = render_icon_rgba(1024)
    master_png = _encode_png(1024, 1024, master_rgba)
    png_path = ASSETS_DIR / "icon.png"
    png_path.write_bytes(master_png)

    # Render ICO sizes (16, 32, 48, 64, 128, 256)
    ico_entries: List[Tuple[int, bytes]] = []
    for sz in (16, 32, 48, 64, 128, 256):
        rgba = render_icon_rgba(sz)
        ico_entries.append((sz, _encode_png(sz, sz, rgba)))
    _write_ico(ASSETS_DIR / "icon.ico", ico_entries)

    # On macOS build .icns via sips + iconutil
    if shutil.which("sips") and shutil.which("iconutil"):
        with tempfile.TemporaryDirectory() as tmp:
            iconset = Path(tmp) / "icon.iconset"
            iconset.mkdir()
            pairs = [
                (16, "icon_16x16.png"),
                (32, "icon_16x16@2x.png"),
                (32, "icon_32x32.png"),
                (64, "icon_32x32@2x.png"),
                (128, "icon_128x128.png"),
                (256, "icon_128x128@2x.png"),
                (256, "icon_256x256.png"),
                (512, "icon_256x256@2x.png"),
                (512, "icon_512x512.png"),
                (1024, "icon_512x512@2x.png"),
            ]
            for sz, name in pairs:
                out_file = iconset / name
                if sz == 1024:
                    shutil.copy2(png_path, out_file)
                else:
                    subprocess.run(
                        ["sips", "-z", str(sz), str(sz), str(png_path), "--out", str(out_file)],
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
            subprocess.run(
                ["iconutil", "-c", "icns", str(iconset), "-o", str(ASSETS_DIR / "icon.icns")],
                check=True,
            )


if __name__ == "__main__":
    main()
