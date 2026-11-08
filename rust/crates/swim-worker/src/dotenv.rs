//! `.env` の読み方を python-dotenv (今の Python 版が pydantic-settings 経由で使う) と同じにする。
//! 見本: rust/tests/golden/dotenv_cases.json

/// `.env` の中身を (名前, 値) の並びにする。同じ名前が 2 回あれば後の値 (位置は最初のまま)。
/// `=` のない行・コメント・空行は飛ばす
pub fn parse(text: &str) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = Vec::new();
    for line in text.lines() {
        if let Some((k, v)) = parse_line(line) {
            if let Some(slot) = out.iter_mut().find(|(key, _)| *key == k) {
                slot.1 = v;
            } else {
                out.push((k, v));
            }
        }
    }
    out
}

fn parse_line(line: &str) -> Option<(String, String)> {
    let mut s = line.trim_start_matches([' ', '\t']);
    if s.is_empty() || s.starts_with('#') {
        return None;
    }
    if let Some(rest) = s.strip_prefix("export") {
        if rest.starts_with([' ', '\t']) {
            s = rest.trim_start_matches([' ', '\t']);
        }
    }
    let (key, rest) = if let Some(q) = s.strip_prefix('\'') {
        let end = q.find('\'')?;
        (q[..end].to_string(), &q[end + 1..])
    } else {
        let end = s.find(|c: char| c == '=' || c == '#' || c.is_whitespace()).unwrap_or(s.len());
        (s[..end].to_string(), &s[end..])
    };
    if key.is_empty() {
        return None;
    }
    let rest = rest.trim_start_matches([' ', '\t']);
    let rest = rest.strip_prefix('=')?;
    let rest = rest.trim_start_matches([' ', '\t']);
    let value = if let Some(q) = rest.strip_prefix('\'') {
        let (body, _) = take_quoted(q, '\'')?;
        body.replace("\\\\", "\\").replace("\\'", "'")
    } else if let Some(q) = rest.strip_prefix('"') {
        let (body, _) = take_quoted(q, '"')?;
        decode_double(&body)
    } else {
        // 引用符なし: 空白に続く # 以降はコメント (python-dotenv の `\s+#.*`)、末尾の空白を落とす
        let mut v = rest.to_string();
        let bytes: Vec<char> = v.chars().collect();
        for i in 1..bytes.len() {
            if bytes[i] == '#' && bytes[i - 1].is_whitespace() {
                let mut j = i;
                while j > 0 && bytes[j - 1].is_whitespace() {
                    j -= 1;
                }
                v = bytes[..j].iter().collect();
                break;
            }
        }
        v.trim_end().to_string()
    };
    Some((key, value))
}

/// 引用符の中身 (エスケープされた引用符は飛ばす) と残り
fn take_quoted(s: &str, q: char) -> Option<(String, &str)> {
    let mut escaped = false;
    for (i, c) in s.char_indices() {
        if escaped {
            escaped = false;
        } else if c == '\\' {
            escaped = true;
        } else if c == q {
            return Some((s[..i].to_string(), &s[i + 1..]));
        }
    }
    None
}

/// 二重引用符の中のエスケープ (python-dotenv の decode_escapes と同じ範囲)
fn decode_double(s: &str) -> String {
    let mut out = String::new();
    let mut it = s.chars().peekable();
    while let Some(c) = it.next() {
        if c != '\\' {
            out.push(c);
            continue;
        }
        match it.next() {
            Some('n') => out.push('\n'),
            Some('r') => out.push('\r'),
            Some('t') => out.push('\t'),
            Some('a') => out.push('\u{07}'),
            Some('b') => out.push('\u{08}'),
            Some('f') => out.push('\u{0c}'),
            Some('v') => out.push('\u{0b}'),
            Some('\\') => out.push('\\'),
            Some('\'') => out.push('\''),
            Some('"') => out.push('"'),
            Some(o) => {
                out.push('\\');
                out.push(o);
            }
            None => out.push('\\'),
        }
    }
    out
}
