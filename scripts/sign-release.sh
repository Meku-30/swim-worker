#!/usr/bin/env bash
# draft のリリースの SHA256SUMS に署名して公開する (管理者のノート PC で実行)
#
# 使い方:
#   scripts/sign-release.sh <タグ>               # 確認 → 署名 → SHA256SUMS.sig を上げる → 公開
#   scripts/sign-release.sh --no-publish <タグ>  # 公開はしない (draft のまま。後で gh release edit)
#   scripts/sign-release.sh --yes <タグ>         # 公開の確認を聞かない
#
# 環境変数:
#   SWIM_RELEASE_KEY   署名鍵 (パスフレーズ付き PEM)。既定 ~/.config/swim-release/signing-key.pem
#   SWIM_RELEASE_REPO  既定 Meku-30/swim-worker
#
# やること:
#   1. gh で draft のリリースの全ファイルを取る (gh の認証はあなたのもの)
#   2. SHA256SUMS の先頭行がタグと一致し、全ファイルのハッシュが一致し、足りない・余計な
#      ファイルがないことを確かめる。install.sh・更新スクリプト・unit はこのリポジトリの
#      タグの中身と同じか確かめる
#   3. openssl pkeyutl -sign -rawin で SHA256SUMS に署名 (パスフレーズは openssl が聞く)
#   4. リリースの install.sh・更新スクリプトに埋め込まれた公開鍵で署名を確かめる
#   5. SHA256SUMS.sig を上げ、上がったものが同じか確かめてから公開する
#
# 鍵の作り方・失くしたときは docs/release-signing.md。

set -euo pipefail
umask 077

REPO="${SWIM_RELEASE_REPO:-Meku-30/swim-worker}"
KEY="${SWIM_RELEASE_KEY:-${HOME}/.config/swim-release/signing-key.pem}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 配るファイル (build-release.yml の Prepare release files と同じ)
EXPECTED_ASSETS=(
    swim-worker-linux-amd64 swim-worker-linux-arm64 swim-worker-linux
    swim-worker-macos swim-worker-windows.exe
    install.sh swim-worker-update.sh
    swim-worker.service swim-worker-update.service swim-worker-update.timer
)
# リポジトリのタグの中身と同じであるべきもの (配布物の名前:リポジトリのパス)
SOURCE_ASSETS=(
    install.sh:scripts/install.sh
    swim-worker-update.sh:scripts/swim-worker-update.sh
    swim-worker.service:scripts/swim-worker.service
    swim-worker-update.service:scripts/swim-worker-update.service
    swim-worker-update.timer:scripts/swim-worker-update.timer
)

log()  { echo "==> $*"; }
warn() { echo "!!  $*" >&2; }
die()  { echo "ERR $*" >&2; exit 1; }

PUBLISH=1
ASSUME_YES=0
while (( $# )); do
    case "$1" in
        --no-publish) PUBLISH=0; shift ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
        -*) die "知らないオプション: $1" ;;
        *) break ;;
    esac
