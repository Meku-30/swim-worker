"""scripts/install.sh に埋め込んだ Redis CA 証明書が certs.py と一致することを検証"""
import re
from pathlib import Path

from swim_worker.certs import CA_CERT_PEM

INSTALL_SH = Path(__file__).resolve().parent.parent / "scripts" / "install.sh"


def test_install_sh_embeds_same_ca_as_certs_py():
    text = INSTALL_SH.read_text(encoding="utf-8")
    m = re.search(r"<<'CAEOF' \|\| true\n(.*?)\nCAEOF\n", text, re.DOTALL)
    assert m, "install.sh に REDIS_CA_PEM の heredoc がありません"
    assert m.group(1).strip() == CA_CERT_PEM.strip()


def test_install_sh_helper_verifies_tls():
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "ssl.CERT_NONE" not in text, "Redis AUTH を送る接続で証明書検証を無効化してはいけない"
    assert "ssl.create_default_context(cadata=ca_pem)" in text


def test_install_sh_records_failed_version_and_skips_it():
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert ".failed-version" in text
    # ロールバック時に記録
    rollback = text[text.index("# ロールバック"):]
    assert 'write_root_file "${INSTALL_DIR}/.failed-version" "$LATEST_VERSION"' in rollback
    # ガード評価の前でスキップ
    assert ".failed-version" in text[:text.index("--- ガード1")]


def test_install_sh_guard_authenticates_with_acl_username():
    """自動更新ガードは REDIS_USERNAME があれば ACL ユーザーで AUTH する (無ければ従来の default)"""
    text = INSTALL_SH.read_text(encoding="utf-8")
    guard = text[text.index("--- ガード1"):text.index("PYEOF\n)")]
    assert 'env.get("REDIS_USERNAME", "")' in guard
    assert 'cmd("AUTH", username, password)' in guard
    assert 'cmd("AUTH", password)' in guard
    # 新規インストールの .env にも書く
    assert "REDIS_USERNAME='${REDIS_USERNAME}'" in text


def test_install_sh_guard_falls_back_to_github_on_auth_failure():
    """認証失敗・権限不足では自動更新を止めず (GitHub の最新版で更新)、届かない時だけ止める"""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert 'print(f"AUTH_FALLBACK:{e}")' in text
    assert "ERROR:AUTH_FAILED" not in text
    branch = text[text.index("AUTH_FALLBACK:*)"):]
    branch = branch[:branch.index(";;")]
    assert "exit 0" not in branch  # 更新を続ける
    error_branch = text[text.index("            ERROR:*)"):]
    assert "exit 0" in error_branch[:error_branch.index(";;")]  # 疎通できない時は止める


# --- W1-4 / W1-9 ---------------------------------------------------------

import subprocess


def _bash_function(name: str) -> str:
    """install.sh から関数定義を 1 つ取り出す (関数は `name() {` で始まり行頭の `}` で終わる)"""
    text = INSTALL_SH.read_text(encoding="utf-8")
    m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.DOTALL | re.MULTILINE)
    assert m, f"install.sh に関数 {name} がありません"
    return m.group(0)


def _run_bash(script: str) -> subprocess.CompletedProcess:
    prelude = 'die() { echo "DIE: $*" >&2; exit 1; }\nlog() { :; }\nwarn() { :; }\n'
    return subprocess.run(["bash", "-c", prelude + script], capture_output=True, text=True, timeout=30)


def test_validate_tag_accepts_only_release_tags():
    fn = _bash_function("validate_tag")
    for ok in ("v1.2.3", "v10.0.12"):
        assert _run_bash(fn + f'validate_tag "{ok}" strict').returncode == 0, ok
    # RELEASE_TAG (手動) は rc 付きも可
    assert _run_bash(fn + 'validate_tag "v1.0.0-rc1" manual').returncode == 0
    for bad in ("", "latest", "1.2.3", "v1.2", "v1.2.3-rc1", "v1.2.3/../x", "v1.2.3 ", "v1.2.3;rm"):
        r = _run_bash(fn + f'validate_tag "{bad}" strict')
        assert r.returncode != 0, bad
    for bad in ("", "../x", "v1.0.0/../../x", "v1 0"):
        assert _run_bash(fn + f'validate_tag "{bad}" manual').returncode != 0, bad


def test_downloads_are_pinned_to_the_tag():
    """latest/download はタグの確認と実際のダウンロードの間に別の版へ変わりうるので使わない"""
    text = INSTALL_SH.read_text(encoding="utf-8")
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert "releases/latest/download" not in code
    assert 'DOWNLOAD_BASE="https://github.com/${REPO}/releases/download"' in code
    assert '"${DOWNLOAD_BASE}/${TAG}"' in code
    assert '"${DOWNLOAD_BASE}/${LATEST_TAG}"' in code
    # タグが取れなければ失敗する
    assert re.search(r'validate_tag "\$LATEST_TAG" strict \|\| die', code)


