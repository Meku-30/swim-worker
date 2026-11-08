//! systemd の通信の許可で名前解決が塞がれていないかを起動時に確かめる (Python の dns_check.py)。
//! install.sh の unit は LAN・リンクローカル宛てを閉じ、インストールの時点の DNS サーバーと Redis だけを
//! drop-in (IPAddressAllow) で通す。DNS サーバーの IP が変わると次の更新まで名前解決できないので、気付けるようにログを出す
use std::net::IpAddr;
use std::path::Path;

use ipnet::IpNet;

/// scripts/swim-worker.service の IPAddressDeny と同じ
pub const DENIED_NETWORKS: &[&str] =
    &["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10", "fc00::/7", "fe80::/10"];
pub const RESOLV_CONF: &str = "/etc/resolv.conf";
pub const DROPIN_FILE: &str = "/etc/systemd/system/swim-worker.service.d/10-ip-allow.conf";
pub const FIX_HINT: &str =
    "ネットワーク (DNS サーバー) を変えた場合は sudo bash install.sh を実行し直して通信の許可を書き直してください";

fn net(s: &str) -> Option<IpNet> {
    s.parse::<IpNet>().ok().or_else(|| s.parse::<IpAddr>().ok().map(IpNet::from)).map(|n| n.trunc())
}

/// resolv.conf の DNS サーバーのうち、IPAddressDeny に入り drop-in で許されていないもの
pub fn blocked_nameservers(resolv: &str, dropin: &str) -> Vec<String> {
    let allowed: Vec<IpNet> = dropin
        .lines()
        .filter_map(|l| l.trim().split_once('='))
        .filter(|(k, _)| *k == "IPAddressAllow")
        .flat_map(|(_, v)| v.split_whitespace().filter_map(net).collect::<Vec<_>>())
        .collect();
    let denied: Vec<IpNet> = DENIED_NETWORKS.iter().filter_map(|n| net(n)).collect();
    let mut out = Vec::new();
    for line in resolv.lines() {
        let parts: Vec<&str> = line.split_whitespace().collect();
        if parts.len() < 2 || parts[0] != "nameserver" {
            continue;
        }
        let Ok(ip) = parts[1].split('%').next().unwrap_or("").parse::<IpAddr>() else { continue };
        if ip.is_loopback() {
            continue;
        }
        if denied.iter().any(|n| n.contains(&ip)) && !allowed.iter().any(|n| n.contains(&ip)) {
            out.push(ip.to_string());
        }
    }
    out
}

pub fn warn_if_dns_blocked(resolv: &Path, dropin: &Path) -> Vec<String> {
    let Ok(r) = std::fs::read_to_string(resolv) else { return Vec::new() };
    let d = std::fs::read_to_string(dropin).unwrap_or_default();
    let b = blocked_nameservers(&r, &d);
    if !b.is_empty() {
        tracing::warn!(
            target: "swim_worker.dns_check",
            "DNS サーバー {} への通信が許可されていないため、名前解決できない可能性があります。{FIX_HINT}",
            b.join(", ")
        );
    }
    b
}

/// systemd (install.sh の unit) で動いているときだけ確かめる
pub fn check_at_startup() {
    if cfg!(target_os = "linux") && std::env::var_os("INVOCATION_ID").is_some() {
        warn_if_dns_blocked(Path::new(RESOLV_CONF), Path::new(DROPIN_FILE));
    }
}

const DNS_MESSAGES: &[&str] =
    &["name resolution", "name or service not known", "nodename nor servname", "no address associated with hostname", "getaddrinfo", "failed to lookup address"];

pub fn is_name_resolution_error(message: &str) -> bool {
    let m = message.to_lowercase();
    DNS_MESSAGES.iter().any(|x| m.contains(x))
}