done
(( $# == 1 )) || die "使い方: $0 [--no-publish] [--yes] <タグ>"
TAG="$1"
[[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]] || die "タグの形式が不正です: ${TAG}"

for c in gh openssl sha256sum awk cmp; do
    command -v "$c" >/dev/null || die "$c が必要です"
done
# pkeyutl -rawin (Ed25519 で SHA256SUMS をそのまま署名・検証) は OpenSSL 3.0 から
ossl=$(openssl version 2>/dev/null || true)
if [[ ! "$ossl" =~ ^OpenSSL\ ([0-9]+)\. ]] || (( BASH_REMATCH[1] < 3 )); then
    die "OpenSSL 3.0 以上が必要です (pkeyutl -rawin)。今: ${ossl:-不明}"
fi
[[ -f "$KEY" ]] || die "署名鍵がありません: ${KEY} (docs/release-signing.md の「鍵を作る」)"
if ! head -1 "$KEY" | grep -q "ENCRYPTED PRIVATE KEY"; then
    warn "署名鍵にパスフレーズが付いていません: ${KEY}"
fi
perm=$(stat -c '%a' "$KEY" 2>/dev/null || stat -f '%Lp' "$KEY")
[[ "$perm" == "600" || "$perm" == "400" ]] || warn "署名鍵のパーミッションが ${perm} です (chmod 600 を推奨)"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
ASSETS="${WORK}/assets"
mkdir -p "$ASSETS"

# --- 1. draft を取る ---
is_draft=$(gh release view "$TAG" -R "$REPO" --json isDraft -q .isDraft) \
    || die "リリース ${TAG} が見つかりません (CI の build-release が終わっているか確認)"
[[ "$is_draft" == "true" ]] || die "${TAG} は draft ではありません (公開済みのリリースには署名し直さない)"
log "draft ${TAG} のファイルを取得..."
gh release download "$TAG" -R "$REPO" -D "$ASSETS"
[[ ! -e "${ASSETS}/SHA256SUMS.sig" ]] || die "${TAG} には既に SHA256SUMS.sig があります。確認してください"
[[ -f "${ASSETS}/SHA256SUMS" ]] || die "${TAG} に SHA256SUMS がありません"

# --- 2. 中身を確かめる ---
first=""
IFS= read -r first < "${ASSETS}/SHA256SUMS" || true
[[ "$first" == "# swim-worker-release ${TAG}" ]] \
    || die "SHA256SUMS の先頭行がタグ ${TAG} と一致しません: ${first}"

declare -A listed=()
while read -r hash name; do
    [[ "$hash" == "#" ]] && continue
    name="${name#\*}"
    [[ "$hash" =~ ^[0-9a-f]{64}$ && -n "$name" && "$name" != */* ]] \
        || die "SHA256SUMS の行が不正です: ${hash} ${name}"
    [[ -f "${ASSETS}/${name}" ]] || die "SHA256SUMS にある ${name} がリリースにありません"
    actual=$(sha256sum "${ASSETS}/${name}" | awk '{print $1}')
    [[ "$actual" == "$hash" ]] || die "ハッシュ不一致: ${name}"
    listed["$name"]=1
done < <(tail -n +2 "${ASSETS}/SHA256SUMS")
for f in "${ASSETS}"/*; do
    name="${f##*/}"
    [[ "$name" == "SHA256SUMS" ]] && continue
    [[ -n "${listed[$name]:-}" ]] || die "SHA256SUMS に載っていないファイルがあります: ${name}"
done
for name in "${EXPECTED_ASSETS[@]}"; do
    [[ -n "${listed[$name]:-}" ]] || die "リリースに ${name} がありません"
done
log "SHA256SUMS とファイル (${#listed[@]} 個) のハッシュ OK"

# 配布するスクリプト・unit がリポジトリのタグの中身と同じか
# (SWIM_RELEASE_SRC_DIR があればそこのファイルと比べる。テスト用)
for pair in "${SOURCE_ASSETS[@]}"; do
    asset="${pair%%:*}"
    path="${pair#*:}"
    if [[ -n "${SWIM_RELEASE_SRC_DIR:-}" ]]; then
        cp "${SWIM_RELEASE_SRC_DIR}/${path}" "${WORK}/src"
    else
        git -C "$ROOT" show "${TAG}:${path}" > "${WORK}/src" 2>/dev/null \
            || die "このリポジトリにタグ ${TAG} がありません (git fetch --tags してから)"
    fi
    cmp -s "${WORK}/src" "${ASSETS}/${asset}" || die "${asset} がタグ ${TAG} の ${path} と違います"
done
log "install.sh・更新スクリプト・unit はタグ ${TAG} の中身と同じ"

# リリースの install.sh / 更新スクリプトに埋め込まれた公開鍵 (これで Worker が確かめる)
extract_keys() {
    awk -v dir="$2" -v pre="$3" '
        /^# BEGIN RELEASE PUBKEYS$/ { inb = 1; next }
        /^# END RELEASE PUBKEYS$/ { inb = 0 }
        inb && /^-----BEGIN PUBLIC KEY-----$/ { n++; f = dir "/" pre n ".pem" }
        inb && f != "" { print > f }
        inb && /^-----END PUBLIC KEY-----$/ { close(f); f = "" }
    ' "$1"
}
mkdir -p "${WORK}/keys"
extract_keys "${ASSETS}/install.sh" "${WORK}/keys" install
extract_keys "${ASSETS}/swim-worker-update.sh" "${WORK}/keys" update
compgen -G "${WORK}/keys/install*.pem" >/dev/null \
    || die "リリースの install.sh に公開鍵が埋め込まれていません"

echo
echo "----- SHA256SUMS (${TAG}) -----"
cat "${ASSETS}/SHA256SUMS"
echo "-------------------------------"
echo

# --- 3. 署名 ---
log "SHA256SUMS に署名します (署名鍵のパスフレーズを入力)"
openssl pkeyutl -sign -inkey "$KEY" -rawin \
    -in "${ASSETS}/SHA256SUMS" -out "${WORK}/SHA256SUMS.sig"
[[ "$(wc -c < "${WORK}/SHA256SUMS.sig")" -eq 64 ]] || die "署名の長さが 64 バイトではありません (Ed25519 の鍵か確認)"

# --- 4. 埋め込みの公開鍵で確かめる (install.sh・更新スクリプトのどちらでも通ること) ---
verify_with() {
    local pre="$1" k
    for k in "${WORK}/keys/${pre}"*.pem; do
        [[ -f "$k" ]] || continue
        if openssl pkeyutl -verify -pubin -inkey "$k" -rawin \
                -in "${ASSETS}/SHA256SUMS" -sigfile "${WORK}/SHA256SUMS.sig" >/dev/null 2>&1; then
            echo "${k##*/}"
            return 0
        fi
    done
    return 1
}
k1=$(verify_with install) || die "リリースの install.sh に埋め込まれた公開鍵で署名を確かめられません (鍵が違う)"
k2=$(verify_with update) || die "リリースの更新スクリプトに埋め込まれた公開鍵で署名を確かめられません"
log "署名 OK (埋め込みの公開鍵 ${k1%.pem} / ${k2%.pem} で確認)"

# --- 5. 上げて公開 ---
log "SHA256SUMS.sig を上げます..."
gh release upload "$TAG" "${WORK}/SHA256SUMS.sig" -R "$REPO"
mkdir -p "${WORK}/check"
gh release download "$TAG" -R "$REPO" -p SHA256SUMS.sig -D "${WORK}/check"
cmp -s "${WORK}/SHA256SUMS.sig" "${WORK}/check/SHA256SUMS.sig" || die "上がった SHA256SUMS.sig が違います"

if (( PUBLISH == 0 )); then
    log "署名を上げました (draft のまま)。公開: gh release edit ${TAG} -R ${REPO} --draft=false"
    exit 0
fi
if (( ASSUME_YES == 0 )); then
    ans=""
    read -rp "${TAG} を公開しますか (公開すると Worker の自動更新の対象になります) [y/N]: " ans || true
    [[ "$ans" == "y" || "$ans" == "Y" ]] || { log "公開しませんでした (draft のまま、署名は上げ済み)"; exit 0; }
fi
gh release edit "$TAG" -R "$REPO" --draft=false >/dev/null
log "${TAG} を公開しました"
