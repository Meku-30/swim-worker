//! 版の比べ方 (update_check.py parse_version と consumer.py の更新の判定)
pub type Version = (u32, u32, u32);

/// "1.2.3" / "v1.2.3" を (1, 2, 3) に。それ以外 (rc 付き・桁数違い・空白入りなど) は None
pub fn parse_version(s: &str) -> Option<Version> {
    let s = s.strip_prefix('v').unwrap_or(s);
    let parts: Vec<&str> = s.split('.').collect();
    if parts.len() != 3 {
        return None;
    }
    let mut out = [0u32; 3];
    for (i, p) in parts.iter().enumerate() {
        if p.is_empty() || p.len() > 6 || !p.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        out[i] = p.parse().ok()?;
    }
    Some((out[0], out[1], out[2]))
}

pub fn gate_reason_ja(reason: &str) -> &str {
    match reason {
        "major" => "メジャーバージョンの変更は手動で更新してください",
        "disabled" => "管理者が自動更新を止めています",
        "not_in_whitelist" => "段階配布の対象外です",
        "error" => "自動更新の設定を確認できません",
        other => other,
    }
}

/// whitelist (カンマ区切り) の名前の一覧。空白だけなら空
pub fn whitelist_names(v: &str) -> Vec<String> {
    v.split(',').map(str::trim).filter(|x| !x.is_empty()).map(String::from).collect()
}
