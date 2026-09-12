"""SHA256SUMS 検証のテスト"""
import hashlib

import pytest

from swim_worker.update_verify import UpdateVerifyError, parse_sha256sums, verify_sha256


def _sums(*entries: tuple[str, bytes]) -> str:
    return "".join(f"{hashlib.sha256(b).hexdigest()}  {name}\n" for name, b in entries)


class TestParseSha256sums:
    def test_parses_sha256sum_format(self):
        text = "aa" * 32 + "  swim-worker-windows.exe\n" + "bb" * 32 + " *swim-worker-macos\n\n"
        sums = parse_sha256sums(text)
        assert sums == {"swim-worker-windows.exe": "aa" * 32, "swim-worker-macos": "bb" * 32}


class TestVerifySha256:
    def test_ok(self):
        content = b"exe-bytes"
        text = _sums(("install.sh", b"x"), ("swim-worker-windows.exe", content))
        assert verify_sha256(content, text, "swim-worker-windows.exe") == hashlib.sha256(content).hexdigest()

    def test_mismatch_raises(self):
        text = _sums(("swim-worker-windows.exe", b"original"))
        with pytest.raises(UpdateVerifyError, match="不一致"):
            verify_sha256(b"tampered", text, "swim-worker-windows.exe")

    def test_missing_entry_raises(self):
        text = _sums(("swim-worker-macos", b"x"))
        with pytest.raises(UpdateVerifyError, match="エントリがありません"):
            verify_sha256(b"x", text, "swim-worker-windows.exe")
