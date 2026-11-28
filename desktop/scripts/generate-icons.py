#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate crisp Google Material-style cross-platform app icons (SVG, PNG, ICO, ICNS) using stdlib Python."""

from __future__ import annotations

import shutil
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path
from typing import List, Tuple

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"

SVG_SOURCE = """<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024" role="img" aria-label="Stata MCP Studio">
  <defs>
    <linearGradient id="surface" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#FFFFFF"/>
      <stop offset="100%" stop-color="#F1F3F4"/>
    </linearGradient>
    <linearGradient id="g-yellow" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#FBBC04"/>
      <stop offset="100%" stop-color="#F29900"/>
    </linearGradient>
    <linearGradient id="g-green" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#34A853"/>
      <stop offset="100%" stop-color="#1E8E3E"/>
    </linearGradient>
    <linearGradient id="g-blue" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#4285F4"/>
      <stop offset="100%" stop-color="#1A73E8"/>
    </linearGradient>
    <linearGradient id="g-red" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="#EA4335"/>
      <stop offset="100%" stop-color="#D93025"/>
    </linearGradient>
  </defs>
  <!-- Google Material white squircle surface -->
  <rect x="64" y="64" width="896" height="896" rx="224" fill="url(#surface)" stroke="#DADCE0" stroke-width="10"/>
  <!-- Subtle baseline axis -->
  <rect x="212" y="756" width="600" height="20" rx="10" fill="#E8EAED"/>
  <!-- Three ascending Google-color vectorized data columns -->
  <rect x="228" y="512" width="132" height="224" rx="40" fill="url(#g-yellow)"/>
  <rect x="412" y="388" width="132" height="348" rx="40" fill="url(#g-green)"/>
  <rect x="596" y="252" width="132" height="484" rx="40" fill="url(#g-blue)"/>
  <!-- Google Red regression / vector-flow curve -->
  <path d="M236 422 L434 294 L608 214 L756 226" fill="none" stroke="url(#g-red)" stroke-width="44" stroke-linecap="round" stroke-linejoin="round"/>
  <!-- MCP hub connector node -->
  <circle cx="766" cy="226" r="68" fill="#FFFFFF"/>
  <circle cx="766" cy="226" r="52" fill="#4285F4"/>
  <circle cx="766" cy="226" r="22" fill="#FFFFFF"/>
</svg>
"""


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def _encode_png(width: int, height: int, rgba: bytearray) -> bytes:
    raw_rows = bytearray()
    stride = width * 4
    for y in range(height):
        raw_rows.append(0)
        raw_rows.extend(rgba[y * stride : (y + 1) * stride])
    compressed = zlib.compress(bytes(raw_rows), level=6)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", compressed)
        + _png_chunk(b"IEND", b"")
    )


def _blend_pixel(r_dst: float, g_dst: float, b_dst: float, a_dst: float, r: int, g: int, b: int, a: float) -> Tuple[float, float, float, float]:
    if a <= 0.0:
        return r_dst, g_dst, b_dst, a_dst
    if a >= 1.0:
        return float(r), float(g), float(b), 1.0
    inv = 1.0 - a
    out_a = a + a_dst * inv
    if out_a <= 1e-6:
        return 0.0, 0.0, 0.0, 0.0
    return (
        (r * a + r_dst * a_dst * inv) / out_a,
        (g * a + g_dst * a_dst * inv) / out_a,
        (b * a + b_dst * a_dst * inv) / out_a,
        out_a,
    )


def _sdf_rounded_rect(x: float, y: float, x0: float, y0: float, x1: float, y1: float, rad: float) -> float:
    """Signed distance to rounded rectangle (negative inside)."""
    cx = (x0 + x1) * 0.5
    cy = (y0 + y1) * 0.5
    hx = (x1 - x0) * 0.5 - rad
    hy = (y1 - y0) * 0.5 - rad
    qx = abs(x - cx) - hx
    qy = abs(y - cy) - hy
    ox = max(qx, 0.0)
    oy = max(qy, 0.0)
    return (ox * ox + oy * oy) ** 0.5 + min(max(qx, qy), 0.0) - rad


