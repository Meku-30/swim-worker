"""更新物の署名 (SHA256SUMS.sig) の検証: Python (GUI の更新) 側

鍵はテストの中で一時的に作る (tests/signing_helpers.py)。
"""
import contextlib
import hashlib
import os

import pytest

pytest.importorskip("cryptography")

from swim_worker import release_keys, update_verify, updater  # noqa: E402
from swim_worker.update_verify import UpdateVerifyError, verify_sums_signature  # noqa: E402
from tests.signing_helpers import make_key, sign  # noqa: E402


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    d = tmp_path_factory.mktemp("keys")
    return {name: make_key(d, name) for name in ("primary", "backup", "other")}


def _signed(tmp_path, key, data: bytes) -> bytes:
    p = tmp_path / "SHA256SUMS"
    p.write_bytes(data)
    sig = tmp_path / "SHA256SUMS.sig"
    sign(key, p, sig)
    return sig.read_bytes()


SUMS = b"aa" * 32 + b"  swim-worker-windows.exe\n"


class TestVerifySumsSignature:
    def test_valid_signature_with_primary(self, tmp_path, keys):
        sig = _signed(tmp_path, keys["primary"], SUMS)
        assert len(sig) == 64
        pubs = (keys["primary"].public_pem, keys["backup"].public_pem)
        assert verify_sums_signature(SUMS, sig, pubs) == 0

    def test_valid_signature_with_backup(self, tmp_path, keys):
        """鍵の入れ替え中は 2 本目の鍵で署名した版も通る"""
        sig = _signed(tmp_path, keys["backup"], SUMS)
        pubs = (keys["primary"].public_pem, keys["backup"].public_pem)
        assert verify_sums_signature(SUMS, sig, pubs) == 1

    def test_tampered_sums_rejected(self, tmp_path, keys):
        sig = _signed(tmp_path, keys["primary"], SUMS)
        tampered = SUMS.replace(b"aa", b"ab", 1)
        with pytest.raises(UpdateVerifyError, match="署名"):
            verify_sums_signature(tampered, sig, (keys["primary"].public_pem,))

    def test_other_key_rejected(self, tmp_path, keys):
        sig = _signed(tmp_path, keys["other"], SUMS)
        pubs = (keys["primary"].public_pem, keys["backup"].public_pem)
        with pytest.raises(UpdateVerifyError, match="署名"):
            verify_sums_signature(SUMS, sig, pubs)

    @pytest.mark.parametrize("sig", [b"", b"x" * 63, b"x" * 65, b"\0" * 64])
    def test_missing_or_broken_signature_rejected(self, keys, sig):
        with pytest.raises(UpdateVerifyError):
            verify_sums_signature(SUMS, sig, (keys["primary"].public_pem,))

    def test_no_keys_configured_rejected(self, tmp_path, keys):
        """公開鍵が未設定 (プレースホルダ) のままなら、どんな署名も通さない"""
        sig = _signed(tmp_path, keys["primary"], SUMS)
        with pytest.raises(UpdateVerifyError, match="公開鍵"):
            verify_sums_signature(SUMS, sig, ())

    def test_more_than_two_keys_rejected(self, tmp_path, keys):
        sig = _signed(tmp_path, keys["primary"], SUMS)
        pubs = tuple(k.public_pem for k in keys.values())
        with pytest.raises(UpdateVerifyError, match="2 本"):
            verify_sums_signature(SUMS, sig, pubs)

    def test_non_ed25519_key_rejected(self, tmp_path, keys):
        import subprocess
        rsa = tmp_path / "rsa.pem"
        subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                        "-out", str(rsa)], check=True, capture_output=True)
        pub = subprocess.run(["openssl", "pkey", "-in", str(rsa), "-pubout"], check=True,
                             capture_output=True, text=True).stdout
        sig = _signed(tmp_path, keys["primary"], SUMS)
        with pytest.raises(UpdateVerifyError, match="Ed25519"):
            verify_sums_signature(SUMS, sig, (pub,))

    def test_defaults_to_embedded_keys(self, tmp_path, keys, monkeypatch):
        monkeypatch.setattr(release_keys, "RELEASE_PUBKEYS_PEM", (keys["backup"].public_pem,))
        sig = _signed(tmp_path, keys["backup"], SUMS)
        assert verify_sums_signature(SUMS, sig) == 0


class TestEmbeddedKeys:
    def test_embedded_keys_are_at_most_two_ed25519_public_keys(self):
        pems = release_keys.RELEASE_PUBKEYS_PEM
        assert isinstance(pems, tuple) and len(pems) <= 2
        for pem in pems:
            assert pem.startswith("-----BEGIN PUBLIC KEY-----")
            assert "PRIVATE" not in pem
            update_verify.load_release_pubkeys((pem,))  # Ed25519 として読める


# --- GUI の更新 (updater.fetch_checksums) ---

