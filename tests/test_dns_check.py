"""dns_check: systemd の通信許可 (IPAddressDeny + drop-in) で塞がれる DNS サーバーを起動時に知らせる"""
import logging
import re
import socket
from pathlib import Path

import redis

from swim_worker import dns_check

ROOT = Path(__file__).resolve().parent.parent


def test_denied_networks_match_unit():
    """swim-worker.service の IPAddressDeny と同じ範囲"""
    unit = (ROOT / "scripts" / "swim-worker.service").read_text(encoding="utf-8")
    nets = []
    for line in unit.splitlines():
        m = re.match(r"^IPAddressDeny=(.*)$", line.strip())
        if m:
            nets += m.group(1).split()
    assert sorted(nets) == sorted(str(n) for n in dns_check.DENIED_NETWORKS)


RESOLV = "nameserver 192.0.2.1\nnameserver 10.0.0.1\nnameserver 127.0.0.53\nnameserver fe80::1%eth0\n"


def test_blocked_nameservers():
    dropin = "[Service]\nIPAddressAllow=10.0.0.1\n"
    # 192.0.2.1 は塞がれていない (インターネット側)、127.0.0.53 はループバック
    assert dns_check.blocked_nameservers(RESOLV, dropin) == ["fe80::1"]
    assert dns_check.blocked_nameservers(RESOLV, "") == ["10.0.0.1", "fe80::1"]
    assert dns_check.blocked_nameservers("nameserver bad\n# x\n", "") == []


def test_warn_if_dns_blocked(tmp_path, caplog):
    resolv = tmp_path / "resolv.conf"
    resolv.write_text("nameserver 10.9.8.7\n")
    dropin = tmp_path / "10-ip-allow.conf"
    dropin.write_text("[Service]\nIPAddressAllow=10.9.8.1\n")
    with caplog.at_level(logging.WARNING):
        assert dns_check.warn_if_dns_blocked(resolv, dropin) == ["10.9.8.7"]
    assert "sudo bash install.sh" in caplog.text
    # ファイルが無ければ何もしない
    assert dns_check.warn_if_dns_blocked(tmp_path / "none", dropin) == []


def test_is_name_resolution_error():
    try:
        try:
            raise socket.gaierror(-3, "Temporary failure in name resolution")
        except socket.gaierror as e:
            raise redis.exceptions.ConnectionError("Error -3 connecting") from e
    except redis.exceptions.ConnectionError as e:
        assert dns_check.is_name_resolution_error(e)
    assert dns_check.is_name_resolution_error(
        redis.exceptions.ConnectionError("Error -2 connecting to x:6380. Name or service not known."))
    assert not dns_check.is_name_resolution_error(ConnectionRefusedError("refused"))
