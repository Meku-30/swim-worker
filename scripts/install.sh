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
# 自動更新モード (systemd timer から呼ばれる):
#   sudo bash install.sh --auto
#
# やること:
#   - 対応アーキテクチャのバイナリを GitHub Releases からダウンロード
#   - SHA256SUMS で整合性検証
#   - 専用ユーザー swim-worker を作成 (システムアカウント、シェルなし)
#   - /opt/swim-worker/ に配置 + /opt/swim-worker/.version 書き込み
#   - .env を対話生成 (環境変数が全て揃っていればスキップ)
#   - systemd unit 配置 + enable (起動は手動)
#   - swim-worker-update.timer を配置 + enable (6h 毎の自動更新チェック)
#
# 削除方法:
#   sudo systemctl disable --now swim-worker swim-worker-update.timer
#   sudo rm -rf /opt/swim-worker /etc/systemd/system/swim-worker*.{service,timer}
#   sudo userdel swim-worker

set -euo pipefail

REPO="Meku-30/swim-worker"
INSTALL_DIR="/opt/swim-worker"
SERVICE_USER="swim-worker"
SERVICE_FILE="/etc/systemd/system/swim-worker.service"
UPDATE_SERVICE_FILE="/etc/systemd/system/swim-worker-update.service"
UPDATE_TIMER_FILE="/etc/systemd/system/swim-worker-update.timer"
VERSION_FILE="${INSTALL_DIR}/.version"
UPDATE_LOCK="/var/lock/swim-worker-update.lock"
BASE_URL="https://github.com/${REPO}/releases/latest/download"
API_URL="https://api.github.com/repos/${REPO}/releases/latest"

# --auto モード判定
AUTO_MODE=0
if [[ "${1:-}" == "--auto" ]]; then
    AUTO_MODE=1
fi

