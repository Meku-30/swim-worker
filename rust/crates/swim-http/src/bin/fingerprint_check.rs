//! 見本 (Python 版が送った要求) と同じ要求を Rust から指紋の確認先に送り、受け取られた内容を比べる。
//! 使い方: fingerprint_check <見本の requests ディレクトリ>
//! 差が 1 つでもあれば終了コード 1。SWIM には送らない。
use std::path::PathBuf;
use std::process::ExitCode;

use swim_http::compare::{Http2, Scene, Step, Tls, compare_step};
use swim_http::{OrderedHeaders, build_client, send};

const PEET_URL: &str = "https://tls.peet.ws/api/all";
/// TLS の再開でない (新しい接続の) JA4
const JA4_FULL: &str = "t13d1516h2_8daaf6152771_d8a2da3f94cd";
const SCENES: &[&str] = &["login", "cookie_restore", "browse_f2aspr", "browse_f2dnrq", "execute_api", "fetch_public_get"];

fn wire_headers(peet: &serde_json::Value) -> Vec<(String, String)> {
    let frames = peet["http2"]["sent_frames"].as_array().cloned().unwrap_or_default();
    for f in frames {
        if f["frame_type"] == "HEADERS" {
            return f["headers"]
                .as_array()
                .unwrap()
                .iter()
                .map(|l| {
                    let l = l.as_str().unwrap();
                    let (n, v) = l.split_once(": ").unwrap_or((l, ""));
                    let v = match n {
                        ":authority" => "<authority>",
                        "cookie" => "<cookie>",
                        _ => v,
                    };
                    (n.to_string(), v.to_string())
                })
                .collect();
        }
    }
    Vec::new()
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> ExitCode {
    let dir = PathBuf::from(std::env::args().nth(1).expect("見本のディレクトリを渡す"));
    let (mut client, mut jar) = build_client().expect("client");
    let mut total = 0usize;
    let mut bad = 0usize;
    println!("{:<18} {:>5} {:>5}", "場面", "要求", "差");
    for name in SCENES {
        let text = std::fs::read_to_string(dir.join(format!("{name}.json"))).expect("見本");
        let scene: Scene = serde_json::from_str(&text).expect("見本の形");
        let mut diffs = 0;
        for (i, g) in scene.steps.iter().enumerate() {
            // Python 版は場面の途中で新しいセッション (TLS の再開の記録を持たない) を作ることがある。
            // 見本が新しい接続 (再開でない) のところでは、こちらもクライアントを作り直す
            if g.tls.ja4 == JA4_FULL {
                (client, jar) = build_client().expect("client");
            }
            jar.clear();
            if g.headers.iter().any(|h| h.0 == "cookie") {
                jar.add("SESSION=dummy-session", PEET_URL);
            }
            let headers = OrderedHeaders(g.headers.clone());
            let resp = match send(&client, &g.method, PEET_URL, &headers, g.body.as_ref()).await {
                Ok(r) => r,
                Err(e) => {
                    println!("  {name}[{i}] 送れない: {e}");
                    diffs += 1;
                    continue;
                }
            };
            let peet: serde_json::Value = resp.json().await.expect("確認先の JSON");
            let actual = Step {
                method: g.method.clone(),
                host: g.host.clone(),
                path: g.path.clone(),
                headers: wire_headers(&peet),
                body: g.body.clone(),
                tls: Tls {
                    ja4: peet["tls"]["ja4"].as_str().unwrap_or("").into(),
                    peetprint_hash: peet["tls"]["peetprint_hash"].as_str().unwrap_or("").into(),
                },
                http2: Http2 { akamai_fingerprint: peet["http2"]["akamai_fingerprint"].as_str().unwrap_or("").into() },
            };
            for d in compare_step(g, &actual) {
                println!("  {name}[{i}] {} {}: {d:?}", g.method, g.path);
                diffs += 1;
            }
            tokio::time::sleep(std::time::Duration::from_millis(300)).await;
        }
        println!("{:<18} {:>5} {:>5}", name, scene.steps.len(), diffs);
        total += scene.steps.len();
        bad += diffs;
    }
    println!("合計 要求 {total}、差 {bad}");
    if bad == 0 { ExitCode::SUCCESS } else { ExitCode::FAILURE }
}
