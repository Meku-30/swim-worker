#!/usr/bin/env bash
# リリースと同じ形の置き場を作る (試運転用)。使い捨ての Ed25519 の鍵で SHA256SUMS に署名する
# 使い方: make_release.sh <版 (1.3.0)> <Linux の実行ファイル> <arch> <scripts のある所> <置き場> <鍵 (PEM、無ければ作る)>
set -euo pipefail
ver="$1" bin="$2" arch="$3" scripts="$4" root="$5" key="$6"
tag="v${ver}"
d="${root}/download/${tag}"
mkdir -p "$d"
[[ -f "$key" ]] || openssl genpkey -algorithm ed25519 -out "$key" 2>/dev/null
cp "$bin" "${d}/swim-worker-linux-${arch}"
for f in install.sh swim-worker-update.sh swim-worker.service swim-worker-update.service swim-worker-update.timer; do
    cp "${scripts}/${f}" "${d}/${f}"
done
(cd "$d" && { echo "# swim-worker-release ${tag}"; sha256sum -- swim-worker-linux-* install.sh swim-worker-update.sh *.service *.timer; } > SHA256SUMS)
openssl pkeyutl -sign -inkey "$key" -rawin -in "${d}/SHA256SUMS" -out "${d}/SHA256SUMS.sig"
printf '{"tag_name": "%s"}\n' "$tag" > "${root}/latest.json"
