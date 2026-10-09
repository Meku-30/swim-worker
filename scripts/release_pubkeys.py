#!/usr/bin/env python3
"""リリースの署名を確かめる公開鍵を、配布物に埋め込む / 埋め込みを確かめる

公開鍵の元: scripts/release_pubkeys/*.pub.pem (名前順。1 本、鍵の入れ替え中だけ 2 本まで)
埋め込み先 (BEGIN/END RELEASE PUBKEYS の間を書き換える):
  - swim_worker/release_keys.py   (GUI の更新)
  - scripts/install.sh            (新規インストール・自動更新の適用)
  - scripts/swim-worker-update.sh (固定の更新スクリプト)

使い方:
  python3 scripts/release_pubkeys.py sync             # 元のファイルから埋め込みを書き直す
  python3 scripts/release_pubkeys.py check            # 埋め込みが元と一致するか (CI・テスト)
  python3 scripts/release_pubkeys.py check --require  # さらに 1 本以上あること (リリース CI)

ふつうは scripts/set-release-pubkeys.sh から呼ぶ。公開鍵は Ed25519 だけ受け付ける (openssl で確認)。
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

MAX_KEYS = 2
PEM_RE = re.compile(r"-----BEGIN PUBLIC KEY-----\n[A-Za-z0-9+/=\n]+?-----END PUBLIC KEY-----\n")
BEGIN = "# BEGIN RELEASE PUBKEYS\n"
END = "# END RELEASE PUBKEYS\n"
SHELL_TARGETS = ("scripts/install.sh", "scripts/swim-worker-update.sh")
PY_TARGET = "swim_worker/release_keys.py"
KEY_DIR = "scripts/release_pubkeys"


class KeyError_(Exception):
    pass


def _normalize(pem: str) -> str:
    pem = pem.replace("\r\n", "\n").strip() + "\n"
    if "PRIVATE" in pem:
        raise KeyError_("秘密鍵が渡されました。公開鍵 (openssl pkey -pubout の出力) を使ってください")
    if not PEM_RE.fullmatch(pem):
        raise KeyError_("PEM の公開鍵 (-----BEGIN PUBLIC KEY-----) ではありません")
    return pem


def _check_ed25519(pem: str) -> None:
    r = subprocess.run(["openssl", "pkey", "-pubin", "-noout", "-text"], input=pem,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise KeyError_(f"公開鍵を読めません: {r.stderr.strip()}")
    if not r.stdout.lstrip().startswith("ED25519 Public-Key"):
        raise KeyError_("公開鍵が Ed25519 ではありません")


def source_keys(root: Path) -> list[str]:
    files = sorted((root / KEY_DIR).glob("*.pub.pem"))
    if len(files) > MAX_KEYS:
        raise KeyError_(f"公開鍵は {MAX_KEYS} 本までです ({len(files)} 本)")
    keys = []
    for f in files:
        try:
            pem = _normalize(f.read_text(encoding="ascii"))
            _check_ed25519(pem)
        except KeyError_ as e:
            raise KeyError_(f"{f.name}: {e}") from e
        keys.append(pem)
    return keys


def _block(text: str, path: str) -> tuple[int, int]:
    if text.count(BEGIN) != 1 or text.count(END) != 1:
        raise KeyError_(f"{path} に BEGIN/END RELEASE PUBKEYS が 1 組ありません")
    s = text.index(BEGIN) + len(BEGIN)
    e = text.index(END)
    return s, e


def shell_block(keys: list[str]) -> str:
    return ("read -r -d '' RELEASE_PUBKEYS_PEM <<'PUBKEYEOF' || true\n"
            + "".join(keys) + "PUBKEYEOF\n")


def py_block(keys: list[str]) -> str:
    if not keys:
        return "RELEASE_PUBKEYS_PEM: tuple[str, ...] = ()\n"
    body = "".join(f'    """{k}""",\n' for k in keys)
    return "RELEASE_PUBKEYS_PEM: tuple[str, ...] = (\n" + body + ")\n"


def embedded(root: Path) -> dict[str, list[str]]:
    out = {}
    for rel in (*SHELL_TARGETS, PY_TARGET):
        text = (root / rel).read_text(encoding="utf-8")
        s, e = _block(text, rel)
        out[rel] = PEM_RE.findall(text[s:e])
    return out


def sync(root: Path) -> list[str]:
    keys = source_keys(root)
    for rel, block in [(r, shell_block(keys)) for r in SHELL_TARGETS] + [(PY_TARGET, py_block(keys))]:
        p = root / rel
        text = p.read_text(encoding="utf-8")
        s, e = _block(text, rel)
        p.write_text(text[:s] + block + text[e:], encoding="utf-8")
    return keys


def check(root: Path, require: bool) -> list[str]:
    keys = source_keys(root)
    for rel, got in embedded(root).items():
        if got != keys:
            raise KeyError_(f"{rel} の埋め込みの公開鍵が {KEY_DIR}/ と一致しません "
                            "(scripts/set-release-pubkeys.sh か `release_pubkeys.py sync` で書き直す)")
    if require and not keys:
        raise KeyError_("リリースの署名を確かめる公開鍵が未設定です。署名できないのでリリースしません "
                        "(docs/release-signing.md の「鍵を作る」)")
    return keys


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("sync", "check"))
    ap.add_argument("--require", action="store_true", help="公開鍵が 1 本も無ければ失敗 (リリース用)")
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    a = ap.parse_args(argv)
    try:
        keys = sync(a.root) if a.command == "sync" else check(a.root, a.require)
    except KeyError_ as e:
        print(f"ERR {e}", file=sys.stderr)
        return 1
    print(f"OK 公開鍵 {len(keys)} 本" + (" (未設定)" if not keys else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
