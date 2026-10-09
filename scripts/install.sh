#!/usr/bin/env bash
# swim-worker Linux インストーラー
#
# 使い方 (推奨: スクリプトを確認してから実行):
#   curl -fsSL -o install.sh https://github.com/Meku-30/swim-worker/releases/latest/download/install.sh
#   less install.sh              # 中身を確認
#   sudo bash install.sh
#
# 非対話モード (管理者向け):
#   sudo REDIS_HOST=... [REDIS_USERNAME=...] REDIS_PASSWORD=... SWIM_USERNAME=... SWIM_PASSWORD=... WORKER_NAME=... \
#     bash install.sh
#
# 特定バージョンをインストール (検証/手動ロールバック用):
#   sudo RELEASE_TAG=v1.0.0-rc1 bash install.sh
#   (通常は省略して最新 stable を使う)
#
# 自動更新モード (systemd timer から呼ばれる):
#   sudo bash install.sh --auto
#   (常に最新の stable を追従、prerelease は拾わない)
#
# やること:
#   - 最新 stable のタグを調べ、そのタグに固定して GitHub Releases からダウンロード
#   - SHA256SUMS で整合性検証
#   - 専用ユーザー swim-worker を作成 (システムアカウント、シェルなし)
#   - /opt/swim-worker/ に配置 + /opt/swim-worker/.version 書き込み
#     (/opt/swim-worker は root の持ち物。Worker が書けるのは data/ だけ、.env は読むだけ)
#   - .env を対話生成 (環境変数が全て揃っていればスキップ)
#   - systemd unit 配置 + enable (起動は手動)
#   - 名前解決のサーバー・Redis への通信を許可する drop-in を配置
#     (unit は LAN・リンクローカル宛ての通信を閉じているため)
#   - swim-worker-update.timer を配置 + enable (6h 毎の自動更新チェック)
#
# 削除方法:
#   sudo systemctl disable --now swim-worker swim-worker-update.timer
#   sudo rm -rf /opt/swim-worker /etc/systemd/system/swim-worker*.{service,timer} \
#     /etc/systemd/system/swim-worker.service.d
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

