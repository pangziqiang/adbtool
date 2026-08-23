#!/usr/bin/env python3
"""生成 adbtool 应用图标（icns）。

用法:
    venv/bin/python scripts/gen_icon.py [AT字号] [TOOL字号] [行距]

默认生成到 assets/adbtool.icns，参数可用于微调排版。
"""
import os
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont

W = H = 1024
AT_FONT = "/System/Library/Fonts/Supplemental/Arial Black.ttf"
TOOL_FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
TEXT_COLOR = (105, 200, 245, 255)


def make_icon(at_size=350, tool_size=150, gap=160) -> Image.Image:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    top = (30, 60, 90, 255)
    bot = (48, 92, 128, 255)
    dg = ImageDraw.Draw(img)
    for y in range(H):
        t = y / (H - 1)
        c = tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(4))
        dg.line([(0, y), (W, y)], fill=c)
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [18, 18, W - 18, H - 18], radius=185, fill=255
    )
    img.putalpha(mask)

    d = ImageDraw.Draw(img)
    at_font = ImageFont.truetype(AT_FONT, at_size)
    tool_font = ImageFont.truetype(TOOL_FONT, tool_size)
    total = at_size + tool_size + gap
    y_at = (H - total) / 2 + at_size / 2
    y_tool = (H - total) / 2 + at_size + gap + tool_size / 2
    d.text((W / 2, y_at), "AT", font=at_font, fill=TEXT_COLOR, anchor="mm")
    d.text((W / 2, y_tool), "ADB TOOL", font=tool_font, fill=TEXT_COLOR, anchor="mm")
    return img


def main():
    at = int(sys.argv[1]) if len(sys.argv) > 1 else 350
    tool = int(sys.argv[2]) if len(sys.argv) > 2 else 150
    gap = int(sys.argv[3]) if len(sys.argv) > 3 else 160
    base = make_icon(at, tool, gap)

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_icns = os.path.join(root, "assets", "adbtool.icns")

    with tempfile.TemporaryDirectory() as td:
        iconset = os.path.join(td, "adbtool.iconset")
        os.makedirs(iconset)
        for size, name in [
            (16, "icon_16x16.png"), (32, "icon_16x16@2x.png"),
            (32, "icon_32x32.png"), (64, "icon_32x32@2x.png"),
            (128, "icon_128x128.png"), (256, "icon_128x128@2x.png"),
            (256, "icon_256x256.png"), (512, "icon_256x256@2x.png"),
            (512, "icon_512x512.png"), (1024, "icon_512x512@2x.png"),
        ]:
            base.resize((size, size), Image.Resampling.LANCZOS).save(
                os.path.join(iconset, name)
            )
        subprocess.run(
            ["iconutil", "-c", "icns", iconset, "-o", out_icns], check=True
        )
    print(f"已生成 {out_icns}（AT={at} TOOL={tool} gap={gap}）")


if __name__ == "__main__":
    main()