BASE = "https://github.com/Meku-30/swim-worker/releases/download/v9.9.9"
ASSET = "swim-worker-windows.exe"


class SigFetcher:
    """tests/test_gui_modules.py の FakeFetcher と同じ形 (get_bytes・get_text・stream)"""

    def __init__(self, files: dict):
        self.files = files
        self.requested = []

    def get_bytes(self, url, max_bytes=1 << 20):
        self.requested.append(url)
        name = url.rsplit("/", 1)[-1]
        if name not in self.files:
            raise RuntimeError(f"{name} 取得失敗: status=404")
        return self.files[name]

    def get_text(self, url, max_bytes=1 << 20):
        return self.get_bytes(url, max_bytes).decode()

    @contextlib.contextmanager
    def stream(self, url):
        self.requested.append(url)
        data = self.files.get(url.rsplit("/", 1)[-1], b"")
        yield 200, len(data), iter([data])


def _release(tmp_path, key, content: bytes, *, with_sig=True, tag="v9.9.9", header=None):
    if header is None:
        header = f"# swim-worker-release {tag}\n"
    sums = (header + f"{hashlib.sha256(content).hexdigest()}  {ASSET}\n").encode()
    files = {ASSET: content, "SHA256SUMS": sums}
    if with_sig:
        files["SHA256SUMS.sig"] = _signed(tmp_path, key, sums)
    return files


class TestUpdaterVerifiesSignature:
    def test_signed_release_downloads(self, tmp_path, keys, monkeypatch):
        monkeypatch.setattr(release_keys, "RELEASE_PUBKEYS_PEM",
                            (keys["primary"].public_pem, keys["backup"].public_pem))
        content = os.urandom(2 * 1024 * 1024)
        f = SigFetcher(_release(tmp_path, keys["primary"], content))
        dest = tmp_path / "new.exe"
        updater.download_and_verify(f"{BASE}/{ASSET}", dest, fetcher=f)
        assert dest.read_bytes() == content
        # 署名を確かめてから本体を落とす
        names = [u.rsplit("/", 1)[-1] for u in f.requested]
        assert names.index("SHA256SUMS.sig") < names.index(ASSET)

    def test_unsigned_release_is_refused_before_download(self, tmp_path, keys, monkeypatch):
        monkeypatch.setattr(release_keys, "RELEASE_PUBKEYS_PEM", (keys["primary"].public_pem,))
        f = SigFetcher(_release(tmp_path, keys["primary"], os.urandom(2 * 1024 * 1024),
                                with_sig=False))
        with pytest.raises(Exception, match="SHA256SUMS.sig"):
            updater.download_and_verify(f"{BASE}/{ASSET}", tmp_path / "n.exe", fetcher=f)
        assert not any(u.endswith(ASSET) for u in f.requested)
        assert list(tmp_path.glob("n.exe*")) == []

    def test_wrong_key_is_refused_before_download(self, tmp_path, keys, monkeypatch):
        monkeypatch.setattr(release_keys, "RELEASE_PUBKEYS_PEM", (keys["primary"].public_pem,))
        f = SigFetcher(_release(tmp_path, keys["other"], os.urandom(2 * 1024 * 1024)))
        with pytest.raises(UpdateVerifyError, match="署名"):
            updater.download_and_verify(f"{BASE}/{ASSET}", tmp_path / "n.exe", fetcher=f)
        assert not any(u.endswith(ASSET) for u in f.requested)

    def test_tampered_sums_is_refused(self, tmp_path, keys, monkeypatch):
        monkeypatch.setattr(release_keys, "RELEASE_PUBKEYS_PEM", (keys["primary"].public_pem,))
        content = os.urandom(2 * 1024 * 1024)
        files = _release(tmp_path, keys["primary"], content)
        evil = os.urandom(2 * 1024 * 1024)
        files[ASSET] = evil
        files["SHA256SUMS"] = f"{hashlib.sha256(evil).hexdigest()}  {ASSET}\n".encode()
        with pytest.raises(UpdateVerifyError, match="署名"):
            updater.download_and_verify(f"{BASE}/{ASSET}", tmp_path / "n.exe",
                                        fetcher=SigFetcher(files))

    def test_no_embedded_keys_refuses_update(self, tmp_path, keys, monkeypatch):
        monkeypatch.setattr(release_keys, "RELEASE_PUBKEYS_PEM", ())
        f = SigFetcher(_release(tmp_path, keys["primary"], os.urandom(2 * 1024 * 1024)))
        with pytest.raises(UpdateVerifyError, match="公開鍵"):
            updater.download_and_verify(f"{BASE}/{ASSET}", tmp_path / "n.exe", fetcher=f)