if [[ $AUTO_MODE -eq 0 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; NC=''
fi

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

# ========================================================================
# --auto モード: バージョン比較 + ガードチェック + 更新実行 + ロールバック
# ========================================================================
if [[ $AUTO_MODE -eq 1 ]]; then
    # 多重実行防止 (timer が前回の更新中に再発火するケース対策)
    exec 9>"$UPDATE_LOCK"
    if ! flock -n 9; then
        log "他の更新プロセスが実行中、スキップ"
        exit 0
    fi

    [[ -f "$VERSION_FILE" ]] || die ".version がありません。通常の install.sh を先に実行してください"
    CURRENT_VERSION=$(cat "$VERSION_FILE")
    [[ -n "$CURRENT_VERSION" ]] || die ".version が空です"

    # 最新バージョンを取得 (GitHub API は prerelease を除外する /releases/latest)
    LATEST_TAG=$(curl -fsSL --proto '=https' --tlsv1.2 "$API_URL" \
        | grep -oE '"tag_name"[[:space:]]*:[[:space:]]*"[^"]+"' \
        | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
    [[ -n "$LATEST_TAG" ]] || die "GitHub API から latest tag を取得できません"
    LATEST_VERSION="${LATEST_TAG#v}"

    if [[ "$CURRENT_VERSION" == "$LATEST_VERSION" ]]; then
        log "最新版です (v${CURRENT_VERSION})"
        exit 0
    fi

    # --- ガード1: ローカル opt-out ファイル ---
    if [[ -f "${INSTALL_DIR}/.no-auto-update" ]]; then
        log "自動更新が opt-out されています (${INSTALL_DIR}/.no-auto-update)"
        exit 0
    fi

    # --- ガード2: Redis kill switch + whitelist (staged rollout) ---
    # Worker バイナリに問い合わせる形にすると依存が増えるため、
    # 同梱の小さい Python で直接問い合わせる (バイナリ内 Python は使えないため
    # 専用 helper を置く。シンプルに curl_cffi ではなく標準 python + ssl を使う)
    if command -v python3 >/dev/null; then
        # .env から Redis 設定読み込み
        WORKER_NAME=$(grep -E "^WORKER_NAME=" "${INSTALL_DIR}/.env" | head -1 | sed "s/^WORKER_NAME=//; s/^'//; s/'$//")
        GUARD_RESULT=$(python3 - <<'PYEOF' 2>/dev/null || echo "ERROR"
import re, socket, ssl, sys

# .env を読み込み (install.sh が書いた単引用符形式)
env = {}
try:
    with open("/opt/swim-worker/.env") as f:
        for line in f:
            m = re.match(r"^([A-Z_]+)=(.*)$", line.strip())
            if not m:
                continue
            k, v = m.group(1), m.group(2)
            if (v.startswith("'") and v.endswith("'")) or (v.startswith('"') and v.endswith('"')):
                v = v[1:-1]
            env[k] = v
except Exception:
    print("ERROR:env-unreadable")
    sys.exit(0)

host = env.get("REDIS_HOST", "")
port = int(env.get("REDIS_PORT", "6380"))
password = env.get("REDIS_PASSWORD", "")
worker_name = env.get("WORKER_NAME", "")
if not host:
    print("ERROR:no-host")
    sys.exit(0)

# Redis TLS 接続 (kill switch 参照のみ。機密値は扱わないため CERT_NONE で許容)
try:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection((host, port), timeout=5)
    sock = ctx.wrap_socket(raw, server_hostname=host)
    sock.settimeout(5)

    def recv_until(delim):
        buf = b""
        while delim not in buf:
            ch = sock.recv(1)
            if not ch:
                return buf
            buf += ch
        return buf

    def read_resp():
        """RESP 単一応答 (simple/error/bulk) を読む。配列等は未対応。"""
        head = recv_until(b"\r\n")
        if not head:
            return None
        kind = head[:1]
        body = head[1:-2]
        if kind == b"+":
            return body.decode("utf-8", "replace")
        if kind == b"-":
            raise RuntimeError(body.decode("utf-8", "replace"))
        if kind == b"$":
            n = int(body)
            if n < 0:
                return None
            remaining = n + 2  # data + \r\n
            data = b""
            while len(data) < remaining:
                chunk = sock.recv(remaining - len(data))
                if not chunk:
                    break
                data += chunk
            return data[:-2].decode("utf-8", "replace")
        return None  # ints 等は今回不要

    def cmd(*args):
        buf = f"*{len(args)}\r\n".encode()
        for a in args:
            b = a.encode()
            buf += f"${len(b)}\r\n".encode() + b + b"\r\n"
        sock.sendall(buf)
        return read_resp()

    cmd("AUTH", password)   # +OK 期待 (失敗時は例外)
    enabled   = cmd("GET", "swim:auto_update_enabled")
    whitelist = cmd("GET", "swim:auto_update_whitelist")
    sock.close()

    if enabled != "true":
        print("DISABLED")
        sys.exit(0)
    if whitelist and whitelist.strip():
        allowed = [x.strip() for x in whitelist.split(",") if x.strip()]
        if worker_name not in allowed:
            print(f"NOT_IN_WHITELIST:{worker_name}")
            sys.exit(0)
    print("OK")
except Exception as e:
    print(f"ERROR:{type(e).__name__}:{e}")
    sys.exit(0)
PYEOF
)
        case "$GUARD_RESULT" in
            "DISABLED")
                log "Coordinator kill switch が OFF (swim:auto_update_enabled != 'true')、更新スキップ"
                exit 0
                ;;
            NOT_IN_WHITELIST:*)
                log "staged rollout whitelist に含まれていない Worker: ${WORKER_NAME}、更新スキップ"
                exit 0
                ;;
            ERROR:*)
                warn "Coordinator 疎通確認失敗 (${GUARD_RESULT#ERROR:})、安全側で更新スキップ"
                exit 0
                ;;
            "OK")
                log "ガード通過: 更新を開始します"
                ;;
            *)
                warn "予期しないガード応答 (${GUARD_RESULT})、更新スキップ"
                exit 0
                ;;
        esac
    else
        warn "python3 がないため Coordinator ガードをスキップ"
    fi

    # --- ガード3: メジャーバージョン変更は手動必須 ---
    CUR_MAJOR="${CURRENT_VERSION%%.*}"
    NEW_MAJOR="${LATEST_VERSION%%.*}"
    if [[ "$CUR_MAJOR" != "$NEW_MAJOR" ]]; then
        warn "メジャーバージョン変更 (${CUR_MAJOR}.x → ${NEW_MAJOR}.x)、自動更新をスキップ"
        warn "手動で sudo bash install.sh を実行してください"
        exit 0
    fi

    log "自動更新: v${CURRENT_VERSION} → v${LATEST_VERSION}"

    # --- 新バイナリ DL + 検証 ---
    TMPDIR=$(mktemp -d)
    trap 'rm -rf "$TMPDIR"' EXIT

    curl -fsSL --proto '=https' --tlsv1.2 -o "${TMPDIR}/${BINARY_NAME}" "${BASE_URL}/${BINARY_NAME}"
    curl -fsSL --proto '=https' --tlsv1.2 -o "${TMPDIR}/SHA256SUMS"      "${BASE_URL}/SHA256SUMS"

    expected=$(grep "  ${BINARY_NAME}$" "${TMPDIR}/SHA256SUMS" | awk '{print $1}')
    actual=$(sha256sum "${TMPDIR}/${BINARY_NAME}" | awk '{print $1}')
    if [[ -z "$expected" || "$expected" != "$actual" ]]; then
        die "SHA256 不一致: expected=${expected}, actual=${actual}"
    fi

    # --- 旧バイナリをバックアップ → 新バイナリ配置 → restart ---
    cp -p "${INSTALL_DIR}/swim-worker" "${INSTALL_DIR}/swim-worker.old"
    install -m 0755 -o "$SERVICE_USER" -g "$SERVICE_USER" \
        "${TMPDIR}/${BINARY_NAME}" "${INSTALL_DIR}/swim-worker"

    log "swim-worker を再起動..."
    systemctl restart swim-worker.service

    # --- ロールバック判定: 60秒待って is-active + NRestarts < 2 ---
    sleep 60
    IS_ACTIVE=$(systemctl is-active swim-worker.service 2>/dev/null || echo "inactive")
    N_RESTARTS=$(systemctl show --property=NRestarts --value swim-worker.service 2>/dev/null || echo "0")

    if [[ "$IS_ACTIVE" == "active" && "$N_RESTARTS" -lt 2 ]]; then
        log "自動更新成功 (is-active=active, NRestarts=${N_RESTARTS})"
        echo "$LATEST_VERSION" > "$VERSION_FILE"
        rm -f "${INSTALL_DIR}/swim-worker.old"
        exit 0
    fi

    # ロールバック
    warn "新版が不安定 (is-active=${IS_ACTIVE}, NRestarts=${N_RESTARTS})、ロールバック実行"
    install -m 0755 -o "$SERVICE_USER" -g "$SERVICE_USER" \
        "${INSTALL_DIR}/swim-worker.old" "${INSTALL_DIR}/swim-worker"
    systemctl restart swim-worker.service
    sleep 5
    if systemctl is-active --quiet swim-worker.service; then
        warn "ロールバック完了 (v${CURRENT_VERSION} に復帰)"
    else
        die "ロールバックも失敗。手動調査が必要 (journalctl -u swim-worker -n 100)"
    fi
    rm -f "${INSTALL_DIR}/swim-worker.old"
    exit 1
