# -*- coding: utf-8 -*-
"""生成一个「奶龙」风格的备用通知图标（奶龙-绘制版.png）。

**平时完全用不到这个脚本。** 当前正在用的图标是 `通知图标.png`（外部图片处理来的），
本脚本不会碰它——默认写到 `奶龙-绘制版.png`，想换成绘制版就自己改名或去设置里指过去。

只有想改配色/比例时才需要跑：

    pip install pillow
    python 生成通知图标.py                 # 输出 奶龙-绘制版.png
    python 生成通知图标.py 别的名字.png

绘制思路：先在 1024×1024 画布上画（头 + 两只耳朵取并集算干净描边），
再缩到 256×256 并做调色板量化，体积从 30KB 降到约 5KB。
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

S = 1024          # 绘制画布
OUT = 256         # 输出尺寸

YELLOW = (255, 199, 20, 255)
OUTLINE = (226, 138, 0, 255)
EYE = (38, 27, 23, 255)
WHITE = (255, 255, 255, 255)
BLUSH = (255, 140, 105, 100)
MOUTH = (92, 42, 36, 255)
TONGUE = (255, 132, 148, 255)

HEAD = (90, 195, 934, 945)            # 头（大圆，略扁）
EAR_L = (180, 85, 430, 335)           # 左耳
EAR_R = (594, 85, 844, 335)           # 右耳
EYE_L = (285, 400, 475, 605)          # 左眼（大圆眼）
EYE_R = (549, 400, 739, 605)          # 右眼
HI_BIG_L = (296, 408, 392, 504)       # 左眼大高光
HI_BIG_R = (560, 408, 656, 504)       # 右眼大高光
HI_SMALL_L = (409, 541, 447, 579)     # 左眼小高光
HI_SMALL_R = (673, 541, 711, 579)     # 右眼小高光
MOUTH_BOX = (390, 660, 634, 830)      # 嘴（大张口）
TEETH_BOX = (410, 648, 614, 700)      # 上排牙
TONGUE_BOX = (424, 735, 600, 855)     # 舌头
BLUSH_L = (155, 645, 325, 740)        # 左腮红
BLUSH_R = (699, 645, 869, 740)        # 右腮红


def build() -> Image.Image:
    # 1) 身体轮廓（两耳 + 头 的并集）
    mask = Image.new("L", (S, S), 0)
    md = ImageDraw.Draw(mask)
    md.ellipse(EAR_L, fill=255)
    md.ellipse(EAR_R, fill=255)
    md.ellipse(HEAD, fill=255)

    # 2) 由并集算出干净的外描边（膨胀 - 原形），避免耳朵和头交界处出现接缝
    grown = mask.filter(ImageFilter.MaxFilter(45))
    outline = ImageChops.subtract(grown, mask)

    body = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    body.paste(OUTLINE, (0, 0), outline)
    body.paste(YELLOW, (0, 0), mask)

    # 3) 顶部柔光，让大圆头有点体积感
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((230, 250, 650, 470), fill=(255, 255, 255, 60))
    glow = glow.filter(ImageFilter.GaussianBlur(80))
    body = Image.alpha_composite(body, Image.composite(
        glow, Image.new("RGBA", (S, S), (0, 0, 0, 0)), mask))

    # 4) 五官
    feat = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # 腮红
    blush = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    bd = ImageDraw.Draw(blush)
    bd.ellipse(BLUSH_L, fill=BLUSH)
    bd.ellipse(BLUSH_R, fill=BLUSH)
    feat = Image.alpha_composite(feat, blush.filter(ImageFilter.GaussianBlur(18)))
    fd = ImageDraw.Draw(feat)

    # 眼睛
    for box in (EYE_L, EYE_R):
        fd.ellipse(box, fill=EYE)
    # 高光：大点 + 小点，做出水汪汪的感觉
    for box in (HI_BIG_L, HI_BIG_R):
        fd.ellipse(box, fill=WHITE)
    for box in (HI_SMALL_L, HI_SMALL_R):
        fd.ellipse(box, fill=(255, 255, 255, 200))

    # 嘴：大张口 -> 上排牙 -> 舌头，后两者裁在嘴形内
    mouth = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mouth).ellipse(MOUTH_BOX, fill=255)
    fd.bitmap((0, 0), mouth, fill=MOUTH)
    teeth = Image.new("L", (S, S), 0)
    ImageDraw.Draw(teeth).ellipse(TEETH_BOX, fill=255)
    fd.bitmap((0, 0), ImageChops.multiply(teeth, mouth), fill=WHITE)
    tongue = Image.new("L", (S, S), 0)
    ImageDraw.Draw(tongue).ellipse(TONGUE_BOX, fill=255)
    fd.bitmap((0, 0), ImageChops.multiply(tongue, mouth), fill=TONGUE)

    # 5) 五官限制在身体内
    feat = Image.composite(feat, Image.new("RGBA", (S, S), (0, 0, 0, 0)), mask)
    body = Image.alpha_composite(body, feat)

    return body.resize((OUT, OUT), Image.LANCZOS)


def main() -> int:
    here = Path(__file__).resolve().parent
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else here / "奶龙-绘制版.png"
    img = build()
    img.quantize(colors=128, method=Image.FASTOCTREE).save(out, "PNG", optimize=True)
    print(f"saved {out} ({out.stat().st_size} bytes, {img.size[0]}x{img.size[1]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
