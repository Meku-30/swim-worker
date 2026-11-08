//! 要求のヘッダーの組み立て。Python 版 (curl_cffi 0.16.3 の chrome136) が実際に送る並びと同じにする
//! (見本: rust/tests/golden/requests/)。決まり:
//! 1. あれば `cookie` が先頭
//! 2. Chrome の既定の 13 個が決まった順。セッションの既定・要求のヘッダーで同じ名前があれば、その位置のまま値を替える
//! 3. それ以外の名前は「セッションの既定 → 要求」の辞書の順で後ろに足す (同じ名前は最初の位置で値だけ替える)
//! 4. body があれば最後に content-type・content-length (JSON)、body なしの POST は content-length: 0・content-type (フォーム)
use swim_http::OrderedHeaders;

pub const UA: &str =
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36";
pub const NAV_ACCEPT: &str = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7";

/// chrome136 の既定 (名前は小文字)
const CHROME_DEFAULTS: &[(&str, &str)] = &[
    ("sec-ch-ua", r#""Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99""#),
    ("sec-ch-ua-mobile", "?0"),
    ("sec-ch-ua-platform", r#""macOS""#),
    ("upgrade-insecure-requests", "1"),
    ("user-agent", UA),
    ("accept", NAV_ACCEPT),
    ("sec-fetch-site", "none"),
    ("sec-fetch-mode", "navigate"),
    ("sec-fetch-user", "?1"),
    ("sec-fetch-dest", "document"),
    ("accept-encoding", "gzip, deflate, br, zstd"),
    ("accept-language", "en-US,en;q=0.9"),
    ("priority", "u=0, i"),
];

/// 要求の body の種類 (content-type・content-length の付け方が違う)
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BodyKind {
    None,
    /// JSON の body (長さはバイト数)
    Json(usize),
    /// body なしの POST
    EmptyPost,
}

/// 辞書の update と同じ: 名前 (大文字小文字を問わない) が既にあれば値だけ替え、なければ後ろに足す
fn merge(into: &mut Vec<(String, String)>, from: &[(&str, &str)]) {
    for (k, v) in from {
        let k = k.to_ascii_lowercase();
        match into.iter_mut().find(|(n, _)| *n == k) {
            Some(slot) => slot.1 = v.to_string(),
            None => into.push((k, v.to_string())),
        }
    }
}

pub fn build(session: &[(&str, &str)], request: &[(&str, &str)], body: BodyKind) -> OrderedHeaders {
    let mut merged = Vec::new();
    merge(&mut merged, session);
    merge(&mut merged, request);
    let mut out: Vec<(String, String)> = vec![("cookie".into(), String::new())];
    for (name, default) in CHROME_DEFAULTS {
        let v = merged.iter().find(|(n, _)| n == name).map_or(*default, |(_, v)| v.as_str());
        out.push((name.to_string(), v.to_string()));
    }
    for (n, v) in &merged {
        if !CHROME_DEFAULTS.iter().any(|(d, _)| d == n) {
            out.push((n.clone(), v.clone()));
        }
    }
    match body {
        BodyKind::None => {}
        BodyKind::Json(len) => {
            out.push(("content-type".into(), "application/json".into()));
            out.push(("content-length".into(), len.to_string()));
        }
        BodyKind::EmptyPost => {
            out.push(("content-length".into(), "0".into()));
            out.push(("content-type".into(), "application/x-www-form-urlencoded".into()));
        }
    }
    OrderedHeaders(out)
}