fi

# ========================================================================
# 通常モード: フルインストール (新規または手動アップグレード)
# ========================================================================
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

log "systemd unit / timer をダウンロード中..."
curl -fsSL --proto '=https' --tlsv1.2 -o "${TMPDIR}/swim-worker.service" \
    "${BASE_URL}/swim-worker.service"
curl -fsSL --proto '=https' --tlsv1.2 -o "${TMPDIR}/swim-worker-update.service" \
    "${BASE_URL}/swim-worker-update.service"
curl -fsSL --proto '=https' --tlsv1.2 -o "${TMPDIR}/swim-worker-update.timer" \
    "${BASE_URL}/swim-worker-update.timer"

# --- 整合性検証 ---
log "SHA256 を検証中..."
cd "$TMPDIR"
for f in "${BINARY_NAME}" swim-worker.service swim-worker-update.service swim-worker-update.timer; do
    expected=$(grep "  ${f}$" SHA256SUMS | awk '{print $1}')
    [[ -n "$expected" ]] || die "SHA256SUMS に ${f} のエントリがありません"
    actual=$(sha256sum "$f" | awk '{print $1}')
    if [[ "$expected" != "$actual" ]]; then
        die "SHA256 不一致: ${f} (expected=${expected}, actual=${actual})"
    fi
