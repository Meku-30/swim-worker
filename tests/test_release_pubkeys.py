"""公開鍵の埋め込み (scripts/release_pubkeys.py・set-release-pubkeys.sh) とリリース CI の関門"""
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.signing_helpers import make_key

ROOT = Path(__file__).resolve().parent.parent
FILES = ("scripts/install.sh", "scripts/swim-worker-update.sh", "swim_worker/release_keys.py",
         "scripts/release_pubkeys.py", "scripts/set-release-pubkeys.sh")

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl が要る")


def _tool(root: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["python3", str(root / "scripts" / "release_pubkeys.py"), *args,
                           "--root", str(root)], capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    """埋め込み先と道具だけを写した、公開鍵が未設定 (プレースホルダ) の作業用コピー"""
    r = tmp_path / "repo"
    for rel in FILES:
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, r / rel)
    (r / "scripts" / "release_pubkeys").mkdir()
    assert _tool(r, "sync").returncode == 0  # 未設定の状態に戻す
    return r


def _set(repo: Path, *pubs: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(repo / "scripts" / "set-release-pubkeys.sh"), *map(str, pubs)],
                          capture_output=True, text=True)


def test_repository_embeds_are_consistent():
    """リポジトリの 3 か所の埋め込みが scripts/release_pubkeys/ と一致する"""
    r = _tool(ROOT, "check")
    assert r.returncode == 0, r.stderr


def test_placeholder_fails_release_check(repo):
    """公開鍵が未設定のままだと、リリース CI の関門 (check --require) が失敗する"""
    r = _tool(repo, "check", "--require")
    assert r.returncode != 0 and "未設定" in r.stderr
    assert _tool(repo, "check").returncode == 0  # ふつうのテストは通る


def test_set_pubkeys_embeds_into_all_three(repo, tmp_path):
    k1 = make_key(tmp_path / "k", "one")
    r = _set(repo, k1.public)
    assert r.returncode == 0, r.stderr
    assert _tool(repo, "check", "--require").returncode == 0
    for rel in ("scripts/install.sh", "scripts/swim-worker-update.sh", "swim_worker/release_keys.py"):
        assert k1.public_pem.strip() in (repo / rel).read_text(), rel
    # Python の埋め込みはそのまま読める
    ns: dict = {}
    exec((repo / "swim_worker/release_keys.py").read_text(), ns)
    assert ns["RELEASE_PUBKEYS_PEM"] == (k1.public_pem,)
    # シェルの埋め込みも heredoc として読める
    sh = (repo / "scripts/install.sh").read_text()
    block = sh[sh.index("# BEGIN RELEASE PUBKEYS"):sh.index("# END RELEASE PUBKEYS")]
    out = subprocess.run(["bash", "-c", block + 'printf %s "$RELEASE_PUBKEYS_PEM"'],
                         capture_output=True, text=True).stdout
    assert out.strip() == k1.public_pem.strip()


def test_two_keys_for_rotation(repo, tmp_path):
    k1, k2 = make_key(tmp_path / "k", "one"), make_key(tmp_path / "k", "two")
    assert _set(repo, k1.public, k2.public).returncode == 0
    ns: dict = {}
    exec((repo / "swim_worker/release_keys.py").read_text(), ns)
    assert ns["RELEASE_PUBKEYS_PEM"] == (k1.public_pem, k2.public_pem)
    # 1 本に戻せる
    assert _set(repo, k2.public).returncode == 0
    exec((repo / "swim_worker/release_keys.py").read_text(), ns)
    assert ns["RELEASE_PUBKEYS_PEM"] == (k2.public_pem,)


def test_rejects_private_key_non_ed25519_and_three_keys(repo, tmp_path):
    k = make_key(tmp_path / "k", "one")
    r = _set(repo, k.private)
    assert r.returncode != 0 and "秘密鍵" in r.stderr
    rsa = tmp_path / "rsa.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                    "-out", str(rsa)], check=True, capture_output=True)
    rsa_pub = tmp_path / "rsa.pub.pem"
    subprocess.run(["openssl", "pkey", "-in", str(rsa), "-pubout", "-out", str(rsa_pub)],
                   check=True, capture_output=True)
    r = _set(repo, rsa_pub)
    assert r.returncode != 0 and "Ed25519" in r.stderr
    assert _set(repo, k.public, k.public, k.public).returncode != 0
    # 失敗しても埋め込みは未設定のまま
    assert _tool(repo, "check", "--require").returncode != 0


def test_hand_edited_embed_is_detected(repo, tmp_path):
    k1, k2 = make_key(tmp_path / "k", "one"), make_key(tmp_path / "k", "two")
    assert _set(repo, k1.public).returncode == 0
    p = repo / "scripts/swim-worker-update.sh"
    p.write_text(p.read_text().replace(k1.public_pem, k2.public_pem))
    r = _tool(repo, "check")
    assert r.returncode != 0 and "swim-worker-update.sh" in r.stderr


# --- リリース CI ---------------------------------------------------------------

def _workflow(name: str) -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))


def test_release_ci_requires_pubkeys_and_tests_before_build():
    wf = _workflow("build-release.yml")
    jobs = wf["jobs"]
    gate = [n for n, j in jobs.items()
            if any("release_pubkeys.py check --require" in (s.get("run") or "") for s in j["steps"])]
    assert gate, "公開鍵が未設定なら失敗する関門がない"
    test_jobs = [n for n, j in jobs.items()
                 if any("pytest" in (s.get("run") or "") for s in j["steps"])]
    assert test_jobs
    needs = jobs["build"].get("needs", [])
    needs = [needs] if isinstance(needs, str) else needs
    assert set(gate) <= set(needs) and set(test_jobs) <= set(needs)
