"""自動更新でダウンロードしたバイナリの整合性検証 (SHA256SUMS)

CI が release ディレクトリで `sha256sum * > SHA256SUMS` を生成する。
GUI 自動更新 (gui.py) はこのモジュールで exe を検証してから差し替える
(install.sh は同じ検証を bash で行っている)。tkinter 非依存で単体テスト可能。
"""
import hashlib


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