# Redis TLS 用 CA 証明書 (swim_worker/certs.py の CA_CERT_PEM と同一。公開鍵なので秘匿不要)。
# --auto の kill switch 確認で Redis に AUTH (パスワード送信) するため、
# 証明書検証なし (CERT_NONE) では MITM でパスワードを窃取されうる。必ず検証する。
# tests/test_install_sh.py が certs.py との一致を検証している。
read -r -d '' REDIS_CA_PEM <<'CAEOF' || true
-----BEGIN CERTIFICATE-----
MIIFETCCAvmgAwIBAgIUa37Dpr6cCRedc6aFqB0Lxita18UwDQYJKoZIhvcNAQEL
BQAwGDEWMBQGA1UEAwwNc3dpbS1yZWRpcy1jYTAeFw0yNjAzMzEyMTA2MzdaFw0z
NjAzMjgyMTA2MzdaMBgxFjAUBgNVBAMMDXN3aW0tcmVkaXMtY2EwggIiMA0GCSqG
SIb3DQEBAQUAA4ICDwAwggIKAoICAQCRhkoOXWg0ewc/HFxp59EO1nws/g6x+czH
Vbclrwiu5rty1AYcZs7OggqDAi+Uju7eJTvQxhWE2uOk3yYYWT3VcJsD3nblZAuA
i6gi6rIOM47fweVyUAyuFRdGibCTqqvvRye5SQxG6QJa4PZTl/GeAz90MqThES50
jkhSe2esA5TRGNTJI8yshD/JVjCRdu6sPuzK1X9LwDcAJqKCTrPtnAxU0j53ub/r
8gORWwgxFhiY8eRK5TMmENeqcplntx69DC4RenxqnxA8vaF3R40Vsqmufpfvvxph
KlEtzWXCeXznnOTkTnVejVir0gvzQjETcnXp4oQJyEgvBv6DGGJojlbWhlcwMpnY
aLsE74Uq+nS27vlvH1UZlyc++TACqbCvYm9bwVJkUeVcMJRqp3zzXHXmTDqWB7YY
CbPQNuXIwjEiTpEm3SykqaFQhlxEFjpB0u6rQGfWEwB8pF5SiYOruam0rz8x2M6i
jQ9KcOOeo8eKuV1UDwM7P9bCt0EMr3Vd51ttancdWk+GG4YmSf1gHZwmbJuJpBCB
YAiHFptliSRT0IvQ0haILvCz7Fc06g5YSFYGtcFP4UBdbh2yTnsrw78qHnjeb9vi
BszxexdElFAk5xaG1WKl0VYs1FrWdVcngi2BRkS/zSCjILOeBfSxLBGgEGgFv8Gd
GUzuycDUnwIDAQABo1MwUTAdBgNVHQ4EFgQUngQHZ7JSi0eCv0gsvpKwu9p332Qw
HwYDVR0jBBgwFoAUngQHZ7JSi0eCv0gsvpKwu9p332QwDwYDVR0TAQH/BAUwAwEB
/zANBgkqhkiG9w0BAQsFAAOCAgEAEFRbB+Pe1CGzR1kNNgpw2j/OOitB5hm03GhH
W6as1nEaizQxGX+GV5N70yvLYef+ig43iSq7ved04/mCQONCnMD3Og0OGExmOOJ/
ffs0m8c5jLo3Zlvesk2O5iyQPqvYUYT2DnZvZTKc0MW+ab4vsIonpe2GlWZm2kOq
7ryXA+xjuZNXJVeEj9XWnQ6ZxFdv1U2S7c44mGETk571At6qasa24DONNwC/9omB
6cvdm1b28+sxVVZgFC4oZYQIKX0k9emGONcE47R7NKi3ku63vpzqV+uh6+94yzSt
a8QzXEsp2W4bVdEJc6asAI6ATLVn2ULTWdcuJHURLeHj+hcR7N240Z0uMSKqriGx
nnDgs6iUFEq3EGsxl90HqYO0KfabH8mFXVd2sXLBKJxb8Bq6+OcA9cFfgRaFxmrP
y0sZG+mz7jURCrpoijb5qqMGKMwL5b82A9A8BNbuysoo5bICY50PVG4zwOvB6kBT
Wzt7/195TDXVypH9M7DDnMD0XPrsrxQ1ce9Eg7jWdhMe7dPzz0lm9kJefFPyLgiO
HY7SsUQX4NBMjof3S6Cg7+bzJtjiQazJNuJULHslGWd9gbwd9X3k0UGjgeMOMESj
bmKpFYjCVWnobdviueeior9ma52p387KUSydPkArU3gY0UTVBNG/yk/1x1351Ql7
U2NY60E=
-----END CERTIFICATE-----
CAEOF

# 通常モード: RELEASE_TAG 環境変数で特定バージョンを強制可能 (検証/手動ロールバック用)。
# 未設定なら GitHub API で最新 stable のタグを調べ、そのタグに固定してダウンロードする。
# (releases/latest/download を使うと、タグを調べてからダウンロードするまでの間に
#  別の版が公開された場合に、.version と中身がずれる)
# --auto モードは常に最新 stable (prerelease は拾わない)。
RELEASE_TAG="${RELEASE_TAG:-}"
LATEST_API_URL="https://api.github.com/repos/${REPO}/releases/latest"
DOWNLOAD_BASE="https://github.com/${REPO}/releases/download"
# Worker が Redis に接続して登録まで済んだら書く起動成功マーカー (swim_worker/paths.py)
STARTUP_MARKER="${INSTALL_DIR}/data/.startup_ok"
# 更新後、新しい版が起動成功マーカーを書くまで待つ秒数
STARTUP_WAIT=120
DROPIN_DIR="/etc/systemd/system/swim-worker.service.d"
DROPIN_FILE="${DROPIN_DIR}/10-ip-allow.conf"

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

