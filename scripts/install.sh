#!/usr/bin/env bash
# swim-worker Linux インストーラー
#
# 使い方 (推奨: スクリプトを確認してから実行):
#   curl -fsSL -o install.sh https://github.com/Meku-30/swim-worker/releases/latest/download/install.sh
#   less install.sh              # 中身を確認
#   sudo bash install.sh
#
# 非対話モード (管理者向け):
#   sudo REDIS_HOST=... REDIS_PASSWORD=... SWIM_USERNAME=... SWIM_PASSWORD=... WORKER_NAME=... \
#     bash install.sh
#
# やること:
#   - 対応アーキテクチャのバイナリを GitHub Releases からダウンロード
#   - SHA256SUMS で整合性検証
#   - 専用ユーザー swim-worker を作成 (システムアカウント、シェルなし)
#   - /opt/swim-worker/ に配置
#   - .env を対話生成 (環境変数が全て揃っていればスキップ)
#   - systemd unit 配置 + enable (起動は手動)
#
# 削除方法:
#   sudo systemctl disable --now swim-worker
#   sudo rm -rf /opt/swim-worker /etc/systemd/system/swim-worker.service
#   sudo userdel swim-worker

set -euo pipefail

REPO="Meku-30/swim-worker"
INSTALL_DIR="/opt/swim-worker"
SERVICE_USER="swim-worker"
SERVICE_FILE="/etc/systemd/system/swim-worker.service"
BASE_URL="https://github.com/${REPO}/releases/latest/download"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}==>${NC} $*"; }
warn() { echo -e "${YELLOW}!!${NC}  $*"; }
die()  { echo -e "${RED}ERR${NC} $*" >&2; exit 1; }

# --- 事前チェック ---
[[ $EUID -eq 0 ]] || die "root で実行してください (sudo bash install.sh)"

command -v systemctl >/dev/null || die "systemd が必要です"
command -v curl >/dev/null      || die "curl が必要です (apt install -y curl)"
command -v sha256sum >/dev/null || die "sha256sum が必要です"

# アーキテクチャ判定
ARCH_RAW=$(uname -m)
case "$ARCH_RAW" in
    x86_64|amd64)      ARCH="amd64" ;;
    aarch64|arm64)     ARCH="arm64" ;;
    *) die "未対応のアーキテクチャ: $ARCH_RAW (amd64 / arm64 のみサポート)" ;;
esac
BINARY_NAME="swim-worker-linux-${ARCH}"
log "アーキテクチャ: ${ARCH_RAW} → ${BINARY_NAME}"

# --- ダウンロード ---
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

log "バイナリをダウンロード中..."
curl -fsSL --proto '=https' --tlsv1.2 -o "${TMPDIR}/${BINARY_NAME}" \
    "${BASE_URL}/${BINARY_NAME}"

log "SHA256SUMS をダウンロード中..."
curl -fsSL --proto '=https' --tlsv1.2 -o "${TMPDIR}/SHA256SUMS" \
    "${BASE_URL}/SHA256SUMS"

log "systemd unit と .env.example をダウンロード中..."
curl -fsSL --proto '=https' --tlsv1.2 -o "${TMPDIR}/swim-worker.service" \
    "${BASE_URL}/swim-worker.service"
curl -fsSL --proto '=https' --tlsv1.2 -o "${TMPDIR}/.env.example" \
    "${BASE_URL}/.env.example"

# --- 整合性検証 ---
log "SHA256 を検証中..."
cd "$TMPDIR"
# SHA256SUMS から必要な行だけ抽出して検証 (他ファイルの欠落で失敗しないように)
for f in "${BINARY_NAME}" swim-worker.service .env.example; do
    expected=$(grep "  ${f}$" SHA256SUMS | awk '{print $1}')
    [[ -n "$expected" ]] || die "SHA256SUMS に ${f} のエントリがありません"
    actual=$(sha256sum "$f" | awk '{print $1}')
    if [[ "$expected" != "$actual" ]]; then
        die "SHA256 不一致: ${f} (expected=${expected}, actual=${actual})"
    fi
done
log "整合性 OK"
cd - >/dev/null

