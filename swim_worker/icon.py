"""SWIM Worker アイコン描画 (レーダー型)

トレイアイコン・ウィンドウタイトルバー・exe埋め込みアイコン (.ico/.icns) で
同じデザインを使い回すための共通モジュール。

状態色:
  green = 稼働中
  gray  = 停止中 / 非稼働
  red   = エラー
"""
import math

from PIL import Image, ImageDraw

COLORS = {
    "green": (0, 170, 80),
    "gray": (110, 110, 110),
    "red": (200, 40, 40),
}


def create_icon(color: str = "green", size: int = 64) -> Image.Image:
    """レーダー型アイコンを生成する。

    円背景 + 同心円2つ + 十字 + 右上方向へのスイープ線 + 中心点。
    """
    bg = COLORS.get(color, COLORS["gray"])
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = max(1, size // 16)
    draw.ellipse([pad, pad, size - 1 - pad, size - 1 - pad], fill=bg)
    cx = size / 2
    cy = size / 2
    white = (255, 255, 255, 255)
    line_w = max(1, size // 24)
    r1 = size * 0.40  # 外側円
    r2 = size * 0.20  # 内側円
    draw.ellipse([cx - r1, cy - r1, cx + r1, cy + r1], outline=white, width=line_w)
    draw.ellipse([cx - r2, cy - r2, cx + r2, cy + r2], outline=white, width=line_w)
    draw.line([(cx, cy - r1), (cx, cy + r1)], fill=white, width=line_w)
    draw.line([(cx - r1, cy), (cx + r1, cy)], fill=white, width=line_w)
    # スイープ線 (右上方向)
    angle = math.radians(-45)
    ex = cx + r1 * math.cos(angle)
    ey = cy + r1 * math.sin(angle)
    draw.line([(cx, cy), (ex, ey)], fill=white, width=line_w + 1)
    # 中心点
    dot = max(1, size // 16)
    draw.ellipse([cx - dot, cy - dot, cx + dot, cy + dot], fill=white)
    return img