done
log "整合性 OK"
cd - >/dev/null

# --- 最新バージョン取得 (.version 書き込み用) ---
LATEST_TAG=$(curl -fsSL --proto '=https' --tlsv1.2 "$API_URL" \
    | grep -oE '"tag_name"[[:space:]]*:[[:space:]]*"[^"]+"' \
    | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
LATEST_VERSION="${LATEST_TAG#v}"

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

# バージョンファイル書き込み (自動更新時の比較対象)
if [[ -n "$LATEST_VERSION" ]]; then
    echo "$LATEST_VERSION" > "$VERSION_FILE"
    chown "$SERVICE_USER:$SERVICE_USER" "$VERSION_FILE"
    chmod 0644 "$VERSION_FILE"
fi

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
    for pair in "REDIS_HOST:${REDIS_HOST}" "REDIS_PASSWORD:${REDIS_PASSWORD}" \
                "SWIM_USERNAME:${SWIM_USERNAME}" "SWIM_PASSWORD:${SWIM_PASSWORD}" \
                "WORKER_NAME:${WORKER_NAME}"; do
        name="${pair%%:*}"
        val="${pair#*:}"
        case "$val" in
            *\'*|*$'\n'*)
                die "${name} に単引用符 ( ' ) や改行は含められません。別の値を使ってください。" ;;
        esac
    done

    umask 077
    cat > "$ENV_FILE" <<EOF
REDIS_HOST='${REDIS_HOST}'
REDIS_PORT=${REDIS_PORT}
REDIS_PASSWORD='${REDIS_PASSWORD}'
SWIM_USERNAME='${SWIM_USERNAME}'
SWIM_PASSWORD='${SWIM_PASSWORD}'
WORKER_NAME='${WORKER_NAME}'
EOF
    umask 022
    chown "$SERVICE_USER:$SERVICE_USER" "$ENV_FILE"
    chmod 0600 "$ENV_FILE"
    log ".env を作成: ${ENV_FILE} (パーミッション 600)"
fi

# --- systemd unit / update timer 配置 ---
log "systemd unit を配置..."
install -m 0644 "${TMPDIR}/swim-worker.service"        "$SERVICE_FILE"
install -m 0644 "${TMPDIR}/swim-worker-update.service" "$UPDATE_SERVICE_FILE"
install -m 0644 "${TMPDIR}/swim-worker-update.timer"   "$UPDATE_TIMER_FILE"
systemctl daemon-reload
systemctl enable swim-worker.service >/dev/null
systemctl enable --now swim-worker-update.timer >/dev/null
log "サービスと自動更新 timer を有効化しました"

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
echo "  自動更新 timer: sudo systemctl status swim-worker-update.timer"
echo "  自動更新を無効化: sudo touch ${INSTALL_DIR}/.no-auto-update"
echo
if [[ $WAS_RUNNING -eq 0 ]]; then
    echo "起動したら管理者 (meku) に「起動しました」と連絡してください。"
fi
