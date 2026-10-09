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


def _guard(fake: str | None, tmp_path) -> subprocess.CompletedProcess:
    """coordinator_guard を、偽の python3 (決まった判定を返す) で動かす"""
    stub = ("" if fake is None else
            f'python3() {{ cat > /dev/null; echo "{fake}"; }}\n')
    path = "/nonexistent" if fake is None else "/usr/bin:/bin"
    return subprocess.run(
        ["/bin/bash", "-c", 'set -uo pipefail\nlog() { echo "LOG: $*"; }\nwarn() { echo "WARN: $*"; }\n'
         + stub + 'INSTALL_DIR="' + str(tmp_path) + '"\nREDIS_CA_PEM=x\n'
         + _function(UPDATE_SH, "coordinator_guard") + "coordinator_guard"],
        capture_output=True, text=True, env={"PATH": path}, timeout=30)


@pytest.mark.parametrize("fake,rc", [
    ("OK", 0),
    ("AUTH_FALLBACK:WRONGPASS", 2),   # 認証失敗は GitHub の最新版 (署名を確認) で続ける
    ("DISABLED", 1),
    ("NOT_IN_WHITELIST:w1", 1),
    ("ERROR:ConnectionRefusedError:x", 1),  # 届かないときは止める
    ("garbage", 1),
    (None, 1),                         # python3 が無ければ確かめられないので止める
])
def test_coordinator_guard_return_codes(fake, rc, tmp_path):
    r = _guard(fake, tmp_path)
    assert r.returncode == rc, r.stdout + r.stderr


def test_update_script_guard_only_mode():
    """--guard-only: 一時停止・段階配布だけを確かめる (install.sh の移行が使う。opt-out は見ない)"""
    code = UPDATE_SH.read_text(encoding="utf-8")
    branch = code[code.index('if [[ "${1:-}" == "--guard-only" ]]; then'):]
    branch = branch[:branch.index("\nfi\n")]
    assert "coordinator_guard" in branch
    assert code.index("--guard-only") < code.index('if [[ -f "${INSTALL_DIR}/.no-auto-update" ]]')
    assert code.index("--guard-only") < code.index('"${BASE}/${f}"')
    assert "exit 0" in branch and "exit 3" in branch
    # 本来の流れも同じ関数で判定する (二重実装しない)
    main = code[code.index("# --- ガード2"):]
    assert "coordinator_guard || guard_rc=$?" in main
    assert code.count("result=$(INSTALL_DIR=") == 1


def test_update_script_checks_guards_before_downloading():
    """一時停止・段階配布・メジャー版・opt-out・ロールバック済みの版はダウンロードの前に確かめる"""
    code = UPDATE_SH.read_text(encoding="utf-8")
    first_download = code.index('"${BASE}/${f}"')
    for marker in (".no-auto-update", "FAILED_VERSION_FILE", "coordinator_guard || guard_rc=$?",
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
    # pkeyutl の -rawin (Ed25519 で SHA256SUMS をそのまま確かめる) は 3.0 から
    ("OpenSSL 3.0.0 7 sep 2021", True),
    ("OpenSSL 1.1.1w  11 Sep 2023", False),
    ("OpenSSL 1.1.1  11 Sep 2018", False),
    ("OpenSSL 1.1.0l  10 Sep 2019", False),
    ("OpenSSL 1.0.2u  20 Dec 2019", False),
    ("LibreSSL 3.3.6", False),
    ("", False),
])
def test_openssl_version_ok(v, ok):
    r = _run(_function(UPDATE_SH, "openssl_version_ok") + f'openssl_version_ok "{v}"')
    assert (r.returncode == 0) is ok


def test_require_openssl_explains_old_openssl(tmp_path):
    """1.1.1 では分かりやすいエラー (3.0 以上が要ること・今の版) で止まる"""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "openssl").write_text('#!/bin/sh\necho "OpenSSL 1.1.1w  11 Sep 2023"\n')
    (bindir / "openssl").chmod(0o755)
    r = subprocess.run(["/bin/bash", "-c", 'die() { echo "DIE: $*" >&2; exit 1; }\n'
                        + _function(UPDATE_SH, "openssl_version_ok")
                        + _function(UPDATE_SH, "require_openssl") + "require_openssl"],
                       capture_output=True, text=True, env={"PATH": f"{bindir}:/usr/bin:/bin"})
    assert r.returncode != 0
    assert "OpenSSL 3.0 以上" in r.stderr and "1.1.1w" in r.stderr


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

def _auto_sections():
    text = INSTALL_SH.read_text(encoding="utf-8")
    auto = text[text.index("if [[ $AUTO_MODE -eq 1 ]]; then"):text.index("# 通常モード: フルインストール")]
    mig = auto[auto.index('if [[ -z "${SWIM_UPDATE_TAG:-}" ]]; then'):]
    mig = mig[:mig.index("\n    fi\n")]
    apply = auto[auto.index('LATEST_TAG="$SWIM_UPDATE_TAG"'):]
    return text, mig, apply


