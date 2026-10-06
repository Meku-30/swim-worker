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
    assert 'echo "$LATEST_VERSION" > "${INSTALL_DIR}/.failed-version"' in rollback
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
