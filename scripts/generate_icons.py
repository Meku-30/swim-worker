#!/usr/bin/env python3
"""swim_worker/resources/icon.ico と icon.icns を生成する。

PyInstaller の `--icon` で埋め込む用。PIL で swim_worker.icon.create_icon()
を使って描画するので、Python コードがアイコンの唯一のソース。

使い方:
  python scripts/generate_icons.py
"""
import sys
from pathlib import Path

# プロジェクトルートを sys.path に追加
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from swim_worker.icon import create_icon  # noqa: E402

RESOURCES = ROOT / "swim_worker" / "resources"
RESOURCES.mkdir(parents=True, exist_ok=True)


def main() -> None:
    # 最も高解像度な元画像 (1024px) を作り、そこから Pillow に縮小させる。
    # create_icon は size に応じて線幅・パディングを自動調整するが、低解像度で
    # 描いて拡大するとボケるので、各サイズで個別に描画してから Pillow に渡す。
    sizes_ico = [16, 24, 32, 48, 64, 128, 256]
    sizes_icns = [16, 32, 64, 128, 256, 512, 1024]

    # .ico 生成: Pillow は base image + sizes で複数解像度 ICO を生成できる
    base_ico = create_icon("green", 256)
    ico_path = RESOURCES / "icon.ico"
    base_ico.save(
        ico_path,
        format="ICO",
        sizes=[(s, s) for s in sizes_ico],
    )
    print(f"[OK] {ico_path}")

    # .icns 生成: Pillow は 1024px 以上のソースから自動で各サイズを生成する
    base_icns = create_icon("green", 1024)
    icns_path = RESOURCES / "icon.icns"
    try:
        base_icns.save(icns_path, format="ICNS")
        print(f"[OK] {icns_path}")
    except Exception as e:
        # 環境によっては ICNS サポートが制限される場合がある
        print(f"[WARN] .icns 生成失敗 (macOS ビルド時のみ必要): {e}")

    # デバッグ用 PNG (実寸確認用)
    for s in (16, 32, 64, 256):
        (RESOURCES / f"icon_{s}.png").unlink(missing_ok=True)
    png_path = RESOURCES / "icon_256.png"
    create_icon("green", 256).save(png_path, format="PNG")
    print(f"[OK] {png_path}")


if __name__ == "__main__":
    main()
