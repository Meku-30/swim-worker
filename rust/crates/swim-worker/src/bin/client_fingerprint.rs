//! 照合: SwimClient (Worker の本体) が組み立てた要求を、送り先だけ指紋の確認先 (tls.peet.ws) に替えて送り、
//! 見本 (Python 版が送ったもの、rust/tests/golden/requests/) と比べる。SWIM には送らない。
//! SwimClient には作った応答を返す。セッション (= TLS の再開の記録) は SwimClient が作る位置のまま。
//! 使い方: client_fingerprint <見本の requests ディレクトリ>   差があれば終了コード 1
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use serde_json::{json, Value};
use swim_http::compare::{compare_step, Http2, Scene, Step, Tls};
use swim_http::OrderedHeaders;
use swim_worker::swim::fake::ok_json;
use swim_worker::swim::transport::{HttpResponse, Session, SessionFactory};
use swim_worker::swim::{SwimClient, SWIM_DEFAULT_REDIRECT_URL, SWIM_LOGIN_URL, SWIM_PORTAL_URL, SWIM_TOP_URL};

const PEET_URL: &str = "https://tls.peet.ws/api/all";

#[derive(Clone, Default)]
struct PeetFactory {
    log: Arc<Mutex<Vec<Step>>>,
}

struct PeetSession {
    client: wreq::Client,
    jar: Arc<wreq::cookie::Jar>,
    cookies: Vec<(String, String)>,
    log: Arc<Mutex<Vec<Step>>>,
}

impl SessionFactory for PeetFactory {
    fn new_session(&self) -> Box<dyn Session> {
        let (client, jar) = swim_http::build_client().expect("client");
        Box::new(PeetSession { client, jar, cookies: Vec::new(), log: self.log.clone() })
    }
}

