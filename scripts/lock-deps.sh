#!/usr/bin/env bash
# 依存をハッシュ付きでロックし直す (requirements*.txt を変えたら実行してコミットする)
#
#   requirements.lock     ← requirements.txt      (Docker・Linux の CLI)
#   requirements-dev.lock ← requirements-dev.txt  (CI のテスト・ビルド。GUI・PyInstaller を含む)
#
# --universal で Windows / macOS / Linux の依存 (keyring のバックエンド、pywin32-ctypes、
# pyobjc 等) とそれぞれの wheel のハッシュを 1 つのファイルに入れる。CI と Dockerfile は
# pip install --require-hashes で入れる。uv が要る (https://docs.astral.sh/uv/)。
#
#   scripts/lock-deps.sh            # 今の制約の範囲で版を固定し直す (既存のロックを尊重)
#   scripts/lock-deps.sh --upgrade  # 制約の範囲で最新に上げる (リリースノートを確認してから)

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
command -v uv >/dev/null || { echo "ERR uv が必要です" >&2; exit 1; }

for pair in "requirements.txt:requirements.lock" "requirements-dev.txt:requirements-dev.lock"; do
    src="${pair%%:*}"
    out="${pair#*:}"
    uv pip compile --universal --python-version 3.11 --generate-hashes --quiet "$@" "$src" -o "$out"
    echo "書きました: $out"
done