# --- ヘルパー関数 ---

# GitHub API から tag_name を取得 (python3 の json.load で確実にパース)
# 使い方: tag=$(fetch_latest_tag "$API_URL")
fetch_latest_tag() {
    local url="$1"
    curl -fsSL --proto '=https' --tlsv1.2 "$url" \
        | python3 -c 'import json, sys; d = json.load(sys.stdin); print(d.get("tag_name", ""))' \
        2>/dev/null || true
}

# タグの形式を確認する
#   strict: vX.Y.Z だけ (最新 stable・自動更新)
#   manual: RELEASE_TAG 用。vX.Y.Z-rc1 のような prerelease も可
# 使い方: validate_tag <tag> <strict|manual>
validate_tag() {
    local tag="$1" mode="${2:-strict}"
    if [[ "$mode" == "strict" ]]; then
        [[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]
    else
        [[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]]
    fi
}

# 起動成功マーカーを待つ (Worker は Redis に接続して登録まで済んだら data/.startup_ok を書く)。
# 再起動の前にマーカーを消しておくこと
# 使い方: wait_for_startup <秒>
wait_for_startup() {
    local limit="$1" waited=0
    while (( waited < limit )); do
        if [[ -f "$STARTUP_MARKER" ]] && systemctl is-active --quiet swim-worker.service; then
            return 0
        fi
        sleep 5
        waited=$((waited + 5))
    done
    return 1
}

# /opt/swim-worker は root の持ち物、Worker (サービスユーザー) が書けるのは data/ だけ。
# .env は root:swim-worker 0640 (Worker は設定を読むだけ)。
# 以前の install.sh は全体をサービスユーザーの持ち物にしていたので、更新 (--auto) のたびにも直す。
# シンボリックリンクは先を変えない (chown -h)。.env・data/ がリンクなら止める
fix_permissions() {
    local f
    if [[ -L "$INSTALL_DIR" || -L "${INSTALL_DIR}/data" || -L "${INSTALL_DIR}/.env" ]]; then
        die "${INSTALL_DIR} (data/ または .env) がシンボリックリンクです。確認してください"
    fi
    mkdir -p "${INSTALL_DIR}/data"
    chown root:root "$INSTALL_DIR"
    chmod 0755 "$INSTALL_DIR"
    for f in "$INSTALL_DIR"/* "$INSTALL_DIR"/.[!.]*; do
        if [[ ! -e "$f" && ! -L "$f" ]] || [[ "$f" == "${INSTALL_DIR}/data" ]]; then
            continue
        fi
        chown -h root:root "$f"
    done
    chown -hR "$SERVICE_USER:$SERVICE_USER" "${INSTALL_DIR}/data"
    chmod 0750 "${INSTALL_DIR}/data"
    if [[ -f "${INSTALL_DIR}/.env" ]]; then
        chown root:"$SERVICE_USER" "${INSTALL_DIR}/.env"
        chmod 0640 "${INSTALL_DIR}/.env"
    fi
}

# swim-worker.service は LAN・リンクローカル・CGNAT 宛ての通信を閉じている (IPAddressDeny)。
# 名前解決のサーバー (家庭のルーター・クラウドの 169.254.169.254 等) と Redis は
# その範囲にあっても通す drop-in の中身を出力する。ループバックはもともと閉じていない
# 使い方: ip_allow_dropin_content <resolv.conf> <REDIS_HOST>
ip_allow_dropin_content() {
    local resolv="$1" redis_host="$2" key ip rest
    local -a ips=()
    if [[ -r "$resolv" ]]; then
        while read -r key ip rest; do
            [[ "$key" == "nameserver" && -n "${ip:-}" ]] || continue
            ips+=("${ip%%\%*}")   # fe80::1%eth0 → fe80::1
        done < "$resolv"
    fi
    if [[ -n "$redis_host" ]]; then
        if [[ "$redis_host" =~ ^[0-9a-fA-F:.]+$ ]]; then
            ips+=("$redis_host")
        elif command -v getent >/dev/null; then
            while read -r ip rest; do
                ips+=("$ip")
            done < <(getent ahosts "$redis_host" 2>/dev/null || true)
        fi
    fi
    echo "# install.sh が生成 (名前解決のサーバー・Redis への通信を IPAddressDeny の例外にする)"
    echo "[Service]"
    if (( ${#ips[@]} > 0 )); then
        printf '%s\n' "${ips[@]}" | sort -u | while read -r ip; do
            [[ "$ip" =~ ^[0-9a-fA-F:.]+$ ]] || continue
            case "$ip" in 127.*|::1) continue ;; esac
            echo "IPAddressAllow=${ip}"
        done
    fi
}

# 上の drop-in を /etc/systemd/system/swim-worker.service.d/ に置く (反映は daemon-reload 後)
write_ip_allow_dropin() {
    local redis_host="" tmp
    if [[ -r "${INSTALL_DIR}/.env" ]]; then
        redis_host=$(grep -E "^REDIS_HOST=" "${INSTALL_DIR}/.env" 2>/dev/null | head -1 \
            | sed "s/^REDIS_HOST=//; s/^'//; s/'$//; s/^\"//; s/\"$//" || true)
    fi
    mkdir -p "$DROPIN_DIR"
    tmp=$(mktemp)
    ip_allow_dropin_content /etc/resolv.conf "$redis_host" > "$tmp"
    install -m 0644 -o root -g root "$tmp" "$DROPIN_FILE"
    rm -f "$tmp"
}

# root が ${INSTALL_DIR} 直下に小さいファイルを書く (既存がリンクでも先を書き換えないよう消してから)
# 使い方: write_root_file <path> <内容>
write_root_file() {
    rm -f "$1"
    printf '%s\n' "$2" > "$1"
    chmod 0644 "$1"
}

# 指定したファイル群を DL して SHA256SUMS で整合性検証する
# 使い方: download_and_verify <base_url> <tmpdir> <file1> [<file2> ...]
download_and_verify() {
    local base_url="$1"
    local tmpdir="$2"
    shift 2
    local f expected actual

    # 本体ファイル群 + SHA256SUMS を DL
    for f in "$@" SHA256SUMS; do
        curl -fsSL --proto '=https' --tlsv1.2 \
            -o "${tmpdir}/${f}" "${base_url}/${f}" \
            || die "ダウンロード失敗: ${f}"
    done

    # SHA256 照合
    for f in "$@"; do
        expected=$(grep "  ${f}$" "${tmpdir}/SHA256SUMS" | awk '{print $1}')
        [[ -n "$expected" ]] || die "SHA256SUMS に ${f} のエントリがありません"
        actual=$(sha256sum "${tmpdir}/${f}" | awk '{print $1}')
        if [[ "$expected" != "$actual" ]]; then
            die "SHA256 不一致: ${f} (expected=${expected}, actual=${actual})"
        fi
    done
}

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

    # 最新バージョンを取得 (--auto は常に最新 stable)。以後のダウンロードはこのタグに固定
    LATEST_TAG=$(fetch_latest_tag "$LATEST_API_URL")
    validate_tag "$LATEST_TAG" strict || die "GitHub API から最新のタグを取得できません (${LATEST_TAG:-空})"
    LATEST_VERSION="${LATEST_TAG#v}"

    if [[ "$CURRENT_VERSION" == "$LATEST_VERSION" ]]; then
        log "最新版です (v${CURRENT_VERSION})"
        exit 0
    fi

    # ダウングレード防止: 現行 > latest なら skip (prerelease 検証中等の保護)
    NEWER=$(printf '%s\n%s\n' "$CURRENT_VERSION" "$LATEST_VERSION" | sort -V | tail -1)
    if [[ "$NEWER" == "$CURRENT_VERSION" && "$CURRENT_VERSION" != "$LATEST_VERSION" ]]; then
        log "現行 (v${CURRENT_VERSION}) が latest (v${LATEST_VERSION}) より新しい、skip (prerelease 検証中等)"
        exit 0
    fi

    # --- ガード0: 前回この版でロールバックしていれば再試行しない (手動 install.sh で解除) ---
    FAILED_VERSION_FILE="${INSTALL_DIR}/.failed-version"
    if [[ -f "$FAILED_VERSION_FILE" ]]; then
        FAILED_VERSION=$(cat "$FAILED_VERSION_FILE")
        if [[ "$FAILED_VERSION" == "$LATEST_VERSION" ]]; then
            log "v${LATEST_VERSION} は前回ロールバックした版のため skip (手動で install.sh を実行すると解除)"
            exit 0
        fi
        rm -f "$FAILED_VERSION_FILE"   # より新しい版が出たので解除
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
    AUTH_BROKEN=0
    if command -v python3 >/dev/null; then
        # .env から WORKER_NAME 抽出 (log 表示用。Python helper は独自に .env 全体を読む)
        WORKER_NAME=""
        if [[ -r "${INSTALL_DIR}/.env" ]]; then
            WORKER_NAME=$(grep -E "^WORKER_NAME=" "${INSTALL_DIR}/.env" 2>/dev/null \
                | head -1 | sed "s/^WORKER_NAME=//; s/^'//; s/'$//" || true)
        fi
        GUARD_RESULT=$(INSTALL_DIR="$INSTALL_DIR" SWIM_REDIS_CA_PEM="$REDIS_CA_PEM" python3 - <<'PYEOF' 2>/dev/null || echo "ERROR"
import os, re, socket, ssl, sys

# .env を読み込み (install.sh が書いた単引用符形式)
env = {}
try:
    env_path = os.path.join(os.environ.get("INSTALL_DIR", "/opt/swim-worker"), ".env")
    with open(env_path) as f:
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
username = env.get("REDIS_USERNAME", "")  # ACL のワーカー用ユーザー (空なら default)
password = env.get("REDIS_PASSWORD", "")
worker_name = env.get("WORKER_NAME", "")
if not host:
    print("ERROR:no-host")
    sys.exit(0)

# Redis TLS 接続。AUTH でパスワードを送るため、埋め込み CA でサーバー証明書を
# 検証する (Worker 本体 redis-py と同じ CA・同じ hostname/IP SAN 照合)。
ca_pem = os.environ.get("SWIM_REDIS_CA_PEM", "")
if not ca_pem.strip():
    print("ERROR:no-ca-pem")
    sys.exit(0)
try:
    ctx = ssl.create_default_context(cadata=ca_pem)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
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

    class RedisError(Exception):
        """Redis -ERR 応答 (AUTH 失敗、コマンドエラー等)"""

    def read_resp():
        """RESP 単一応答 (simple string / error / bulk string) を読む。"""
        head = recv_until(b"\r\n")
        if not head:
            return None
        kind = head[:1]
        body = head[1:-2]
        if kind == b"+":
            return body.decode("utf-8", "replace")
        if kind == b"-":
            # -ERR / -WRONGPASS / -NOAUTH 等の Redis エラー応答
            raise RedisError(body.decode("utf-8", "replace"))
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
        # 整数 (":") 等は現状不要。未知の応答は None
        return None

    def cmd(*args):
        buf = f"*{len(args)}\r\n".encode()
        for a in args:
            b = a.encode()
            buf += f"${len(b)}\r\n".encode() + b + b"\r\n"
        sock.sendall(buf)
        return read_resp()

    # 認証の失敗・権限不足 (パスワードの作り直し・入力ミス・古い .env) は AUTH_FALLBACK:
    # Redis の一時停止・段階配布の設定を読めない = Coordinator にもつなげず働けていない Worker
    # なので、自動更新を止めずに GitHub の最新版で更新する (直った版が来れば復帰できる)
    try:
        if username:
            cmd("AUTH", username, password)
        else:
            cmd("AUTH", password)
    except RedisError as e:
        print(f"AUTH_FALLBACK:{e}")
        sys.exit(0)

    try:
        enabled   = cmd("GET", "swim:auto_update_enabled")
        whitelist = cmd("GET", "swim:auto_update_whitelist")
    except RedisError as e:
        if str(e).startswith(("NOPERM", "NOAUTH")):
            print(f"AUTH_FALLBACK:{e}")
            sys.exit(0)
        raise
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
            AUTH_FALLBACK:*)
                warn "Redis 認証失敗 (${GUARD_RESULT#AUTH_FALLBACK:}) — .env の REDIS_USERNAME / REDIS_PASSWORD を確認。"
                warn "  Coordinator の一時停止・段階配布の設定を読めないため、GitHub の最新版で更新を続けます"
                AUTH_BROKEN=1
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
        # python3 がないと kill switch / whitelist を確認できない。
        # 安全側 (fail-closed) で更新自体をスキップする。Pi OS / Ubuntu / Debian は
        # 標準で python3 同梱なので通常このパスには入らない。
        warn "python3 がないため Coordinator ガードを確認できません、安全側で更新スキップ"
        exit 0
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

    # --- 新バイナリ DL + 検証 (調べたタグに固定) ---
    TMPDIR=$(mktemp -d)
    trap 'rm -rf "$TMPDIR"' EXIT
    download_and_verify "${DOWNLOAD_BASE}/${LATEST_TAG}" "$TMPDIR" "$BINARY_NAME"

    # --- 旧バイナリをバックアップ → 新バイナリ配置 → restart ---
    rm -f "${INSTALL_DIR}/swim-worker.old"
    cp -p "${INSTALL_DIR}/swim-worker" "${INSTALL_DIR}/swim-worker.old"
    install -m 0755 -o root -g root "${TMPDIR}/${BINARY_NAME}" "${INSTALL_DIR}/swim-worker"
    fix_permissions
    write_ip_allow_dropin
    systemctl daemon-reload

    log "swim-worker を再起動..."
    rm -f "$STARTUP_MARKER"
    systemctl restart swim-worker.service

    # --- ロールバック判定 ---
    # 新しい版が起動成功マーカーを書く (= Redis に接続して登録まで済んだ) のを待つ。
    # Redis の認証に失敗している Worker (AUTH_FALLBACK) はマーカーを書けないので、
    # 60 秒後に落ちずに動いていれば成功とする
    if [[ $AUTH_BROKEN -eq 1 ]]; then
        sleep 60
        if systemctl is-active --quiet swim-worker.service; then
            log "自動更新完了 (Redis の認証に失敗しているため、起動の確認はプロセスの稼働のみ)"
            write_root_file "$VERSION_FILE" "$LATEST_VERSION"
            rm -f "${INSTALL_DIR}/swim-worker.old"
            exit 0
        fi
    elif wait_for_startup "$STARTUP_WAIT"; then
        log "自動更新成功 (起動を確認)"
        write_root_file "$VERSION_FILE" "$LATEST_VERSION"
        rm -f "${INSTALL_DIR}/swim-worker.old"
        exit 0
    fi

    # ロールバック
    warn "新版 (v${LATEST_VERSION}) の起動を確認できない、ロールバック実行"
    install -m 0755 -o root -g root \
        "${INSTALL_DIR}/swim-worker.old" "${INSTALL_DIR}/swim-worker"
    rm -f "$STARTUP_MARKER"
    systemctl restart swim-worker.service
    if [[ $AUTH_BROKEN -eq 0 ]] && ! wait_for_startup "$STARTUP_WAIT"; then
        # 旧版に戻しても起動しない = 版ではなく環境 (Redis・ネットワークの停止など) の問題。
        # .failed-version は書かず、次回の自動更新で同じ版をもう一度試す
        warn "旧版 (v${CURRENT_VERSION}) に戻したが起動を確認できない。Redis・ネットワークを確認してください"
        warn "  (journalctl -u swim-worker -n 100)。次回の自動更新で v${LATEST_VERSION} を再試行します"
        rm -f "${INSTALL_DIR}/swim-worker.old"
        exit 1
    fi
    warn "ロールバック完了 (v${CURRENT_VERSION} に復帰)"
    write_root_file "${INSTALL_DIR}/.failed-version" "$LATEST_VERSION"
    rm -f "${INSTALL_DIR}/swim-worker.old"
    exit 1
fi

# ========================================================================
# 通常モード: フルインストール (新規または手動アップグレード)
# ========================================================================
log "アーキテクチャ: ${ARCH_RAW} → ${BINARY_NAME}"

# --- 入れる版を決める (タグに固定してダウンロードする) ---
if [[ -n "$RELEASE_TAG" ]]; then
    validate_tag "$RELEASE_TAG" manual || die "RELEASE_TAG の形式が不正です: ${RELEASE_TAG} (例: v1.2.3)"
    TAG="$RELEASE_TAG"
else
    LATEST_TAG=$(fetch_latest_tag "$LATEST_API_URL")
    validate_tag "$LATEST_TAG" strict || die "GitHub API から最新のタグを取得できません (${LATEST_TAG:-空})"
    TAG="$LATEST_TAG"
fi
VERSION="${TAG#v}"
log "バージョン: ${TAG}"

# --- ダウンロード + 整合性検証 (ヘルパー関数で一括処理) ---
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

log "バイナリ / systemd unit / timer をダウンロード & SHA256 検証..."
download_and_verify "${DOWNLOAD_BASE}/${TAG}" "$TMPDIR" \
    "$BINARY_NAME" \
    swim-worker.service \
    swim-worker-update.service \
    swim-worker-update.timer
log "整合性 OK"

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
install -m 0755 -o root -g root "${TMPDIR}/${BINARY_NAME}" "${INSTALL_DIR}/swim-worker"

# バージョンファイル書き込み (自動更新時の比較対象。ダウンロードしたタグと同じ)
write_root_file "$VERSION_FILE" "$VERSION"
rm -f "${INSTALL_DIR}/.failed-version"   # 手動インストール/アップグレードでロールバック済み扱いを解除

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
        read -rp "Redis ユーザー名 (管理者から指定がなければ空欄): " REDIS_USERNAME
        read -rsp "Redis パスワード: " REDIS_PASSWORD; echo
        read -rp "SWIM ユーザー名: " SWIM_USERNAME
        read -rsp "SWIM パスワード: " SWIM_PASSWORD; echo
        read -rp "Worker 名 (ローマ字、他Workerと重複不可): " WORKER_NAME
    fi
    REDIS_PORT=${REDIS_PORT:-6380}
    REDIS_USERNAME=${REDIS_USERNAME:-}
    for pair in "REDIS_HOST:${REDIS_HOST}" "REDIS_USERNAME:${REDIS_USERNAME}" "REDIS_PASSWORD:${REDIS_PASSWORD}" \
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
REDIS_USERNAME='${REDIS_USERNAME}'
REDIS_PASSWORD='${REDIS_PASSWORD}'
SWIM_USERNAME='${SWIM_USERNAME}'
SWIM_PASSWORD='${SWIM_PASSWORD}'
WORKER_NAME='${WORKER_NAME}'
EOF
    umask 022
    log ".env を作成: ${ENV_FILE} (root:${SERVICE_USER} 640、Worker は読むだけ)"
fi

# 所有者を整える (/opt/swim-worker は root、data/ だけサービスユーザー、.env は 640)
fix_permissions

# --- systemd unit / update timer 配置 ---
log "systemd unit を配置..."
install -m 0644 "${TMPDIR}/swim-worker.service"        "$SERVICE_FILE"
install -m 0644 "${TMPDIR}/swim-worker-update.service" "$UPDATE_SERVICE_FILE"
install -m 0644 "${TMPDIR}/swim-worker-update.timer"   "$UPDATE_TIMER_FILE"
write_ip_allow_dropin
log "通信の許可 (名前解決・Redis) を ${DROPIN_FILE} に書きました"
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
