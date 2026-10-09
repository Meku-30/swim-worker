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
# 自動更新 (--auto):
#   ふだんは固定の更新スクリプト /usr/local/libexec/swim-worker/update.sh
#   (swim-worker-update.service が実行) が、一時停止・段階配布・メジャー版を確かめ、
#   署名を確かめたこの install.sh を SWIM_UPDATE_TAG=<タグ> 付きの --auto で実行する。
#   バイナリを替える前に unit・drop-in・更新スクリプト・update.service・timer を <名前>.old に退避し、
#   起動を確かめられなければバイナリ・.version と一緒に戻す (ロールバック)。
#   SWIM_UPDATE_TAG なしの --auto (旧 update.service が最新の install.sh を取って実行する場合・
#   手で sudo bash install.sh --auto した場合) は旧方式からの移行: 署名を確かめた新しい更新スクリプトの
#   --guard-only で一時停止・段階配布を確かめ、通れば更新スクリプト・update.service・timer だけを
#   置いて終わる (本体の unit・バイナリは次の timer の更新で drop-in と一緒に入る)。opt-out の機も移す
#
# やること:
#   - 最新 stable のタグを調べ、そのタグに固定して GitHub Releases からダウンロード
#   - SHA256SUMS の署名 (SHA256SUMS.sig、Ed25519) を埋め込みの公開鍵で検証し、
#     各ファイルのハッシュを SHA256SUMS で検証 (署名のないリリースは入れない。OpenSSL 3.0 以上が必要)
#   - 専用ユーザー swim-worker を作成 (システムアカウント、シェルなし)
#   - /opt/swim-worker/ に配置 + /opt/swim-worker/.version 書き込み
#     (/opt/swim-worker は root の持ち物。Worker が書けるのは data/ だけ、.env は読むだけ)
#   - .env を対話生成 (環境変数が全て揃っていればスキップ)
#   - systemd unit 配置 + enable (起動は手動)
#   - 名前解決のサーバー・Redis への通信を許可する drop-in を配置
#     (unit は LAN・リンクローカル宛ての通信を閉じているため)
#   - 固定の更新スクリプトを /usr/local/libexec/swim-worker/update.sh に配置
#   - swim-worker-update.timer を配置 + enable (6h 毎の自動更新チェック)
#
# 削除方法:
#   sudo systemctl disable --now swim-worker swim-worker-update.timer
#   sudo rm -rf /opt/swim-worker /etc/systemd/system/swim-worker*.{service,timer} \
#     /etc/systemd/system/swim-worker.service.d /usr/local/libexec/swim-worker
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
# 固定の更新スクリプト (swim-worker-update.service が実行する。root:root 0755)
UPDATER_DIR="/usr/local/libexec/swim-worker"
UPDATER_PATH="${UPDATER_DIR}/update.sh"

# リリース (SHA256SUMS) の署名を確かめる Ed25519 公開鍵 (ふだんは 1 本、鍵の入れ替え中だけ 2 本まで)。
# scripts/set-release-pubkeys.sh が scripts/release_pubkeys/*.pub.pem から書く。手で編集しない。
# swim-worker-update.sh・swim_worker/release_keys.py と同じであることを tests/test_release_pubkeys.py が確かめる
# BEGIN RELEASE PUBKEYS
read -r -d '' RELEASE_PUBKEYS_PEM <<'PUBKEYEOF' || true
-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAZ+6EShmEUwozcwVIWHNfkMdw4JvziE15STc5ZJ7yuLk=
-----END PUBLIC KEY-----
PUBKEYEOF
# END RELEASE PUBKEYS

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

# `openssl version` の出力が OpenSSL 3.0 以上か (Ed25519 で SHA256SUMS をそのまま確かめる
# `pkeyutl -rawin` は 3.0 から。1.1.1 には無い)
# 使い方: openssl_version_ok "OpenSSL 3.0.13 30 Jan 2024"
openssl_version_ok() {
    local v="$1"
    [[ "$v" =~ ^OpenSSL\ ([0-9]+)\.([0-9]+)\.([0-9]+) ]] || return 1
    (( BASH_REMATCH[1] >= 3 ))
}

