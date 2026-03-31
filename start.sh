#!/usr/bin/env bash
# SWIM Worker 起動スクリプト (Mac/Linux)
set -e

echo "=========================================="
echo "  SWIM Worker"
echo "=========================================="
echo ""

if [ ! -f ".env" ]; then
    echo "[エラー] .env ファイルが見つかりません。"
    echo ".env.example をコピーして .env を作成し、設定を記入してください。"
    exit 1
fi

if [ ! -f "ca.crt" ]; then
    echo "[エラー] ca.crt ファイルが見つかりません。"
    echo "管理者から ca.crt を入手して同じフォルダに配置してください。"
    exit 1
fi

# 実行ファイルを自動検出
if [ -f "./swim-worker-linux" ]; then
    EXEC="./swim-worker-linux"
elif [ -f "./swim-worker-macos" ]; then
    EXEC="./swim-worker-macos"
else
    echo "[エラー] swim-worker-linux または swim-worker-macos が見つかりません。"
    echo "Releases ページからダウンロードしてください。"
    exit 1
fi

chmod +x "$EXEC"
echo "起動中... (停止: Ctrl+C)"
echo ""
exec "$EXEC"