fn wire(peet: &Value) -> Vec<(String, String)> {
    for f in peet["http2"]["sent_frames"].as_array().cloned().unwrap_or_default() {
        if f["frame_type"] == "HEADERS" {
            return f["headers"]
                .as_array()
                .unwrap()
                .iter()
                .map(|l| {
                    let (n, v) = l.as_str().unwrap().split_once(": ").unwrap_or(("", ""));
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

#[async_trait]
impl Session for PeetSession {
    async fn request(&mut self, method: &str, url: &str, headers: &OrderedHeaders, body: Option<&Value>)
        -> Result<HttpResponse, String> {
        let resp = swim_http::send(&self.client, method, PEET_URL, headers, body).await.map_err(|e| e.to_string())?;
        let peet: Value = resp.json().await.map_err(|e| e.to_string())?;
        let u = url::Url::parse(url).unwrap();
        let path = match u.query() {
            Some(q) => format!("{}?{q}", u.path()),
            None => u.path().to_string(),
        };
        self.log.lock().unwrap().push(Step {
            method: method.into(),
            host: u.host_str().unwrap_or("").into(),
            path,
            headers: wire(&peet),
            body: body.cloned(),
            tls: Tls {
                ja4: peet["tls"]["ja4"].as_str().unwrap_or("").into(),
                peetprint_hash: peet["tls"]["peetprint_hash"].as_str().unwrap_or("").into(),
            },
            http2: Http2 { akamai_fingerprint: peet["http2"]["akamai_fingerprint"].as_str().unwrap_or("").into() },
        });
        tokio::time::sleep(std::time::Duration::from_millis(300)).await;
        // SwimClient を先へ進めるだけの作った応答
        if url == SWIM_LOGIN_URL {
            self.set_cookie("SESSION", "dummy-session");
            let mut r = ok_json(url, json!({"statusCode": 0, "datas": {"redirectUrl": SWIM_DEFAULT_REDIRECT_URL}}));
            r.set_cookies = 1;
            return Ok(r);
        }
        if url.starts_with(&format!("{SWIM_TOP_URL}/swim/api/informations")) {
            return Ok(ok_json(url, json!({"datas": []})));
        }
        Ok(ok_json(url, json!({"statusCode": 0, "datas": {}})))
    }
    fn cookies(&self) -> Vec<(String, String)> {
        self.cookies.clone()
    }
    fn set_cookie(&mut self, name: &str, value: &str) {
        self.cookies.retain(|(n, _)| n != name);
        self.cookies.push((name.into(), value.into()));
        // Cookie ヘッダーの位置を見本と比べるため、確認先にも同じ Cookie を置く
        self.jar.add(format!("{name}={value}; Path=/").as_str(), PEET_URL);
    }
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> ExitCode {
    let dir = PathBuf::from(std::env::args().nth(1).expect("見本のディレクトリを渡す"));
    let tmp = std::env::temp_dir().join(format!("swim-client-fp-{}", std::process::id()));
    std::fs::create_dir_all(&tmp).unwrap();
    let f = PeetFactory::default();
    let new_client = |file: &str| SwimClient::new("dummy-user", "dummy-pass", tmp.join(file), Box::new(f.clone()));
    let take = || std::mem::take(&mut *f.log.lock().unwrap());
    let mut results: Vec<(&str, Vec<Step>)> = Vec::new();

    // 待ち時間は本物のまま (分布の照合は別のテスト)。全体で数分かかる
    new_client("login.json").login().await.unwrap();
    results.push(("login", take()));

    std::fs::write(tmp.join("restore.json"), r#"{"SESSION": "dummy-session"}"#).unwrap();
    new_client("restore.json").login().await.unwrap();
    results.push(("cookie_restore", take()));

    for (scene, prefix) in [("browse_f2aspr", "f2aspr"), ("browse_f2dnrq", "f2dnrq")] {
        let c = new_client("none.json");
        c.login().await.unwrap();
        take();
        c.open_service_page(&format!("{SWIM_PORTAL_URL}/{prefix}/web/dummy")).await;
        results.push((scene, take()));
    }

    // Python の取得の道具と同じく、2 つ目以降の「ログイン済み」は前の場面が保存した Cookie の復元を通る
    let c = new_client("none.json");
    c.login().await.unwrap();
    take();
    let urls = [
        format!("{SWIM_PORTAL_URL}/f2aspr/web/FLV850/dummy"),
        format!("{SWIM_PORTAL_URL}/f2dnrq/web/FUV201/dummy"),
        format!("{SWIM_PORTAL_URL}/service/api/dummy"),
    ];
    for u in &urls {
        c.mark_visited(u).await;
    }
    for u in &urls {
        c.execute_api(u, &json!({"ctrlInfo": {}, "dummy": 1}), true).await.unwrap();
    }
    results.push(("execute_api", take()));

    new_client("pub.json")
        .fetch_public_get(&format!("{SWIM_TOP_URL}/swim/api/informations"), &[("lang".into(), "ja".into())], &[])
        .await
        .unwrap();
    results.push(("fetch_public_get", take()));
    let _ = std::fs::remove_dir_all(&tmp);

    let (mut total, mut bad) = (0, 0);
    println!("{:<18} {:>5} {:>5}", "場面", "要求", "差");
    for (name, got) in results {
        let g: Scene = serde_json::from_str(&std::fs::read_to_string(dir.join(format!("{name}.json"))).unwrap()).unwrap();
        let mut diffs = 0;
        if got.len() != g.steps.len() {
            println!("  {name}: 要求の数が違う ({} 対 見本 {})", got.len(), g.steps.len());
            diffs += 1;
        }
        for (i, (a, s)) in got.iter().zip(&g.steps).enumerate() {
            if a.path != s.path || a.method != s.method {
                println!("  {name}[{i}] 違う要求: {} {} 対 見本 {} {}", a.method, a.path, s.method, s.path);
                diffs += 1;
            }
            for d in compare_step(s, a) {
                println!("  {name}[{i}] {} {}: {d:?}", s.method, s.path);
                diffs += 1;
            }
        }
        println!("{:<18} {:>5} {:>5}", name, g.steps.len(), diffs);
        total += g.steps.len();
        bad += diffs;
    }
    println!("合計 要求 {total}、差 {bad}");
    if bad == 0 { ExitCode::SUCCESS } else { ExitCode::FAILURE }
}