def test_rollback_uses_startup_marker():
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "NRestarts" not in text
    assert "data/.startup_ok" in text
    fn = _bash_function("wait_for_startup")
    assert "systemctl is-active" in fn
    auto = text[text.index("自動更新: v${CURRENT_VERSION}"):text.index("# 通常モード: フルインストール")]
    # 再起動の前にマーカーを消し、新版がマーカーを書くのを待つ
    assert auto.index('rm -f "$STARTUP_MARKER"') < auto.index("systemctl restart swim-worker.service")
    # 旧版に戻しても起動しない (Redis 停止など環境の問題) ときは .failed-version を書かない
    rollback = auto[auto.index("ロールバック実行"):]
    failed_write = rollback.index('write_root_file "${INSTALL_DIR}/.failed-version"')
    assert "wait_for_startup" in rollback[:failed_write]


def test_install_dir_is_root_owned_and_only_data_is_service_user():
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert 'chown -R "$SERVICE_USER:$SERVICE_USER" "${INSTALL_DIR}"\n' not in text
    assert '-o "$SERVICE_USER"' not in text  # バイナリはサービスユーザーの持ち物にしない
    fn = _bash_function("fix_permissions")
    assert 'chown root:root "$INSTALL_DIR"' in fn
    assert 'chown -hR "$SERVICE_USER:$SERVICE_USER" "${INSTALL_DIR}/data"' in fn
    assert 'chown root:"$SERVICE_USER" "${INSTALL_DIR}/.env"' in fn and "chmod 0640" in fn
    assert "chown -h" in fn  # シンボリックリンクの先を変えない
    # 既存インストールの更新 (--auto) でも移行する
    auto = text[text.index("自動更新: v${CURRENT_VERSION}"):text.index("# 通常モード: フルインストール")]
    assert "fix_permissions" in auto
    normal = text[text.index("# 通常モード: フルインストール"):]
    assert "fix_permissions" in normal


def test_ip_allow_dropin_content(tmp_path):
    fn = _bash_function("ip_allow_dropin_content")
    resolv = tmp_path / "resolv.conf"
    resolv.write_text(
        "# comment\nnameserver 192.0.2.53\nnameserver fe80::1%eth0\n"
        "nameserver 127.0.0.53\nsearch example\nnameserver bad;host\n")
    r = _run_bash(fn + f'ip_allow_dropin_content "{resolv}" ""')
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert "[Service]" in lines
    assert "IPAddressAllow=192.0.2.53" in lines
    assert "IPAddressAllow=fe80::1" in lines
    assert not any("bad" in l for l in lines)
    assert not any("127.0.0.53" in l for l in lines)  # ループバックはもともと閉じていない
    # Redis のホスト (IP 直書き) も通す
    r = _run_bash(fn + f'ip_allow_dropin_content "{resolv}" "100.64.1.2"')
    assert "IPAddressAllow=100.64.1.2" in r.stdout.splitlines()
    # resolv.conf が無くても失敗しない
    r = _run_bash(fn + f'ip_allow_dropin_content "{tmp_path}/none" ""')
    assert r.returncode == 0 and "[Service]" in r.stdout


def test_dropin_written_in_both_modes():
    text = INSTALL_SH.read_text(encoding="utf-8")
    auto = text[text.index("自動更新: v${CURRENT_VERSION}"):text.index("# 通常モード: フルインストール")]
    normal = text[text.index("# 通常モード: フルインストール"):]
    assert "write_ip_allow_dropin" in auto and "write_ip_allow_dropin" in normal


def test_fix_permissions_commands(tmp_path):
    """fix_permissions が data/ 以外を root に、data/ をサービスユーザーに、.env を 640 にする"""
    inst = tmp_path / "opt"
    (inst / "data").mkdir(parents=True)
    for name in ("swim-worker", "swim-worker.old", ".env", ".version"):
        (inst / name).write_text("x")
    (inst / "data" / ".startup_ok").write_text("x")
    fn = _bash_function("fix_permissions")
    stub = 'chown() { echo "chown $*"; }\nchmod() { echo "chmod $*"; }\n'
    r = _run_bash(stub + f'INSTALL_DIR="{inst}"\nSERVICE_USER=swim-worker\n' + fn + "fix_permissions")
    assert r.returncode == 0, r.stderr
    out = r.stdout.splitlines()
    assert f"chown root:root {inst}" in out
    assert f"chmod 0755 {inst}" in out
    for name in ("swim-worker", "swim-worker.old", ".env", ".version"):
        assert f"chown -h root:root {inst}/{name}" in out, name
    assert f"chown -h root:root {inst}/data" not in out
    assert f"chown -hR swim-worker:swim-worker {inst}/data" in out
    assert f"chmod 0750 {inst}/data" in out
    assert f"chown root:swim-worker {inst}/.env" in out
    assert f"chmod 0640 {inst}/.env" in out
    # .env がシンボリックリンクなら止める
    (inst / ".env").unlink()
    (inst / ".env").symlink_to(tmp_path / "elsewhere")
    r = _run_bash(stub + f'INSTALL_DIR="{inst}"\nSERVICE_USER=swim-worker\n' + fn + "fix_permissions")
    assert r.returncode != 0 and "DIE" in r.stderr
