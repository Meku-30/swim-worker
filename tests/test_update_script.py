"""固定の更新スクリプト (scripts/swim-worker-update.sh) と、install.sh の署名の検証

署名と検証の往復は openssl で行う (鍵はテストの中で一時的に作る)。
"""
import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from swim_worker.certs import CA_CERT_PEM
from tests.signing_helpers import make_key, sign

ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = ROOT / "scripts" / "install.sh"
UPDATE_SH = ROOT / "scripts" / "swim-worker-update.sh"
UPDATE_SERVICE = ROOT / "scripts" / "swim-worker-update.service"

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl が要る")

SHARED_FUNCTIONS = ("fetch_latest_tag", "validate_tag", "openssl_version_ok", "require_openssl",
                    "verify_sums_signature", "check_release_line")


def _function(path: Path, name: str) -> str:
    text = path.read_text(encoding="utf-8")
    m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.DOTALL | re.MULTILINE)
    assert m, f"{path.name} に関数 {name} がありません"
    return m.group(0)


def _run(script: str, **env) -> subprocess.CompletedProcess:
    prelude = 'set -uo pipefail\ndie() { echo "DIE: $*" >&2; exit 1; }\nlog() { :; }\nwarn() { :; }\n'
    return subprocess.run(["bash", "-c", prelude + script], capture_output=True, text=True,
                          timeout=60, env={"PATH": "/usr/bin:/bin", **env})


def _verify_fn() -> str:
    return _function(UPDATE_SH, "verify_sums_signature") + _function(UPDATE_SH, "check_release_line")


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    d = tmp_path_factory.mktemp("keys")
    return {name: make_key(d, name) for name in ("primary", "second", "other")}


def _sums(tmp_path: Path, tag: str, files: dict[str, bytes], header: str | None = None) -> Path:
    if header is None:
        header = f"# swim-worker-release {tag}\n"
    lines = [header] + [f"{hashlib.sha256(b).hexdigest()}  {n}\n" for n, b in files.items()]
    p = tmp_path / "SHA256SUMS"
    p.write_text("".join(lines))
    return p


# --- 共通 --------------------------------------------------------------------

@pytest.mark.parametrize("name", SHARED_FUNCTIONS)
def test_shared_functions_are_identical(name):
    """install.sh と固定の更新スクリプトの検証まわりは同じ実装"""
    assert _function(INSTALL_SH, name) == _function(UPDATE_SH, name)


def test_update_script_embeds_same_ca_as_certs_py():
    text = UPDATE_SH.read_text(encoding="utf-8")
    m = re.search(r"<<'CAEOF' \|\| true\n(.*?)\nCAEOF\n", text, re.DOTALL)
    assert m, "REDIS_CA_PEM の heredoc がありません"
    assert m.group(1).strip() == CA_CERT_PEM.strip()


def test_update_script_guard_verifies_tls_and_uses_acl_username():
    text = UPDATE_SH.read_text(encoding="utf-8")
    assert "ssl.CERT_NONE" not in text
    assert "ssl.create_default_context(cadata=ca_pem)" in text
    assert 'cmd("AUTH", username, password)' in text and 'cmd("AUTH", password)' in text


def test_update_script_auth_fallback_continues_and_errors_stop():
    """認証失敗は GitHub の最新版 (署名を確認) で続け、届かない時だけ止める"""
    text = UPDATE_SH.read_text(encoding="utf-8")
    branch = text[text.index("    AUTH_FALLBACK:*)"):]
    branch = branch[:branch.index(";;")]
    assert "exit 0" not in branch and "AUTH_BROKEN=1" in branch
    err = text[text.index("    ERROR:*)"):]
    assert "exit 0" in err[:err.index(";;")]


