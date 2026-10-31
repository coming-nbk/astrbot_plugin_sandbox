#!/usr/bin/env python3
# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later

"""生成插件图标 logo.png / icon.png。

不依赖 Pillow：直接拼 PNG（zlib + struct），4 倍超采样做抗锯齿。
图形是「终端提示符」：圆角方块 + 白色 `>` 和 `_`，48px 下也能认出来。

    python3 scripts/make_logo.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

SIZE = 512
SS = 4  # 超采样倍数
ROOT = Path(__file__).resolve().parents[1]

# 主题色（和页面里的 --color-primary 一致）
TOP = (37, 99, 235)  # #2563EB
BOTTOM = (29, 78, 216)  # #1D4ED8
WHITE = (255, 255, 255)


def _rounded_alpha(x: float, y: float, size: float, radius: float) -> float:
    """圆角矩形的覆盖率：内部 1，外部 0，边缘按距离过渡。"""
    half = size / 2
    cx = abs(x - half) - (half - radius)
    cy = abs(y - half) - (half - radius)
    dx = max(cx, 0.0)
    dy = max(cy, 0.0)
    outside = (dx * dx + dy * dy) ** 0.5
    inside = min(max(cx, cy), 0.0)
    distance = outside + inside - radius
    # distance <= 0 在内部；用 1px 过渡
    return max(0.0, min(1.0, 0.5 - distance))


def _segment_distance(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> float:
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    length2 = vx * vx + vy * vy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, (wx * vx + wy * vy) / length2))
    dx, dy = wx - t * vx, wy - t * vy
    return (dx * dx + dy * dy) ** 0.5


def _glyph_alpha(x: float, y: float) -> float:
    """`>` 与 `_` 的覆盖率。坐标按 512 设计。"""
    alpha = 0.0
    stroke = 26.0
    # 尖括号 >
    alpha = max(
        alpha,
        max(
            0.0,
            min(
                1.0,
                0.5
                - (
                    _segment_distance(x, y, 176, 176, 256, 256) - stroke / 2
                ),
            ),
        ),
    )
    alpha = max(
        alpha,
        max(
            0.0,
            min(
                1.0,
                0.5
                - (
                    _segment_distance(x, y, 256, 256, 176, 336) - stroke / 2
                ),
            ),
        ),
    )
    # 下划线 _
    alpha = max(
        alpha,
        max(
            0.0,
            min(
                1.0,
                0.5
                - (
                    _segment_distance(x, y, 296, 336, 356, 336) - stroke / 2
                ),
            ),
        ),
    )
    return alpha


def render() -> bytes:
    pixels = bytearray()
    step = 1.0 / SS
    for py in range(SIZE):
        row = bytearray()
        for px in range(SIZE):
            bg_cov = 0.0
            glyph_cov = 0.0
            for sy in range(SS):
                for sx in range(SS):
                    x = px + (sx + 0.5) * step
                    y = py + (sy + 0.5) * step
                    bg_cov += _rounded_alpha(x, y, SIZE, 108)
                    glyph_cov += _glyph_alpha(x, y)
            samples = SS * SS
            bg = bg_cov / samples
            glyph = min(1.0, glyph_cov / samples)

            # 竖直渐变
            t = py / (SIZE - 1)
            base = tuple(
                int(TOP[i] + (BOTTOM[i] - TOP[i]) * t) for i in range(3)
            )
            color = tuple(
                int(base[i] * (1 - glyph) + WHITE[i] * glyph) for i in range(3)
            )
            alpha = int(round(bg * 255))
            row.extend((*color, alpha))
        pixels.extend(b"\x00")  # 每行滤波类型 0
        pixels.extend(row)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(pixels), 9))
        + chunk(b"IEND", b"")
    )


def main() -> int:
    data = render()
    for name in ("logo.png", "icon.png"):
        path = ROOT / name
        path.write_bytes(data)
        print(f"已生成 {path.name}  {len(data) / 1024:.1f} KB  {SIZE}x{SIZE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
