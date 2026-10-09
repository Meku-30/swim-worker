"""自動更新でダウンロードしたバイナリの整合性検証 (SHA256SUMS + 署名)

CI が release ディレクトリで `sha256sum * > SHA256SUMS` を生成し、リリースを draft で作る。
管理者がノート PC の Ed25519 鍵で SHA256SUMS に署名し (scripts/sign-release.sh)、
`SHA256SUMS.sig` (生の 64 バイト) を上げてから公開する。
SHA256SUMS の先頭行は `# swim-worker-release vX.Y.Z` (CI が書く)。署名はこの行ごとなので、
署名は版 (タグ) に結び付く: 古い版の署名済みファイルを新しいタグとして出しても、
取りに行ったタグと先頭行が合わず通らない (リプレイ・ダウングレードの防止)。
GUI 自動更新 (updater.py) は SHA256SUMS の署名を埋め込みの公開鍵 (release_keys.py) で
確かめ、そのうえで exe のハッシュを照合する (install.sh・swim-worker-update.sh は同じ検証を
openssl で行っている)。tkinter 非依存で単体テスト可能。
"""
import hashlib

from swim_worker import release_keys

SIGNATURE_SIZE = 64   # Ed25519 の署名は 64 バイト
MAX_RELEASE_KEYS = 2  # 鍵の入れ替え用に 2 本まで受け付ける (ふだんは 1 本)
RELEASE_LINE_PREFIX = "# swim-worker-release "


class UpdateVerifyError(RuntimeError):
    """SHA256SUMS にエントリが無い、またはハッシュ不一致"""


def parse_sha256sums(text: str) -> dict[str, str]:
    """`sha256sum` 形式 ("<hex>  <name>" / "<hex> *<name>") を {name: hex} に変換する"""
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        digest, name = parts
        name = name.lstrip("*").strip()
        result[name] = digest.lower()
    return result


def verify_sha256(content: bytes, sums_text: str, asset_name: str) -> str:
    """content の SHA256 が SHA256SUMS の asset_name エントリと一致するか検証する。

    一致すれば hex digest を返し、エントリ欠落・不一致なら UpdateVerifyError。
    """
    sums = parse_sha256sums(sums_text)
    expected = sums.get(asset_name)
    if not expected:
        raise UpdateVerifyError(f"SHA256SUMS に {asset_name} のエントリがありません")
    actual = hashlib.sha256(content).hexdigest()
    if actual != expected:
        raise UpdateVerifyError(
            f"SHA256 不一致: {asset_name} (expected={expected[:16]}…, actual={actual[:16]}…)"
        )
    return actual


def load_release_pubkeys(pems):
    """PEM の公開鍵を Ed25519PublicKey にする。Ed25519 以外・3 本以上は UpdateVerifyError"""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    pems = tuple(pems)
    if not pems:
        raise UpdateVerifyError("更新の署名を確かめる公開鍵が未設定です (この版では自動更新できません)")
    if len(pems) > MAX_RELEASE_KEYS:
        raise UpdateVerifyError(f"公開鍵は {MAX_RELEASE_KEYS} 本までです ({len(pems)} 本)")
    keys = []
    for pem in pems:
        try:
            key = load_pem_public_key(pem.encode("ascii"))
        except Exception as e:
            raise UpdateVerifyError(f"公開鍵を読めません: {e}") from e
        if not isinstance(key, Ed25519PublicKey):
            raise UpdateVerifyError("公開鍵が Ed25519 ではありません")
        keys.append(key)
    return keys


def verify_sums_signature(sums: bytes, signature: bytes, pubkeys_pem=None) -> int:
    """SHA256SUMS の署名を、埋め込みの公開鍵のどれかで確かめる。

    通れば何本目の鍵 (0 始まり) で通ったかを返す。署名が無い・壊れている・
    改ざん・別の鍵・公開鍵が未設定はすべて UpdateVerifyError。
    """
    from cryptography.exceptions import InvalidSignature

    keys = load_release_pubkeys(
        release_keys.RELEASE_PUBKEYS_PEM if pubkeys_pem is None else pubkeys_pem)
    if len(signature) != SIGNATURE_SIZE:
        raise UpdateVerifyError(
            f"SHA256SUMS の署名の長さが不正です ({len(signature)} バイト、{SIGNATURE_SIZE} のはず)")
    for i, key in enumerate(keys):
        try:
            key.verify(signature, sums)
            return i
        except InvalidSignature:
            continue
    raise UpdateVerifyError("SHA256SUMS の署名を確かめられません (改ざん、または知らない鍵の署名)")


def release_line(tag: str) -> str:
    """SHA256SUMS の先頭行 (CI が書き、署名の対象に含まれる)"""
    return f"{RELEASE_LINE_PREFIX}{tag}"


def check_release_line(sums: bytes, tag: str) -> None:
    """署名した SHA256SUMS の先頭行が、取りに行ったタグと完全に一致するか。違えば UpdateVerifyError"""
    first = sums.split(b"\n", 1)[0]
    if first != release_line(tag).encode("ascii"):
        shown = first[:60].decode("utf-8", "replace")
        raise UpdateVerifyError(
            f"SHA256SUMS の版の行が {tag} と一致しません ({shown!r})。別の版のファイルの可能性があります")