class TestReleaseLine:
    """署名した SHA256SUMS の先頭行 `# swim-worker-release vX.Y.Z` で、署名を版 (タグ) に結び付ける。
    古い版の署名済みファイルを新しいタグとして出す (リプレイ・ダウングレード) のを防ぐ"""

    def test_check_release_line(self):
        ok = b"# swim-worker-release v1.2.3\n" + SUMS
        update_verify.check_release_line(ok, "v1.2.3")
        for bad_sums, tag in [
            (SUMS, "v1.2.3"),                                            # 版の行が無い
            (b"# swim-worker-release v1.2.2\n" + SUMS, "v1.2.3"),       # 別の版
            (b"# swim-worker-release v1.2.3 \n" + SUMS, "v1.2.3"),      # 余計な空白
            (b"# swim-worker-release v1.2.30\n" + SUMS, "v1.2.3"),
            (SUMS + b"# swim-worker-release v1.2.3\n", "v1.2.3"),       # 先頭でない
            (b"\xef\xbb\xbf# swim-worker-release v1.2.3\n" + SUMS, "v1.2.3"),
        ]:
            with pytest.raises(UpdateVerifyError, match="版"):
                update_verify.check_release_line(bad_sums, tag)

    def test_parse_skips_release_line(self):
        sums = update_verify.parse_sha256sums("# swim-worker-release v1.2.3\n" + SUMS.decode())
        assert sums == {"swim-worker-windows.exe": "aa" * 32}

    @pytest.fixture
    def embedded(self, keys, monkeypatch):
        monkeypatch.setattr(release_keys, "RELEASE_PUBKEYS_PEM", (keys["primary"].public_pem,))

    def test_old_signed_release_replayed_as_new_tag_is_refused(self, tmp_path, keys, embedded):
        """v1.0.0 の署名済み SHA256SUMS・.sig・exe を v9.9.9 として出しても通さない"""
        f = SigFetcher(_release(tmp_path, keys["primary"], os.urandom(2 * 1024 * 1024),
                                tag="v1.0.0"))
        with pytest.raises(UpdateVerifyError, match="版"):
            updater.download_and_verify(f"{BASE}/{ASSET}", tmp_path / "n.exe", fetcher=f)
        assert not any(u.endswith(ASSET) for u in f.requested)

    def test_missing_release_line_is_refused(self, tmp_path, keys, embedded):
        f = SigFetcher(_release(tmp_path, keys["primary"], os.urandom(2 * 1024 * 1024), header=""))
        with pytest.raises(UpdateVerifyError, match="版"):
            updater.download_and_verify(f"{BASE}/{ASSET}", tmp_path / "n.exe", fetcher=f)

    def test_tampered_release_line_is_refused(self, tmp_path, keys, embedded):
        files = _release(tmp_path, keys["primary"], os.urandom(2 * 1024 * 1024), tag="v1.0.0")
        files["SHA256SUMS"] = files["SHA256SUMS"].replace(b"v1.0.0", b"v9.9.9")
        with pytest.raises(UpdateVerifyError, match="署名"):
            updater.download_and_verify(f"{BASE}/{ASSET}", tmp_path / "n.exe",
                                        fetcher=SigFetcher(files))

    def test_not_newer_than_current_is_refused(self, tmp_path, keys, embedded):
        """自動更新は今の版より新しい版にだけ (正しく署名された古い版へも戻さない)"""
        content = os.urandom(2 * 1024 * 1024)
        for current in ("9.9.9", "10.0.0"):
            f = SigFetcher(_release(tmp_path, keys["primary"], content))
            with pytest.raises(UpdateVerifyError, match="新しく"):
                updater.download_and_verify(f"{BASE}/{ASSET}", tmp_path / "n.exe", fetcher=f,
                                            current_version=current)
            assert not any(u.endswith(ASSET) for u in f.requested)
        f = SigFetcher(_release(tmp_path, keys["primary"], content))
        updater.download_and_verify(f"{BASE}/{ASSET}", tmp_path / "n.exe", fetcher=f,
                                    current_version="9.9.8")

    def test_defaults_to_running_version(self, tmp_path, keys, embedded, monkeypatch):
        monkeypatch.setattr(updater, "__version__", "9.9.9")
        f = SigFetcher(_release(tmp_path, keys["primary"], os.urandom(2 * 1024 * 1024)))
        with pytest.raises(UpdateVerifyError, match="新しく"):
            updater.download_and_verify(f"{BASE}/{ASSET}", tmp_path / "n.exe", fetcher=f)

    def test_tag_in_url_must_be_a_release_tag(self, tmp_path, keys, embedded):
        f = SigFetcher(_release(tmp_path, keys["primary"], os.urandom(2 * 1024 * 1024),
                                tag="latest"))
        with pytest.raises(UpdateVerifyError):
            updater.download_and_verify(
                "https://github.com/Meku-30/swim-worker/releases/latest/download/" + ASSET,
                tmp_path / "n.exe", fetcher=f)
