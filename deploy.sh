#!/bin/bash
# swim-worker デプロイスクリプト (2026-07-20時点: 実際の本番デプロイ方式と乖離あり、要注意)
#
# 【警告】Oracle/VultrのVPSは現在 /opt/swim-worker/ にinstall.sh経由でバイナリ配置
# されており (systemd ExecStart=/opt/swim-worker/swim-worker)、このスクリプトが
# 前提とするソースチェックアウト (ORACLE_BASE=/home/ubuntu/swim-worker 等) への
# rsyncは実際の稼働プロセスに反映されない。
#
# 現在の正規デプロイ経路は「バージョンタグ push → CI release → 各Workerの
# swim-worker-update.timer/systemd経由の自動更新」。即時反映したい場合は
# `ssh <host> && sudo systemctl start swim-worker-update.service` を使うこと。
# 詳細: docs/architecture.md § 配布と自動更新、swim-coordinator/docs/admin-runbook.md § 1.4
#
# Vultr / Oracle の2環境にWorkerを一括デプロイする (レガシー、上記警告参照)
#
# 使い方:
#   ./deploy.sh          # 全環境にデプロイ
#   ./deploy.sh vultr    # Vultrのみ
#   ./deploy.sh oracle   # Oracleのみ

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_WORKER="$SCRIPT_DIR/swim_worker"
SRC_REQUIREMENTS="$SCRIPT_DIR/requirements.txt"
SRC_DOCKERFILE="$SCRIPT_DIR/Dockerfile"

# Vultr
VULTR_HOST="vultr"
VULTR_BASE="/home/meku/swim-worker"

# Oracle
ORACLE_HOST="oracle"
ORACLE_BASE="/home/ubuntu/swim-worker"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}OK${NC} $1"; }
warn() { echo -e "  ${YELLOW}!!${NC} $1"; }
fail() { echo -e "  ${RED}NG${NC} $1"; }

deploy_vultr() {
    echo -e "\n${YELLOW}[Vultr]${NC} デプロイ開始"

    echo "  ファイルコピー..."
    rsync -az --delete "$SRC_WORKER/" "$VULTR_HOST:$VULTR_BASE/swim_worker/"
    scp -q "$SRC_REQUIREMENTS" "$VULTR_HOST:$VULTR_BASE/requirements.txt"
    ok "ファイルコピー完了"

    echo "  依存パッケージ更新 (sudoパスワードが求められます)..."
    # Python 3.12+ は PEP 668 で外部環境を保護するため --break-system-packages が必要
    if ssh -t "$VULTR_HOST" "cd $VULTR_BASE && sudo pip3 install -r requirements.txt --break-system-packages -q"; then
        ok "依存パッケージ更新完了"
    else
        fail "pip install 失敗 — サービスは再起動しません"
        return 1
    fi

    echo "  サービス再起動..."
    ssh -t "$VULTR_HOST" 'sudo systemctl restart swim-worker'
    if ssh "$VULTR_HOST" 'systemctl is-active swim-worker' 2>/dev/null | grep -q active; then
        ok "Vultr デプロイ完了"
    else
        fail "swim-worker が active でない可能性あり — 確認してください"
    fi
}

deploy_oracle() {
    echo -e "\n${YELLOW}[Oracle]${NC} デプロイ開始"

    echo "  ファイルコピー..."
    rsync -az --delete "$SRC_WORKER/" "$ORACLE_HOST:$ORACLE_BASE/swim_worker/"
    scp -q "$SRC_REQUIREMENTS" "$ORACLE_HOST:$ORACLE_BASE/requirements.txt"
    ok "ファイルコピー完了"

    echo "  依存パッケージ更新..."
    if ssh -t "$ORACLE_HOST" "cd $ORACLE_BASE && sudo pip3 install -r requirements.txt --break-system-packages -q"; then
        ok "依存パッケージ更新完了"
    else
        fail "pip install 失敗 — サービスは再起動しません"
        return 1
    fi

    echo "  サービス再起動..."
    ssh "$ORACLE_HOST" 'sudo systemctl restart swim-worker'
    if ssh "$ORACLE_HOST" 'systemctl is-active swim-worker' 2>/dev/null | grep -q active; then
        ok "Oracle デプロイ完了"
    else
        fail "swim-worker が active でない可能性あり — 確認してください"
    fi
}

targets="${1:-all}"

echo "swim-worker デプロイ ($(git -C "$SCRIPT_DIR" describe --tags --always 2>/dev/null || echo 'unknown'))"

case "$targets" in
    all)
        deploy_oracle
        deploy_vultr  # Vultrは最後（sudo対話が必要）
        ;;
    vultr)  deploy_vultr ;;
    oracle) deploy_oracle ;;
    *)
        echo "使い方: $0 [all|vultr|oracle]"
        exit 1
        ;;
esac

echo -e "\n${GREEN}完了${NC}"
