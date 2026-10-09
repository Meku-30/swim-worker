"""配布物 (Dockerfile・systemd unit) と certs のテスト"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_certs_has_no_temp_file_helper():
    """埋め込み CA は ssl_ca_data で渡す。一時ファイルに書き出す get_ca_cert_path は使わない"""
    from swim_worker import certs
    assert not hasattr(certs, "get_ca_cert_path")
    assert certs.CA_CERT_PEM.startswith("-----BEGIN CERTIFICATE-----")


def test_dockerfile_healthcheck_uses_embedded_ca_data():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    hc = text[text.index("HEALTHCHECK"):]
    assert "get_ca_cert_path" not in hc
    assert "ssl_ca_data=CA_CERT_PEM" in hc


def _unit(name: str) -> dict[str, list[str]]:
    """unit ファイルを {キー: [値, ...]} にする (コメント・セクション行は除く)"""
    out: dict[str, list[str]] = {}
    for line in (ROOT / "scripts" / name).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "[")):
            continue
        m = re.match(r"^([A-Za-z]+)=(.*)$", line)
        if m:
            out.setdefault(m.group(1), []).append(m.group(2))
    return out


def test_worker_unit_hardening():
    u = _unit("swim-worker.service")
    assert u["UMask"] == ["0077"]
    assert u["SystemCallArchitectures"] == ["native"]
    deny = " ".join(u.get("IPAddressDeny", [])).split()
    for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
                "100.64.0.0/10", "fc00::/7", "fe80::/10"):
        assert net in deny, net
    # ループバックは閉じない (systemd-resolved のスタブ 127.0.0.53 で名前解決するため)
    assert not any(n.startswith("127.") or n == "::1/128" or n == "localhost" for n in deny)


def test_worker_unit_memory_deny_write_execute_comment_is_accurate():
    text = (ROOT / "scripts" / "swim-worker.service").read_text(encoding="utf-8")
    idx = text.index("MemoryDenyWriteExecute=false")
    comment = text[:idx].rsplit("\n\n", 1)[-1]
    # 「禁止」と書いて false にしている矛盾をなくす
    assert "禁止" not in comment.splitlines()[-1]
    assert "無効" in comment
