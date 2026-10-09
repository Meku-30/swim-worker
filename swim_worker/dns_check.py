"""systemd の通信許可で名前解決が塞がれていないかを起動時に確かめる (Linux の install.sh 版)

swim-worker.service は LAN・リンクローカル宛ての通信を閉じ (IPAddressDeny)、install.sh が
インストール・更新の時点の DNS サーバーと Redis だけを drop-in (IPAddressAllow) で通す。
systemd-resolved を使わない機で DNS サーバーの IP が変わると (別のネットワーク・ルーターの交換)、
次の更新まで名前解決できない。IPAddressAllow はポートを区別できず、unit の再読み込みなしに
作り直せないので、ここでは気付けるようにログを出すだけにする (直し方: sudo bash install.sh)。
"""
import ipaddress
import logging
import os
import socket
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# scripts/swim-worker.service の IPAddressDeny と同じ (tests/test_dns_check.py が一致を確かめる)
DENIED_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10",
    "fc00::/7", "fe80::/10",
))
RESOLV_CONF = Path("/etc/resolv.conf")
DROPIN_FILE = Path("/etc/systemd/system/swim-worker.service.d/10-ip-allow.conf")
FIX_HINT = ("ネットワーク (DNS サーバー) を変えた場合は sudo bash install.sh を実行し直して"
            "通信の許可を書き直してください")


def _ip(text: str):
    try:
        return ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return None


def blocked_nameservers(resolv_text: str, dropin_text: str) -> list[str]:
    """resolv.conf の DNS サーバーのうち、IPAddressDeny に入り drop-in で許可されていないもの"""
    allowed = []
    for line in dropin_text.splitlines():
        key, _, value = line.strip().partition("=")
        if key == "IPAddressAllow":
            for v in value.split():
                try:
                    allowed.append(ipaddress.ip_network(v, strict=False))
                except ValueError:
                    pass
    out = []
    for line in resolv_text.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] != "nameserver":
            continue
        ip = _ip(parts[1])
        if ip is None or ip.is_loopback:
            continue
        denied = any(ip.version == n.version and ip in n for n in DENIED_NETWORKS)
        ok = any(ip.version == n.version and ip in n for n in allowed)
        if denied and not ok:
            out.append(str(ip))
    return out


def warn_if_dns_blocked(resolv: Path = RESOLV_CONF, dropin: Path = DROPIN_FILE) -> list[str]:
    """塞がれる DNS サーバーがあれば警告を出して返す (読めなければ何もしない)"""
    try:
        resolv_text = resolv.read_text(encoding="utf-8", errors="replace")
        dropin_text = dropin.read_text(encoding="utf-8", errors="replace") if dropin.exists() else ""
    except OSError:
        return []
    blocked = blocked_nameservers(resolv_text, dropin_text)
    if blocked:
        logger.warning("DNS サーバー %s への通信が許可されていないため、名前解決できない可能性があります。%s",
                       ", ".join(blocked), FIX_HINT)
    return blocked


def check_at_startup() -> None:
    """systemd (install.sh の unit) で動いているときだけ確かめる"""
    if sys.platform.startswith("linux") and os.environ.get("INVOCATION_ID"):
        warn_if_dns_blocked()


_DNS_MESSAGES = ("name resolution", "name or service not known", "nodename nor servname",
                 "no address associated with hostname", "getaddrinfo")


def is_name_resolution_error(exc: BaseException) -> bool:
    """例外 (原因の連鎖も) が名前解決の失敗か"""
    seen = set()
    e: BaseException | None = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, socket.gaierror):
            return True
        if any(m in str(e).lower() for m in _DNS_MESSAGES):
            return True
        e = e.__cause__ or e.__context__
    return False
