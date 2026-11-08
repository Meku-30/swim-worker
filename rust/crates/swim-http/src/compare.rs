//! 見本 (Python 版が送った要求) と Rust 版が送った要求の比べ方。
//! 見本の形は `rust/tests/golden/README.md`。
use serde::{Deserialize, Serialize};

/// 値を比べないヘッダー (送り先・Cookie の中身・body の長さで変わる)。名前と位置は比べる
pub const IGNORE_VALUES: &[&str] = &[":authority", ":path", "cookie", "content-length"];

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Tls {
    pub ja4: String,
    pub peetprint_hash: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Http2 {
    pub akamai_fingerprint: String,
}

/// 見本の 1 要求
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Step {
    pub method: String,
    pub host: String,
    pub path: String,
    pub headers: Vec<(String, String)>,
    pub body: Option<serde_json::Value>,
    pub tls: Tls,
    pub http2: Http2,
}

/// 見本の 1 場面
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Scene {
    pub scene: String,
    pub steps: Vec<Step>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Diff {
    /// ヘッダーの名前の並び (抜け・余分・順番) が違う
    Order { golden: Vec<String>, actual: Vec<String> },
    /// 同じ位置の同じ名前で値が違う
    Value { name: String, golden: String, actual: String },
    Tls { golden: Tls, actual: Tls },
    Http2 { golden: String, actual: String },
}

pub fn compare_step(golden: &Step, actual: &Step) -> Vec<Diff> {
    let mut out = Vec::new();
    let gn: Vec<String> = golden.headers.iter().map(|h| h.0.clone()).collect();
    let an: Vec<String> = actual.headers.iter().map(|h| h.0.clone()).collect();
    if gn != an {
        out.push(Diff::Order { golden: gn, actual: an });
    } else {
        for ((name, gv), (_, av)) in golden.headers.iter().zip(&actual.headers) {
            if !IGNORE_VALUES.contains(&name.as_str()) && gv != av {
                out.push(Diff::Value { name: name.clone(), golden: gv.clone(), actual: av.clone() });
            }
        }
    }
    if golden.tls != actual.tls {
        out.push(Diff::Tls { golden: golden.tls.clone(), actual: actual.tls.clone() });
    }
    if golden.http2 != actual.http2 {
        out.push(Diff::Http2 {
            golden: golden.http2.akamai_fingerprint.clone(),
            actual: actual.http2.akamai_fingerprint.clone(),
        });
    }
    out
}
