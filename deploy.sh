#!/bin/bash
# swim-worker デプロイスクリプト
# NAS / Vultr / Oracle の3環境にWorkerを一括デプロイする
#
# 使い方:
#   ./deploy.sh          # 全環境にデプロイ
#   ./deploy.sh nas      # NASのみ
#   ./deploy.sh vultr    # Vultrのみ
#   ./deploy.sh oracle   # Oracleのみ

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_WORKER="$SCRIPT_DIR/swim_worker"
SRC_REQUIREMENTS="$SCRIPT_DIR/requirements.txt"
SRC_DOCKERFILE="$SCRIPT_DIR/Dockerfile"

# NAS
NAS_HOST="nas"
NAS_BASE="/share/ZFS18_DATA/ContainerData/swim-distributed"
NAS_DOCKER="/share/ZFS530_DATA/.qpkg/container-station/bin/docker"

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

deploy_nas() {
    echo -e "\n${YELLOW}[NAS]${NC} デプロイ開始"

    echo "  ファイルコピー..."
    rsync -az --delete "$SRC_WORKER/" "$NAS_HOST:$NAS_BASE/swim-worker/swim_worker/"
    scp -Oq "$SRC_REQUIREMENTS" "$NAS_HOST:$NAS_BASE/swim-worker/requirements.txt"
    scp -Oq "$SRC_DOCKERFILE" "$NAS_HOST:$NAS_BASE/swim-worker/Dockerfile"
    ok "ファイルコピー完了"

    echo "  Docker build..."
    ssh "$NAS_HOST" "DOCKER_CONFIG=/tmp/docker_config $NAS_DOCKER compose -f $NAS_BASE/docker-compose.yml build swim-worker" 2>&1 | tail -3
    ok "ビルド完了"

    echo "  コンテナ再起動..."
    ssh "$NAS_HOST" "DOCKER_CONFIG=/tmp/docker_config $NAS_DOCKER compose -f $NAS_BASE/docker-compose.yml up -d swim-worker" 2>&1
    ok "NAS デプロイ完了"
}

deploy_vultr() {
    echo -e "\n${YELLOW}[Vultr]${NC} デプロイ開始"

    echo "  ファイルコピー..."
    rsync -az --delete "$SRC_WORKER/" "$VULTR_HOST:$VULTR_BASE/swim_worker/"
    scp -q "$SRC_REQUIREMENTS" "$VULTR_HOST:$VULTR_BASE/requirements.txt"
    ok "ファイルコピー完了"

    echo "  サービス再起動 (sudoパスワードが求められます)..."
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
        deploy_nas
        deploy_oracle
        deploy_vultr  # Vultrは最後（sudo対話が必要）
        ;;
    nas)    deploy_nas ;;
    vultr)  deploy_vultr ;;
    oracle) deploy_oracle ;;
    *)
        echo "使い方: $0 [all|nas|vultr|oracle]"
        exit 1
        ;;
esac

echo -e "\n${GREEN}完了${NC}"