# --- 専用ユーザー作成 ---
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    log "ユーザー ${SERVICE_USER} を作成..."
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
else
    log "ユーザー ${SERVICE_USER} は既に存在します"
fi

# --- ディレクトリ配置 ---
# 既存サービスが稼働中なら一旦止める (バイナリ差し替えを安全に)
WAS_RUNNING=0
if systemctl is-active --quiet swim-worker.service 2>/dev/null; then
    log "稼働中の swim-worker を停止 (アップグレード)..."
    systemctl stop swim-worker.service
    WAS_RUNNING=1
fi

log "${INSTALL_DIR} にファイルを配置..."
mkdir -p "${INSTALL_DIR}/data"
install -m 0755 -o "$SERVICE_USER" -g "$SERVICE_USER" \
    "${TMPDIR}/${BINARY_NAME}" "${INSTALL_DIR}/swim-worker"
chown -R "$SERVICE_USER:$SERVICE_USER" "${INSTALL_DIR}"
chmod 0750 "${INSTALL_DIR}/data"

# --- .env 作成 ---
ENV_FILE="${INSTALL_DIR}/.env"
if [[ -f "$ENV_FILE" ]]; then
    warn ".env が既に存在します: ${ENV_FILE} (上書きしません)"
else
    if [[ -n "${REDIS_HOST:-}" && -n "${REDIS_PASSWORD:-}" \
          && -n "${SWIM_USERNAME:-}" && -n "${SWIM_PASSWORD:-}" \
          && -n "${WORKER_NAME:-}" ]]; then
        log "環境変数から .env を生成..."
    else
        log ".env を対話生成します (管理者から教えてもらった値を入力)"
        echo
        read -rp "Redis ホスト: " REDIS_HOST
        read -rp "Redis ポート [6380]: " REDIS_PORT
        REDIS_PORT=${REDIS_PORT:-6380}
        read -rsp "Redis パスワード: " REDIS_PASSWORD; echo
        read -rp "SWIM ユーザー名: " SWIM_USERNAME
        read -rsp "SWIM パスワード: " SWIM_PASSWORD; echo
        read -rp "Worker 名 (ローマ字、他Workerと重複不可): " WORKER_NAME
    fi
    REDIS_PORT=${REDIS_PORT:-6380}
    # umask を厳しくしてから生成 (race を避ける)
    umask 077
    cat > "$ENV_FILE" <<EOF
REDIS_HOST=${REDIS_HOST}
REDIS_PORT=${REDIS_PORT}
REDIS_PASSWORD=${REDIS_PASSWORD}
SWIM_USERNAME=${SWIM_USERNAME}
SWIM_PASSWORD=${SWIM_PASSWORD}
WORKER_NAME=${WORKER_NAME}
EOF
    umask 022
    chown "$SERVICE_USER:$SERVICE_USER" "$ENV_FILE"
    chmod 0600 "$ENV_FILE"
    log ".env を作成: ${ENV_FILE} (パーミッション 600)"
fi

# --- systemd unit 配置 ---
log "systemd unit を配置..."
install -m 0644 "${TMPDIR}/swim-worker.service" "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable swim-worker.service >/dev/null
log "サービスを有効化しました (自動起動)"

# アップグレード時は自動で再起動、新規インストールは手動起動を促す
if [[ $WAS_RUNNING -eq 1 ]]; then
    log "swim-worker を再起動..."
    systemctl start swim-worker.service
    sleep 2
    if systemctl is-active --quiet swim-worker.service; then
        log "swim-worker 稼働中"
    else
        warn "swim-worker が起動していません: sudo journalctl -u swim-worker -n 50 で確認してください"
    fi
fi

echo
log "インストール完了"
echo
if [[ $WAS_RUNNING -eq 0 ]]; then
    echo "  起動:   sudo systemctl start swim-worker"
fi
echo "  状態:   sudo systemctl status swim-worker"
echo "  ログ:   sudo journalctl -u swim-worker -f"
echo "  停止:   sudo systemctl stop swim-worker"
echo
if [[ $WAS_RUNNING -eq 0 ]]; then
    echo "起動したら管理者 (meku) に「起動しました」と連絡してください。"
fi
