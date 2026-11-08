"""更新の署名の見本: 使い捨ての Ed25519 の鍵で SHA256SUMS に署名し、Python の verify_sums_signature が通すことを確かめて置く。
鍵は使い捨て (秘密鍵は残さない)。使い方: python tools/golden/dump_update_sig.py --out rust/tests/golden/updater
"""
import argparse
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from swim_worker import update_verify  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    out = Path(ap.parse_args().out)
    exe = b"MZ" + b"\0" * (1024 * 1024)  # 1MB を少し超える偽の exe
    sums = ("# swim-worker-release v1.3.1\n"
            f"{hashlib.sha256(exe).hexdigest()}  swim-worker-windows.exe\n"
            f"{'0' * 64} *install.sh\n").encode()
    keys = [Ed25519PrivateKey.generate() for _ in range(2)]
    pems = [k.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo).decode() for k in keys]
    sig = keys[1].sign(sums)
    assert update_verify.verify_sums_signature(sums, sig, pems) == 1
    (out / "SHA256SUMS").write_bytes(sums)
    (out / "SHA256SUMS.sig").write_bytes(sig)
    (out / "key_a.pub.pem").write_text(pems[0])
    (out / "key_b.pub.pem").write_text(pems[1])
    (out / "exe.sha256").write_text(hashlib.sha256(exe).hexdigest())


if __name__ == "__main__":
    main()
