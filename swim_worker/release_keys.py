"""更新物 (リリースの SHA256SUMS) の署名を検証する Ed25519 公開鍵

ふだんは 1 本。鍵を計画的に入れ替えるときだけ 2 本 (旧鍵で署名した版で新旧 2 本を配る) にでき、
どちらかで検証が通ればよい。鍵を失くしたら、既存の Worker は新しい鍵の版を受け付けないので、
全台で install.sh / GUI を手作業で入れ直す (docs/release-signing.md)。

この値は scripts/release_pubkeys/*.pub.pem から `scripts/set-release-pubkeys.sh` が書く。
手で編集しない。install.sh と swim-worker-update.sh にも同じ鍵が入っていることを
tests/test_release_pubkeys.py が確かめている。空 (未設定) のままだと、GUI の更新は
どのリリースも受け付けず、リリース CI も失敗する。
"""

# BEGIN RELEASE PUBKEYS
RELEASE_PUBKEYS_PEM: tuple[str, ...] = (
    """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAZ+6EShmEUwozcwVIWHNfkMdw4JvziE15STc5ZJ7yuLk=
-----END PUBLIC KEY-----
""",
)
# END RELEASE PUBKEYS