require_openssl() {
    command -v openssl >/dev/null || die "openssl が必要です (apt install -y openssl)。更新の署名を確かめるのに使います"
    local v
    v=$(openssl version 2>/dev/null || true)
    openssl_version_ok "$v" \
        || die "OpenSSL 3.0 以上が必要です (更新の署名の検証に openssl pkeyutl -rawin を使います)。今: ${v:-不明}。Debian 12・Ubuntu 22.04・Raspberry Pi OS Bookworm 以降なら標準で入っています。古い OS は OS の更新が必要です"
}

# SHA256SUMS の署名を、埋め込みの公開鍵 (RELEASE_PUBKEYS_PEM) のどれかで確かめる。
# 署名が無い・長さが違う・公開鍵が未設定・どの鍵でも通らない、はすべて失敗 (1)
# 使い方: verify_sums_signature <SHA256SUMS> <SHA256SUMS.sig>
verify_sums_signature() {
    local sums="$1" sig="$2" keydir key nkeys=0 rc=1
    if [[ ! -s "$sig" ]]; then
        echo "SHA256SUMS.sig がありません (署名のないリリース)" >&2
        return 1
    fi
    if [[ "$(wc -c < "$sig")" -ne 64 ]]; then
        echo "SHA256SUMS.sig の長さが不正です (Ed25519 の署名は 64 バイト)" >&2
        return 1
    fi
    keydir=$(mktemp -d)
    # 埋め込みの PEM を 1 本ずつファイルに分ける
    awk -v dir="$keydir" '
        /^-----BEGIN PUBLIC KEY-----$/ { n++; f = dir "/key" n ".pem" }
        f != "" { print > f }
        /^-----END PUBLIC KEY-----$/ { if (f != "") close(f); f = "" }
    ' <<< "${RELEASE_PUBKEYS_PEM:-}"
    for key in "$keydir"/key*.pem; do
        [[ -f "$key" ]] || continue
        nkeys=$((nkeys + 1))
        if openssl pkeyutl -verify -pubin -inkey "$key" -rawin \
                -in "$sums" -sigfile "$sig" >/dev/null 2>&1; then
            rc=0
            break
        fi
    done
    rm -rf "$keydir"
    if (( nkeys == 0 )); then
        echo "署名を確かめる公開鍵が埋め込まれていません (この版では更新できません)" >&2
        return 1
    fi
    if (( rc != 0 )); then
        echo "SHA256SUMS の署名を確かめられません (改ざん、または知らない鍵の署名)" >&2
    fi
    return "$rc"
}