def _dist_seg(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    c1 = vx * wx + vy * wy
    if c1 <= 0.0:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    c2 = vx * vx + vy * vy
    if c2 <= c1:
        return ((px - bx) ** 2 + (py - by) ** 2) ** 0.5
    t = c1 / c2
    qx, qy = ax + t * vx, ay + t * vy
    return ((px - qx) ** 2 + (py - qy) ** 2) ** 0.5


def _cov(dist: float, aa: float) -> float:
    if dist <= -aa:
        return 1.0
    if dist >= aa:
        return 0.0
    return (aa - dist) / (2.0 * aa)


def _sample_icon(x: float, y: float, aa: float) -> Tuple[float, float, float, float]:
    # Outer white squircle surface (64..960, rx=224)
    d_sq = _sdf_rounded_rect(x, y, 64.0, 64.0, 960.0, 960.0, 224.0)
    cov_sq = _cov(d_sq, aa)
    if cov_sq <= 0.0:
        return 0.0, 0.0, 0.0, 0.0

    # Subtle Material surface gradient (#FFFFFF -> #F1F3F4)
    t_bg = min(1.0, max(0.0, (x + y - 128.0) / 1792.0))
    r = 255.0 + (241.0 - 255.0) * t_bg
    g = 255.0 + (243.0 - 255.0) * t_bg
    b = 255.0 + (244.0 - 255.0) * t_bg
    a = cov_sq

    # Subtle #DADCE0 border around squircle edge
    if -10.0 - aa <= d_sq <= aa:
        border_cov = min(_cov(d_sq, aa), 1.0 - _cov(d_sq + 10.0, aa))
        if border_cov > 0.0:
            r, g, b, a = _blend_pixel(r, g, b, a, 218, 220, 224, border_cov * cov_sq)

    # Subtle baseline axis (#E8EAED)
    d_axis = _sdf_rounded_rect(x, y, 212.0, 756.0, 812.0, 776.0, 10.0)
    c_axis = _cov(d_axis, aa)
    if c_axis > 0.0:
        r, g, b, a = _blend_pixel(r, g, b, a, 232, 234, 237, c_axis)

    # Three ascending Google 4-color bars: Yellow -> Green -> Blue
    bars = [
        (228.0, 512.0, 360.0, 736.0, 40.0, (251, 188, 4), (242, 153, 0)),
        (412.0, 388.0, 544.0, 736.0, 40.0, (52, 168, 83), (30, 142, 62)),
        (596.0, 252.0, 728.0, 736.0, 40.0, (66, 133, 244), (26, 115, 232)),
    ]
    for x0, y0, x1, y1, rad, top_rgb, bot_rgb in bars:
        d_bar = _sdf_rounded_rect(x, y, x0, y0, x1, y1, rad)
        c_bar = _cov(d_bar, aa)
        if c_bar > 0.0:
            tb = min(1.0, max(0.0, (y - y0) / (y1 - y0)))
            br = int(top_rgb[0] + (bot_rgb[0] - top_rgb[0]) * tb)
            bg = int(top_rgb[1] + (bot_rgb[1] - top_rgb[1]) * tb)
            bb = int(top_rgb[2] + (bot_rgb[2] - top_rgb[2]) * tb)
            r, g, b, a = _blend_pixel(r, g, b, a, br, bg, bb, c_bar)

    # Google Red trend curve (#EA4335 -> #D93025), stroke width 44 => radius 22
    segs = [
        (236.0, 422.0, 434.0, 294.0),
        (434.0, 294.0, 608.0, 214.0),
        (608.0, 214.0, 756.0, 226.0),
    ]
    d_line = min(_dist_seg(x, y, ax, ay, bx, by) for ax, ay, bx, by in segs) - 22.0
    c_line = _cov(d_line, aa)
    if c_line > 0.0:
        tr = min(1.0, max(0.0, (x - 236.0) / 520.0))
        rr = int(234 + (217 - 234) * tr)
        rg = int(67 + (48 - 67) * tr)
        rb = int(53 + (37 - 53) * tr)
        r, g, b, a = _blend_pixel(r, g, b, a, rr, rg, rb, c_line)

    # MCP hub connector node at (766, 226)
    d_node = ((x - 766.0) ** 2 + (y - 226.0) ** 2) ** 0.5
    c_outer = _cov(d_node - 68.0, aa)
    if c_outer > 0.0:
        r, g, b, a = _blend_pixel(r, g, b, a, 255, 255, 255, c_outer)
    c_mid = _cov(d_node - 52.0, aa)
    if c_mid > 0.0:
        r, g, b, a = _blend_pixel(r, g, b, a, 66, 133, 244, c_mid)
    c_core = _cov(d_node - 22.0, aa)
    if c_core > 0.0:
        r, g, b, a = _blend_pixel(r, g, b, a, 255, 255, 255, c_core)

    return r, g, b, a


def render_icon_rgba(size: int) -> bytearray:
    buf = bytearray(size * size * 4)
    scale = size / 1024.0
    aa = max(0.8, 0.9 / scale)
    sub = 2 if size <= 256 else 1
    offsets = (0.25, 0.75) if sub == 2 else (0.5,)
    inv_samples = 1.0 / (sub * sub)

    for py in range(size):
        for px in range(size):
            sr = sg = sb = sa = 0.0
            for oy in offsets:
                y = (py + oy) / scale
                for ox in offsets:
                    x = (px + ox) / scale
                    r, g, b, a = _sample_icon(x, y, aa)
                    sr += r * a
                    sg += g * a
                    sb += b * a
                    sa += a
            idx = (py * size + px) * 4
            out_a = sa * inv_samples
            if out_a <= 1e-6:
                continue
            buf[idx] = min(255, max(0, int(round(sr / sa))))
            buf[idx + 1] = min(255, max(0, int(round(sg / sa))))
            buf[idx + 2] = min(255, max(0, int(round(sb / sa))))
            buf[idx + 3] = min(255, max(0, int(round(out_a * 255.0))))
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

    master_rgba = render_icon_rgba(1024)
    master_png = _encode_png(1024, 1024, master_rgba)
    png_path = ASSETS_DIR / "icon.png"
    png_path.write_bytes(master_png)

    ico_entries: List[Tuple[int, bytes]] = []
    for sz in (16, 32, 48, 64, 128, 256):
        rgba = render_icon_rgba(sz)
        ico_entries.append((sz, _encode_png(sz, sz, rgba)))
    _write_ico(ASSETS_DIR / "icon.ico", ico_entries)

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
