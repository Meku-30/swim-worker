#!/usr/bin/env bash
# install.sh・更新スクリプトの写しの 3 か所 (公開鍵・最新版の URL・ダウンロード元) を試運転用に差し替える。
# 署名・ハッシュ・https の確かめはそのまま。配布するものには手を入れない
# 使い方: patch_install.sh <写し> <公開鍵 PEM> <置き場の URL>
set -euo pipefail
f="$1" pub="$2" base="$3"
python3 - "$f" "$pub" "$base" <<'PYEOF'
import re, sys
f, pub, base = sys.argv[1:]
t = open(f).read()
key = open(pub).read().strip()
t, n = re.subn(r"(<<'PUBKEYEOF' \|\| true\n).*?(\nPUBKEYEOF)", lambda m: m.group(1) + key + m.group(2), t, flags=re.S)
assert n == 1, "公開鍵の埋め込みが見つからない"
for name, val in (("LATEST_API_URL", base + "/latest.json"), ("DOWNLOAD_BASE", base + "/download")):
    t, n = re.subn(rf'^{name}="[^"]*"$', f'{name}="{val}"', t, flags=re.M)
    assert n == 1, name
open(f, "w").write(t)
PYEOF