def test_install_sh_auto_without_tag_migrates_to_update_script():
    """旧 update.service (最新の install.sh を --auto で実行) から: 更新スクリプト・update.service・
    timer だけを署名を確かめて置く。本体の unit (IPAddressDeny 付き) は置かない"""
    _, mig, apply = _auto_sections()
    assert 'download_and_verify "${DOWNLOAD_BASE}/${LATEST_TAG}" "$TMPDIR" "${UPDATER_FILES[@]}"' in mig
    assert "install_updater_files" in mig
    for bad in ("install_worker_unit", "SERVICE_FILE", "UPDATE_FILES[@]", "BINARY_NAME"):
        assert bad not in mig, bad
    # 長い処理 (更新スクリプトの実行 = バイナリの更新) はしない。旧 unit の TimeoutStartSec=600 の
    # 下で動いているため。次の timer から新しい update.service (900 秒) で更新する
    assert 'exec "$UPDATER_PATH"' not in mig and "update.sh" not in mig.replace("swim-worker-update.sh", "")
    assert mig.rstrip().endswith("exit 0")
    # 適用 (タグあり) は今の版より新しいときだけ
    assert apply.index("より新しくないため skip") < apply.index("download_and_verify")
    assert '"$BINARY_NAME" "${UPDATE_FILES[@]}"' in apply


def test_migration_goes_through_coordinator_guard():
    """移行も一時停止・段階配布を通ってから。判定は新しい更新スクリプトの --guard-only (同じ実装)。
    opt-out の機も移行する (update.sh がバイナリを更新しない)"""
    _, mig, _ = _auto_sections()
    guard = mig.index('bash "${TMPDIR}/swim-worker-update.sh" --guard-only')
    assert mig.index("download_and_verify") < guard < mig.index("install_updater_files")
    assert ".no-auto-update" not in mig
    after_guard = mig[guard:mig.index("install_updater_files")]
    assert "exit 0" in after_guard  # 止められたら何も置かずに終わる (次回また試す)


def test_update_files_split():
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert ("UPDATER_FILES=(swim-worker-update.service swim-worker-update.timer "
            "swim-worker-update.sh)") in text
    assert 'UPDATE_FILES=(swim-worker.service "${UPDATER_FILES[@]}")' in text
    fn = _function(INSTALL_SH, "install_updater_files")
    assert "SERVICE_FILE\"" not in fn.replace("UPDATE_SERVICE_FILE", "")
    assert 'UPDATER_DIR="/usr/local/libexec/swim-worker"' in text
    assert "install -m 0755 -o root -g root" in fn and 'mv -f "${UPDATER_PATH}.new"' in fn
    assert 'chown root:root "$UPDATER_DIR"' in fn


_INSTALL_STUB = ('install() { local a=(); while (( $# )); do case "$1" in -m|-o|-g) shift 2;; '
                 '*) a+=("$1"); shift;; esac; done; cp "${a[0]}" "${a[1]}"; }\n'
                 'chown() { :; }\n')


def test_install_updater_files_places_files(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for n in ("swim-worker-update.service", "swim-worker-update.timer", "swim-worker-update.sh"):
        (src / n).write_text(n)
    etc = tmp_path / "etc"
    etc.mkdir()
    lib = tmp_path / "libexec" / "swim-worker"
    script = (_INSTALL_STUB + f'SERVICE_FILE="{etc}/a.service"\nUPDATE_SERVICE_FILE="{etc}/b.service"\n'
              f'UPDATE_TIMER_FILE="{etc}/c.timer"\nUPDATER_DIR="{lib}"\n'
              f'UPDATER_PATH="{lib}/update.sh"\n'
              + _function(INSTALL_SH, "install_updater_files") + f'install_updater_files "{src}"')
    r = _run(script)
    assert r.returncode == 0, r.stderr
    assert (lib / "update.sh").read_text() == "swim-worker-update.sh"
    assert not (lib / "update.sh.new").exists()
    assert (etc / "b.service").read_text() == "swim-worker-update.service"
    assert not (etc / "a.service").exists()  # 本体の unit は置かない


# --- 本体の unit と通信許可の drop-in ---------------------------------------------

def _worker_unit_script(tmp_path, dropin_fn: str) -> tuple[str, Path, Path]:
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "swim-worker.service").write_text("IPAddressDeny=10.0.0.0/8\n")
    unit = tmp_path / "etc" / "swim-worker.service"
    dropin = tmp_path / "etc" / "swim-worker.service.d" / "10-ip-allow.conf"
    unit.parent.mkdir(exist_ok=True)
    script = (_INSTALL_STUB + f'SERVICE_FILE="{unit}"\nDROPIN_FILE="{dropin}"\n' + dropin_fn
              + _function(INSTALL_SH, "install_worker_unit") + f'install_worker_unit "{src}"')
    return script, unit, dropin