def test_update_script_checks_guards_before_downloading():
    """一時停止・段階配布・メジャー版・opt-out・ロールバック済みの版はダウンロードの前に確かめる"""
    code = UPDATE_SH.read_text(encoding="utf-8")
    first_download = code.index('"${BASE}/${f}"')
    for marker in (".no-auto-update", "FAILED_VERSION_FILE", "GUARD_RESULT=$(",
                   "メジャーバージョン変更", "sort -V"):
        assert code.index(marker) < first_download, marker
    verify = code.index('verify_sums_signature "${WORK}/SHA256SUMS"')
    line = code.index('check_release_line "${WORK}/SHA256SUMS" "$LATEST_TAG"')
    get_install = code.index('"${BASE}/install.sh"')
    run_install = code.index('bash "${WORK}/install.sh" --auto')
    assert first_download < verify < line < get_install < run_install
    assert 'SWIM_UPDATE_TAG="$LATEST_TAG"' in code
    assert "releases/latest/download" not in code
    assert "require_openssl" in code[:first_download]


def test_update_script_passes_shellcheck():
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck が無い")
    r = subprocess.run(["shellcheck", str(UPDATE_SH), str(INSTALL_SH)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


# --- openssl の版 -------------------------------------------------------------

@pytest.mark.parametrize("v,ok", [
    ("OpenSSL 3.0.13 30 Jan 2024", True),
    ("OpenSSL 3.5.0 8 Apr 2025", True),
    ("OpenSSL 1.1.1w  11 Sep 2023", True),
    ("OpenSSL 1.1.1  11 Sep 2018", True),
    ("OpenSSL 1.1.0l  10 Sep 2019", False),
    ("OpenSSL 1.0.2u  20 Dec 2019", False),
    ("LibreSSL 3.3.6", False),
    ("", False),
])
def test_openssl_version_ok(v, ok):
    r = _run(_function(UPDATE_SH, "openssl_version_ok") + f'openssl_version_ok "{v}"')
    assert (r.returncode == 0) is ok


def test_require_openssl_fails_without_openssl(tmp_path):
    (tmp_path / "bin").mkdir()
    r = subprocess.run(["/bin/bash", "-c", 'die() { echo "DIE: $*" >&2; exit 1; }\n'
                        + _function(UPDATE_SH, "require_openssl") + "require_openssl"],
                       capture_output=True, text=True, env={"PATH": str(tmp_path / "bin")})
    assert r.returncode != 0 and "openssl" in r.stderr


# --- 署名と検証の往復 (openssl) ------------------------------------------------

def _pem_env(*keys) -> dict:
    return {"RELEASE_PUBKEYS_PEM": "".join(k.public_pem for k in keys)}


def _verify(sums: Path, sig: Path, *keys, tag="v1.2.3") -> subprocess.CompletedProcess:
    return _run(_verify_fn() + f'verify_sums_signature "{sums}" "{sig}" && '
                f'check_release_line "{sums}" "{tag}"', **_pem_env(*keys))


class TestShellVerify:
    def test_primary_key(self, tmp_path, keys):
        sums = _sums(tmp_path, "v1.2.3", {"install.sh": b"x"})
        sign(keys["primary"], sums, tmp_path / "sig")
        r = _verify(sums, tmp_path / "sig", keys["primary"], keys["second"])
        assert r.returncode == 0, r.stderr

    def test_second_embedded_key(self, tmp_path, keys):
        """鍵の入れ替え用に 2 本目の鍵でも通る"""
        sums = _sums(tmp_path, "v1.2.3", {"install.sh": b"x"})
        sign(keys["second"], sums, tmp_path / "sig")
        assert _verify(sums, tmp_path / "sig", keys["primary"], keys["second"]).returncode == 0

    def test_tampered(self, tmp_path, keys):
        sums = _sums(tmp_path, "v1.2.3", {"install.sh": b"x"})
        sign(keys["primary"], sums, tmp_path / "sig")
        sums.write_text(sums.read_text().replace("install.sh", "install.sh "))
        r = _verify(sums, tmp_path / "sig", keys["primary"])
        assert r.returncode != 0 and "署名" in r.stderr

    def test_other_key(self, tmp_path, keys):
        sums = _sums(tmp_path, "v1.2.3", {"install.sh": b"x"})
        sign(keys["other"], sums, tmp_path / "sig")
        assert _verify(sums, tmp_path / "sig", keys["primary"]).returncode != 0

    def test_unsigned(self, tmp_path, keys):
        sums = _sums(tmp_path, "v1.2.3", {"install.sh": b"x"})
        r = _verify(sums, tmp_path / "missing", keys["primary"])
        assert r.returncode != 0 and "署名のない" in r.stderr
        (tmp_path / "empty").write_bytes(b"")
        assert _verify(sums, tmp_path / "empty", keys["primary"]).returncode != 0
        (tmp_path / "short").write_bytes(b"x" * 63)
        assert _verify(sums, tmp_path / "short", keys["primary"]).returncode != 0

    def test_no_embedded_keys(self, tmp_path, keys):
        sums = _sums(tmp_path, "v1.2.3", {"install.sh": b"x"})
        sign(keys["primary"], sums, tmp_path / "sig")
        r = _verify(sums, tmp_path / "sig")
        assert r.returncode != 0 and "公開鍵" in r.stderr

    def test_old_release_replayed_as_new_tag(self, tmp_path, keys):
        """古い版の署名済み SHA256SUMS を新しいタグとして出しても通さない"""
        sums = _sums(tmp_path, "v1.0.0", {"install.sh": b"old"})
        sign(keys["primary"], sums, tmp_path / "sig")
        r = _verify(sums, tmp_path / "sig", keys["primary"], tag="v1.2.3")
        assert r.returncode != 0 and "版の行" in r.stderr

    @pytest.mark.parametrize("header", ["", "# swim-worker-release v1.2.3 \n",
                                        "# swim-worker-release v1.2.30\n",
                                        "# swim-worker-release v1.2.3\r\n"])
    def test_bad_release_line(self, tmp_path, keys, header):
        sums = _sums(tmp_path, "v1.2.3", {"install.sh": b"x"}, header=header)
        sign(keys["primary"], sums, tmp_path / "sig")
        assert _verify(sums, tmp_path / "sig", keys["primary"]).returncode != 0

    def test_python_and_shell_agree(self, tmp_path, keys):
        """同じ署名が Python (GUI) でも通る"""
        pytest.importorskip("cryptography")
        from swim_worker.update_verify import check_release_line, verify_sums_signature
        sums = _sums(tmp_path, "v1.2.3", {"install.sh": b"x"})
        sign(keys["primary"], sums, tmp_path / "sig")
        assert verify_sums_signature(sums.read_bytes(), (tmp_path / "sig").read_bytes(),
                                     (keys["primary"].public_pem,)) == 0
        check_release_line(sums.read_bytes(), "v1.2.3")


# --- install.sh の download_and_verify (curl を差し替えてローカルのリリースから取る) ---

FAKE_CURL = r'''
curl() {
    local out="" url="" a
    while (( $# )); do
        case "$1" in
            -o) out="$2"; shift 2 ;;
            --proto|--max-filesize) shift 2 ;;
            -*) shift ;;
            *) url="$1"; shift ;;
        esac
    done
    echo "$url" >> "$CURL_LOG"
    local src="${RELEASES}/${url#https://github.com/Meku-30/swim-worker/releases/download/}"
    [[ -f "$src" ]] || return 22
    cp "$src" "$out"
}
'''


def _install_download(tmp_path, keys_embedded, tag, files):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    log = tmp_path / "curl.log"
    script = (FAKE_CURL + _verify_fn() + _function(INSTALL_SH, "download_and_verify")
              + f'download_and_verify "https://github.com/Meku-30/swim-worker/releases/download/{tag}" '
              f'"{work}" ' + " ".join(files))
    r = _run(script, RELEASES=str(tmp_path / "releases"), CURL_LOG=str(log),
             **_pem_env(*keys_embedded))
    return r, (log.read_text().splitlines() if log.exists() else [])


def _publish(tmp_path, key, tag, files: dict[str, bytes], sums_tag=None, signed=True):
    d = tmp_path / "releases" / tag
    d.mkdir(parents=True)
    for n, b in files.items():
        (d / n).write_bytes(b)
    sums = _sums(d, sums_tag or tag, files)
    if signed:
        sign(key, sums, d / "SHA256SUMS.sig")


class TestInstallShDownload:
    FILES = {"swim-worker-linux-amd64": b"bin" * 100, "swim-worker-update.sh": b"#!/bin/bash\n"}

    def test_signed_release(self, tmp_path, keys):
        _publish(tmp_path, keys["primary"], "v1.2.3", self.FILES)
        r, log = _install_download(tmp_path, [keys["primary"]], "v1.2.3", self.FILES)
        assert r.returncode == 0, r.stderr
        # 先に SHA256SUMS と署名を取り、確かめてから本体
        assert [u.rsplit("/", 1)[-1] for u in log[:2]] == ["SHA256SUMS", "SHA256SUMS.sig"]

    def test_unsigned_release_stops_before_payload(self, tmp_path, keys):
        _publish(tmp_path, keys["primary"], "v1.2.3", self.FILES, signed=False)
        r, log = _install_download(tmp_path, [keys["primary"]], "v1.2.3", self.FILES)
        assert r.returncode != 0 and "DIE" in r.stderr
        assert not any(u.endswith("swim-worker-linux-amd64") for u in log)

    def test_wrong_key_stops_before_payload(self, tmp_path, keys):
        _publish(tmp_path, keys["other"], "v1.2.3", self.FILES)
        r, log = _install_download(tmp_path, [keys["primary"]], "v1.2.3", self.FILES)
        assert r.returncode != 0
        assert not any(u.endswith("swim-worker-linux-amd64") for u in log)

    def test_old_release_replayed_under_new_tag(self, tmp_path, keys):
        _publish(tmp_path, keys["primary"], "v1.2.3", self.FILES, sums_tag="v1.0.0")
        r, log = _install_download(tmp_path, [keys["primary"]], "v1.2.3", self.FILES)
        assert r.returncode != 0 and "v1.2.3 のものではない" in r.stderr
        assert not any(u.endswith("swim-worker-linux-amd64") for u in log)

    def test_payload_hash_mismatch(self, tmp_path, keys):
        _publish(tmp_path, keys["primary"], "v1.2.3", self.FILES)
        (tmp_path / "releases" / "v1.2.3" / "swim-worker-linux-amd64").write_bytes(b"evil")
        r, _ = _install_download(tmp_path, [keys["primary"]], "v1.2.3", self.FILES)
        assert r.returncode != 0 and "SHA256 不一致" in r.stderr


# --- install.sh の --auto (移行・適用) ------------------------------------------

def test_install_sh_auto_without_tag_migrates_to_update_script():
    """旧 update.service (最新の install.sh を --auto で実行) から: 更新スクリプトと unit を
    署名を確かめて置き、更新スクリプトに任せる"""
    text = INSTALL_SH.read_text(encoding="utf-8")
    auto = text[text.index("if [[ $AUTO_MODE -eq 1 ]]; then"):text.index("# 通常モード: フルインストール")]
    mig = auto[auto.index('if [[ -z "${SWIM_UPDATE_TAG:-}" ]]; then'):]
    mig = mig[:mig.index("\n    fi\n")]
    assert '.no-auto-update' in mig
    assert 'download_and_verify "${DOWNLOAD_BASE}/${LATEST_TAG}" "$TMPDIR" "${UPDATE_FILES[@]}"' in mig
    assert mig.index("download_and_verify") < mig.index("install_update_files") \
        < mig.index('exec "$UPDATER_PATH"')
    assert "exec 9>&-" in mig  # ロックを手放してから (更新スクリプトが呼ぶ install.sh が取る)
    # 適用 (タグあり) は今の版より新しいときだけ
    apply = auto[auto.index('LATEST_TAG="$SWIM_UPDATE_TAG"'):]
    assert apply.index("より新しくないため skip") < apply.index("download_and_verify")
    assert '"$BINARY_NAME" "${UPDATE_FILES[@]}"' in apply
    assert "install_update_files" in apply


def test_update_files_include_update_script():
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert ("UPDATE_FILES=(swim-worker.service swim-worker-update.service "
            "swim-worker-update.timer swim-worker-update.sh)") in text
    fn = _function(INSTALL_SH, "install_update_files")
    assert 'UPDATER_DIR="/usr/local/libexec/swim-worker"' in text
    assert "install -m 0755 -o root -g root" in fn and 'mv -f "${UPDATER_PATH}.new"' in fn
    assert 'chown root:root "$UPDATER_DIR"' in fn


def test_install_update_files_places_files(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for n in ("swim-worker.service", "swim-worker-update.service", "swim-worker-update.timer",
              "swim-worker-update.sh"):
        (src / n).write_text(n)
    etc = tmp_path / "etc"
    etc.mkdir()
    lib = tmp_path / "libexec" / "swim-worker"
    stub = ('install() { local a=(); while (( $# )); do case "$1" in -m|-o|-g) shift 2;; '
            '*) a+=("$1"); shift;; esac; done; cp "${a[0]}" "${a[1]}"; }\n'
            'chown() { :; }\n')
    script = (stub + f'SERVICE_FILE="{etc}/a.service"\nUPDATE_SERVICE_FILE="{etc}/b.service"\n'
              f'UPDATE_TIMER_FILE="{etc}/c.timer"\nUPDATER_DIR="{lib}"\n'
              f'UPDATER_PATH="{lib}/update.sh"\n'
              + _function(INSTALL_SH, "install_update_files") + f'install_update_files "{src}"')
    r = _run(script)
    assert r.returncode == 0, r.stderr
    assert (lib / "update.sh").read_text() == "swim-worker-update.sh"
    assert not (lib / "update.sh.new").exists()
    assert (etc / "b.service").read_text() == "swim-worker-update.service"


# --- update.service ----------------------------------------------------------

def _unit(path: Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z]+)=(.*)$", line.strip())
        if m:
            out.setdefault(m.group(1), []).append(m.group(2))
    return out


def test_update_service_runs_fixed_script_with_hardening():
    u = _unit(UPDATE_SERVICE)
    assert u["ExecStart"] == ["/usr/local/libexec/swim-worker/update.sh"]
    assert "releases/latest/download" not in UPDATE_SERVICE.read_text(encoding="utf-8")
    for k, v in {"ProtectHome": "true", "PrivateTmp": "true", "NoNewPrivileges": "true",
                 "ProtectSystem": "full", "ProtectKernelModules": "true",
                 "SystemCallArchitectures": "native", "RestrictSUIDSGID": "true"}.items():
        assert u.get(k) == [v], k
    rw = " ".join(u["ReadWritePaths"]).split()
    assert "/etc/systemd/system" in rw and "-/usr/local/libexec/swim-worker" in rw


def test_update_service_passes_systemd_analyze(tmp_path):
    if shutil.which("systemd-analyze") is None:
        pytest.skip("systemd-analyze が無い")
    r = subprocess.run(["systemd-analyze", "verify", str(UPDATE_SERVICE)],
                       capture_output=True, text=True)
    # このマシンには /usr/local/libexec/swim-worker/update.sh が無いので、その指摘だけは許す
    problems = [l for l in (r.stderr + r.stdout).splitlines()
                if l.strip() and "is not executable" not in l]
    assert problems == []
