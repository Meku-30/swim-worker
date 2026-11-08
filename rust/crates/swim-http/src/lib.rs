//! SWIM との通信に使う HTTP クライアント。
//!
//! Python 版 (curl_cffi の chrome136) と同じ TLS・HTTP/2 の設定で通信し、ヘッダーは
//! 呼び出し側が渡した順番のまま送る (エミュレーションの既定のヘッダーは足さない)。
use std::sync::Arc;

use wreq::header::{HeaderMap, HeaderName, HeaderValue, OrigHeaderMap};
use wreq::{Client, Method, Response, cookie::Jar, redirect};
use wreq_util::Emulation;

pub mod compare;

/// curl_cffi の既定 (allow_redirects=True、max_redirects=30) に合わせる
const MAX_REDIRECTS: usize = 30;

/// 送る順番どおりに並べたヘッダー (名前, 値)。名前は小文字で渡す
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct OrderedHeaders(pub Vec<(String, String)>);

/// Chrome 136 と同じ設定のクライアントと、その Cookie の置き場を作る。
/// TLS のセッションの再開の記録はクライアントごと (curl_cffi の AsyncSession ごとと同じ。共有しない)
pub fn build_client() -> wreq::Result<(Client, Arc<Jar>)> {
    let jar = Arc::new(Jar::default());
    let client = Client::builder()
        .emulation(Emulation::Chrome136)
        .cookie_provider(jar.clone())
        .redirect(redirect::Policy::limited(MAX_REDIRECTS))
        .build()?;
    Ok((client, jar))
}

/// 渡した順番でヘッダーを送る。`cookie`・`content-length` と疑似ヘッダー (`:` で始まるもの) は
/// 値を渡さず、並びの位置だけに使う (Cookie は置き場から、長さは body から決まる)
pub async fn send(
    client: &Client,
    method: &str,
    url: &str,
    headers: &OrderedHeaders,
    body: Option<&serde_json::Value>,
) -> wreq::Result<Response> {
    let method = Method::from_bytes(method.as_bytes()).unwrap_or(Method::GET);
    let mut map = HeaderMap::new();
    let mut order = OrigHeaderMap::new();
    for (name, value) in &headers.0 {
        if name.starts_with(':') {
            continue;
        }
        order.insert(name.clone());
        if name == "cookie" || name == "content-length" {
            continue;
        }
        if let (Ok(n), Ok(v)) = (HeaderName::from_bytes(name.as_bytes()), HeaderValue::from_str(value)) {
            map.append(n, v);
        }
    }
    let mut req = client
        .request(method, url)
        .default_headers(false)
        .headers(map)
        .orig_headers(order);
    if let Some(b) = body {
        // curl_cffi と同じ文字列で送る (body の長さ・中身を Python 版とそろえる)
        req = req.body(python_json(b));
    }
    req.send().await
}

/// curl_cffi は `json=` を `json.dumps(obj, separators=(",", ":"))` (ensure_ascii=True) で送る
/// (curl_cffi 0.16.3 `requests/utils.py:665`)
pub fn python_json(v: &serde_json::Value) -> String {
    let mut out = String::new();
    write_py(v, &mut out);
    out
}

fn write_py(v: &serde_json::Value, out: &mut String) {
    use serde_json::Value::*;
    match v {
        Null | Bool(_) | Number(_) => out.push_str(&v.to_string()),
        String(s) => {
            out.push('"');
            for c in s.chars() {
                match c {
                    '"' => out.push_str("\\\""),
                    '\\' => out.push_str("\\\\"),
                    '\n' => out.push_str("\\n"),
                    '\r' => out.push_str("\\r"),
                    '\t' => out.push_str("\\t"),
                    '\u{08}' => out.push_str("\\b"),
                    '\u{0c}' => out.push_str("\\f"),
                    c if (c as u32) < 0x20 || (c as u32) > 0x7e => {
                        let mut buf = [0u16; 2];
                        for u in c.encode_utf16(&mut buf) {
                            out.push_str(&format!("\\u{:04x}", u));
                        }
                    }
                    c => out.push(c),
                }
            }
            out.push('"');
        }
        Array(a) => {
            out.push('[');
            for (i, x) in a.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_py(x, out);
            }
            out.push(']');
        }
        Object(m) => {
            out.push('{');
            for (i, (k, x)) in m.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_py(&String(k.clone()), out);
                out.push(':');
                write_py(x, out);
            }
            out.push('}');
        }
    }
}
