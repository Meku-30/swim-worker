"""更新物 (リリースの SHA256SUMS) の署名を検証する Ed25519 公開鍵

最大 2 本 (主鍵 + 予備鍵)。どちらかで検証が通ればよい。主鍵を失くしたときは、
予備鍵で署名した版で新しい鍵の組に入れ替える (docs/release-signing.md)。

この値は scripts/release_pubkeys/*.pub.pem から `scripts/set-release-pubkeys.sh` が書く。
手で編集しない。install.sh と swim-worker-update.sh にも同じ鍵が入っていることを
tests/test_release_pubkeys.py が確かめている。空 (未設定) のままだと、GUI の更新は
どのリリースも受け付けず、リリース CI も失敗する。
"""

# BEGIN RELEASE PUBKEYS
RELEASE_PUBKEYS_PEM: tuple[str, ...] = ()
# END RELEASE PUBKEYS