def test_install_worker_unit_places_dropin_before_unit(tmp_path):
    ok = ('write_ip_allow_dropin() { mkdir -p "$(dirname "$DROPIN_FILE")"; '
          '[[ ! -e "$SERVICE_FILE" ]] || { echo "unit が先" >&2; return 1; }; '
          'echo "[Service]" > "$DROPIN_FILE"; }\n')
    script, unit, dropin = _worker_unit_script(tmp_path, ok)
    r = _run(script)
    assert r.returncode == 0, r.stderr
    assert unit.exists() and dropin.exists()


def test_install_worker_unit_keeps_old_unit_if_dropin_fails(tmp_path):
    """drop-in を書けなければ、IPAddressDeny 付きの unit を置かない"""
    fail = 'write_ip_allow_dropin() { die "drop-in を書けない"; }\n'
    script, unit, dropin = _worker_unit_script(tmp_path, fail)
    unit.write_text("old unit\n")
    r = _run(script)
    assert r.returncode != 0
    assert unit.read_text() == "old unit\n"


def test_worker_unit_is_only_placed_with_dropin():
    """どの経路でも本体の unit は install_worker_unit (drop-in → unit の順) でしか置かない"""
    text = INSTALL_SH.read_text(encoding="utf-8")
    fn = _function(INSTALL_SH, "install_worker_unit")
    assert fn.index("write_ip_allow_dropin") < fn.index('"$SERVICE_FILE"')
    rest = text.replace(fn, "")
    for line in rest.splitlines():
        if line.lstrip().startswith("#"):
            continue
        if re.search(r'\b(install|cp|mv)\b.*"\$SERVICE_FILE"', line):
            raise AssertionError(f"install_worker_unit の外で unit を置いている: {line}")
    _, mig, apply = _auto_sections()
    normal = text[text.index("# 通常モード: フルインストール"):]
    assert "install_worker_unit" in apply and "install_worker_unit" in normal
    assert "install_worker_unit" not in mig


# --- ロールバックで unit・drop-in・更新スクリプトも戻す -------------------------------

def _backup_script(tmp_path) -> tuple[str, dict[str, Path]]:
    files = {n: tmp_path / n for n in ("unit", "dropin", "usvc", "utimer", "update.sh")}
    script = (f'SERVICE_FILE="{files["unit"]}"\nDROPIN_FILE="{files["dropin"]}"\n'
              f'UPDATE_SERVICE_FILE="{files["usvc"]}"\nUPDATE_TIMER_FILE="{files["utimer"]}"\n'
              f'UPDATER_PATH="{files["update.sh"]}"\n'
              + _function(INSTALL_SH, "update_backup_targets")
              + _function(INSTALL_SH, "backup_update_files")
              + _function(INSTALL_SH, "restore_update_files")
              + _function(INSTALL_SH, "drop_update_backups"))
    return script, files


def test_backup_and_restore_update_files(tmp_path):
    script, files = _backup_script(tmp_path)
    for n, p in files.items():
        if n != "dropin":  # v1.2.x には drop-in が無い
            p.write_text(f"old {n}")
    (tmp_path / "unit.old").write_text("前回の残り")
    replace = "".join(f'echo "new" > "{p}"\n' for p in files.values())
    r = _run(script + "backup_update_files\n" + replace + "restore_update_files\n")
    assert r.returncode == 0, r.stderr
    for n, p in files.items():
        if n == "dropin":
            assert not p.exists()  # 元に無かったものは消す (古い unit に drop-in は無い)
        else:
            assert p.read_text() == f"old {n}", n
    assert not list(tmp_path.glob("*.old"))


def test_drop_update_backups(tmp_path):
    script, files = _backup_script(tmp_path)
    for p in files.values():
        p.write_text("old")
    r = _run(script + "backup_update_files\ndrop_update_backups\n")
    assert r.returncode == 0, r.stderr
    assert not list(tmp_path.glob("*.old"))
    assert all(p.read_text() == "old" for p in files.values())


def test_apply_backs_up_and_rollback_restores():
    _, _, apply = _auto_sections()
    backup = apply.index("backup_update_files")
    first_replace = apply.index('install -m 0755 -o root -g root "${TMPDIR}/${BINARY_NAME}"')
    assert apply.index("download_and_verify") < backup < first_replace
    # .version はバイナリの置き換えと同時に (途中で止まっても中身と食い違わない)
    after_bin = apply[first_replace:]
    nxt = after_bin.split("\n")[1:3]
    assert any('write_root_file "$VERSION_FILE" "$LATEST_VERSION"' in l for l in nxt), nxt
    assert after_bin.index('write_root_file "$VERSION_FILE" "$LATEST_VERSION"') \
        < after_bin.index("systemctl restart swim-worker.service")
    rollback = apply[apply.index("ロールバック実行"):]
    restore = rollback.index("restore_update_files")
    assert restore < rollback.index("systemctl daemon-reload") < rollback.index("systemctl restart")
    assert 'write_root_file "$VERSION_FILE" "$CURRENT_VERSION"' in rollback
    # 成功したら退避を消す
    success = apply[:apply.index("ロールバック実行")]
    assert success.count("drop_update_backups") >= 2


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