# 署名した SHA256SUMS の先頭行が `# swim-worker-release <タグ>` と完全に一致するか。
# 署名を版に結び付ける (古い版の署名済みファイルを新しいタグとして出すリプレイを防ぐ)
# 使い方: check_release_line <SHA256SUMS> <tag>
check_release_line() {
    local first=""
    IFS= read -r first < "$1" || true
    if [[ "$first" != "# swim-worker-release $2" ]]; then
        echo "SHA256SUMS の版の行が $2 と一致しません。別の版のファイルの可能性があります" >&2
        return 1
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

# 指定したファイル群を DL して検証する: 先に SHA256SUMS と SHA256SUMS.sig を取って署名を
# 埋め込みの公開鍵で確かめ (通らなければ本体は落とさない)、各ファイルのハッシュを照合する
# 使い方: download_and_verify <base_url> <tmpdir> <file1> [<file2> ...]
download_and_verify() {
    local base_url="$1"
    local tmpdir="$2"
    shift 2
    local f expected actual

    for f in SHA256SUMS SHA256SUMS.sig; do
        curl -fsSL --proto '=https' --tlsv1.2 --max-filesize 65536 \
            -o "${tmpdir}/${f}" "${base_url}/${f}" \
            || die "ダウンロード失敗: ${f} (署名のないリリースは入れません)"
    done
    verify_sums_signature "${tmpdir}/SHA256SUMS" "${tmpdir}/SHA256SUMS.sig" \
        || die "リリースの署名を確かめられないため中止します"
    # 署名された版の行が、取りに行ったタグ (base_url の最後) と一致すること
    check_release_line "${tmpdir}/SHA256SUMS" "${base_url##*/}" \
        || die "SHA256SUMS が ${base_url##*/} のものではないため中止します"

    for f in "$@"; do
        curl -fsSL --proto '=https' --tlsv1.2 \
            -o "${tmpdir}/${f}" "${base_url}/${f}" \
            || die "ダウンロード失敗: ${f}"
    done

    # SHA256 照合 (名前は完全一致で探す)
    for f in "$@"; do
        expected=$(awk -v n="$f" '$2 == n || $2 == ("*" n) { print $1; exit }' "${tmpdir}/SHA256SUMS")
        [[ -n "$expected" ]] || die "SHA256SUMS に ${f} のエントリがありません"
        actual=$(sha256sum "${tmpdir}/${f}" | awk '{print $1}')
        if [[ "$expected" != "$actual" ]]; then
            die "SHA256 不一致: ${f} (expected=${expected}, actual=${actual})"
        fi
    done
}

# 検証済みの更新スクリプト・update.service・timer を置く (反映は daemon-reload 後)。
# 本体の unit (swim-worker.service) はここでは置かない (install_worker_unit が drop-in と一緒に置く)。
# 更新スクリプトは実行中に置き換えることがあるので、一時ファイルに書いてから mv で入れ替える
# 使い方: install_updater_files <tmpdir>
install_updater_files() {
    local tmpdir="$1"
    install -m 0644 -o root -g root "${tmpdir}/swim-worker-update.service" "$UPDATE_SERVICE_FILE"
    install -m 0644 -o root -g root "${tmpdir}/swim-worker-update.timer"   "$UPDATE_TIMER_FILE"
    if [[ -L "$UPDATER_DIR" ]]; then
        die "${UPDATER_DIR} がシンボリックリンクです。確認してください"
    fi
    mkdir -p "$UPDATER_DIR"
    chown root:root "$UPDATER_DIR"
    chmod 0755 "$UPDATER_DIR"
    rm -f "${UPDATER_PATH}.new"
    install -m 0755 -o root -g root "${tmpdir}/swim-worker-update.sh" "${UPDATER_PATH}.new"
    mv -f "${UPDATER_PATH}.new" "$UPDATER_PATH"
}

# 本体の unit を、通信許可の drop-in と一緒に置く (反映は daemon-reload 後)。
# unit は LAN 宛ての通信を閉じている (IPAddressDeny) ので、drop-in が無いまま入ると
# DNS がルーターにある機が名前解決できなくなる。drop-in を先に書き、書けたら unit を置く
# (drop-in だけが先に入っても、IPAddressDeny の無い古い unit には影響しない)
# 使い方: install_worker_unit <tmpdir>
install_worker_unit() {
    local tmpdir="$1"
    write_ip_allow_dropin
    [[ -f "$DROPIN_FILE" ]] || die "通信許可の drop-in (${DROPIN_FILE}) を置けませんでした"
    install -m 0644 -o root -g root "${tmpdir}/swim-worker.service" "$SERVICE_FILE"
}

# 更新で置き換えるファイル (ロールバックで戻す)
update_backup_targets() {
    printf '%s\n' "$SERVICE_FILE" "$DROPIN_FILE" "$UPDATE_SERVICE_FILE" "$UPDATE_TIMER_FILE" "$UPDATER_PATH"
}

# 置き換える前に <ファイル>.old に退避する (無かったものは .old も作らない = 戻すときに消す)
backup_update_files() {
    local f
    while IFS= read -r f; do
        rm -f "${f}.old"
        if [[ -f "$f" ]]; then
            cp -p "$f" "${f}.old"
        fi
    done < <(update_backup_targets)
}

# 退避したものに戻す (反映は daemon-reload 後)。退避が無いもの (更新前に無かったもの) は消す
restore_update_files() {
    local f
    while IFS= read -r f; do
        if [[ -f "${f}.old" ]]; then
            mv -f "${f}.old" "$f"
        else
            rm -f "$f"
        fi
    done < <(update_backup_targets)
}

# 更新がうまくいったら退避を消す
drop_update_backups() {
    local f
    while IFS= read -r f; do
        rm -f "${f}.old"
    done < <(update_backup_targets)
}

# 一緒に配る (検証して置く) ファイル。旧方式からの移行では UPDATER_FILES だけを置く
UPDATER_FILES=(swim-worker-update.service swim-worker-update.timer swim-worker-update.sh)
UPDATE_FILES=(swim-worker.service "${UPDATER_FILES[@]}")

# --- 事前チェック ---
[[ $EUID -eq 0 ]] || die "root で実行してください (sudo bash install.sh)"

command -v systemctl >/dev/null || die "systemd が必要です"
command -v curl >/dev/null      || die "curl が必要です (apt install -y curl)"
command -v sha256sum >/dev/null || die "sha256sum が必要です"
require_openssl

# アーキテクチャ判定
ARCH_RAW=$(uname -m)
case "$ARCH_RAW" in
    x86_64|amd64)      ARCH="amd64" ;;
    aarch64|arm64)     ARCH="arm64" ;;
    *) die "未対応のアーキテクチャ: $ARCH_RAW (amd64 / arm64 のみサポート)" ;;
