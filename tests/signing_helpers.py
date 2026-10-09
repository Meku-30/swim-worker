"""署名のテスト用ヘルパー: テストの中だけで使う一時的な Ed25519 鍵を openssl で作る。

本物の署名鍵 (ノート PC の ~/.config/swim-release/signing-key.pem) には触らない。
"""
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SigningKey:
    private: Path   # 秘密鍵 (PEM・パスフレーズなし。テスト用)
    public: Path    # 公開鍵 (PEM)

    @property
    def public_pem(self) -> str:
        return self.public.read_text()


def make_key(dirpath: Path, name: str) -> SigningKey:
    dirpath.mkdir(parents=True, exist_ok=True)
    priv = dirpath / f"{name}.key.pem"
    pub = dirpath / f"{name}.pub.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(priv)],
                   check=True, capture_output=True)
    subprocess.run(["openssl", "pkey", "-in", str(priv), "-pubout", "-out", str(pub)],
                   check=True, capture_output=True)
    return SigningKey(priv, pub)


def sign(key: SigningKey, data_path: Path, sig_path: Path) -> None:
    """sign-release.sh と同じ openssl コマンドで署名する"""
    subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", str(key.private), "-rawin",
                    "-in", str(data_path), "-out", str(sig_path)],
                   check=True, capture_output=True)