esac
BINARY_NAME="swim-worker-linux-${ARCH}"

# ========================================================================
# --auto モード
#   SWIM_UPDATE_TAG あり: 固定の更新スクリプト (update.sh) が一時停止・段階配布・メジャー版を
#     確かめ、署名を確かめたこの install.sh を呼んだ。そのタグの版を入れ、起動を確かめる
#     (起動しなければロールバック)
#   SWIM_UPDATE_TAG なし: 旧 update.service (最新の install.sh を取って --auto で実行) か手動。
#     更新スクリプトと unit を最新リリースから (署名を確かめて) 置き直し、更新スクリプトに任せる
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

    if [[ -z "${SWIM_UPDATE_TAG:-}" ]]; then
        # --- 旧方式からの移行: 固定の更新スクリプト・update.service・timer だけを置く ---
        # 本体の unit・バイナリはここでは替えない (次の timer で新しい update.service が
        # 更新スクリプト経由で drop-in と一緒に入れる)。旧 update.service は TimeoutStartSec=600 で
        # この処理を動かしているので、長い処理 (バイナリの更新・起動待ち) はしない。
        # opt-out した機も移す: 旧 update.service は署名を確かめない最新の
        # install.sh を root で動かし続けるため。バイナリを更新しないのは新しい更新スクリプトが守る
        LATEST_TAG=$(fetch_latest_tag "$LATEST_API_URL")
        validate_tag "$LATEST_TAG" strict || die "GitHub API から最新のタグを取得できません (${LATEST_TAG:-空})"
        TMPDIR=$(mktemp -d)
        trap 'rm -rf "$TMPDIR"' EXIT
        download_and_verify "${DOWNLOAD_BASE}/${LATEST_TAG}" "$TMPDIR" "${UPDATER_FILES[@]}"
        # 一時停止 (kill switch)・段階配布 (whitelist) は、署名を確かめた新しい更新スクリプトの
        # 判定 (--guard-only) をそのまま使う。止められたら何も置かずに終わり、次回また試す
        guard_rc=0
        bash "${TMPDIR}/swim-worker-update.sh" --guard-only || guard_rc=$?
        if (( guard_rc != 0 )); then
            log "Coordinator の一時停止・段階配布により、新方式への移行を見送ります (次回また試します)"
            exit 0
        fi
        install_updater_files "$TMPDIR"
        systemctl daemon-reload
        log "固定の更新スクリプト (${UPDATER_PATH})・update.service・timer を ${LATEST_TAG} から置きました"
        log "次の自動更新 (timer) から新方式で更新します (今すぐ: systemctl start swim-worker-update.service)"
        exit 0
    fi

    # --- 固定の更新スクリプトから: 指定のタグを入れる ---
    LATEST_TAG="$SWIM_UPDATE_TAG"
    validate_tag "$LATEST_TAG" strict || die "SWIM_UPDATE_TAG の形式が不正です: ${LATEST_TAG}"
    LATEST_VERSION="${LATEST_TAG#v}"
    AUTH_BROKEN="${SWIM_UPDATE_AUTH_BROKEN:-0}"
    [[ "$AUTH_BROKEN" == "1" ]] || AUTH_BROKEN=0

    # 今の版より新しい版にだけ更新する (署名済みの古い版へのダウングレードもしない)
    NEWER=$(printf '%s\n%s\n' "$CURRENT_VERSION" "$LATEST_VERSION" | sort -V | tail -1)
    if [[ "$CURRENT_VERSION" == "$LATEST_VERSION" || "$NEWER" != "$LATEST_VERSION" ]]; then
        log "v${LATEST_VERSION} は今の版 (v${CURRENT_VERSION}) より新しくないため skip"
        exit 0
    fi

    log "自動更新: v${CURRENT_VERSION} → v${LATEST_VERSION}"

    # --- 新バイナリ・更新スクリプト・unit を DL + 検証 (タグに固定) ---
    TMPDIR=$(mktemp -d)
    trap 'rm -rf "$TMPDIR"' EXIT
    download_and_verify "${DOWNLOAD_BASE}/${LATEST_TAG}" "$TMPDIR" "$BINARY_NAME" "${UPDATE_FILES[@]}"

    # --- 旧バイナリ・unit・drop-in・更新スクリプトを .old に退避 → 新しいものを配置 → restart ---
    rm -f "${INSTALL_DIR}/swim-worker.old"
    cp -p "${INSTALL_DIR}/swim-worker" "${INSTALL_DIR}/swim-worker.old"
    backup_update_files
    install -m 0755 -o root -g root "${TMPDIR}/${BINARY_NAME}" "${INSTALL_DIR}/swim-worker"
    write_root_file "$VERSION_FILE" "$LATEST_VERSION"   # .version はバイナリと同時に (ロールバックで戻す)
    install_updater_files "$TMPDIR"
    install_worker_unit "$TMPDIR"
    fix_permissions
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
            rm -f "${INSTALL_DIR}/swim-worker.old"
            drop_update_backups
            exit 0
        fi
    elif wait_for_startup "$STARTUP_WAIT"; then
        log "自動更新成功 (起動を確認)"
        rm -f "${INSTALL_DIR}/swim-worker.old"
        drop_update_backups
        exit 0
    fi

    # ロールバック
    warn "新版 (v${LATEST_VERSION}) の起動を確認できない、ロールバック実行"
    install -m 0755 -o root -g root \
        "${INSTALL_DIR}/swim-worker.old" "${INSTALL_DIR}/swim-worker"
    write_root_file "$VERSION_FILE" "$CURRENT_VERSION"
    restore_update_files   # unit・drop-in・更新スクリプト・update.service・timer も戻す
    systemctl daemon-reload
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

log "バイナリ / systemd unit / timer / 更新スクリプトをダウンロード & 署名・SHA256 検証..."
download_and_verify "${DOWNLOAD_BASE}/${TAG}" "$TMPDIR" "$BINARY_NAME" "${UPDATE_FILES[@]}"
log "署名・整合性 OK"

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

# --- systemd unit / update timer / 固定の更新スクリプト配置 ---
log "systemd unit と更新スクリプト (${UPDATER_PATH}) を配置..."
install_updater_files "$TMPDIR"
install_worker_unit "$TMPDIR"
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
