//! SwimClient が組み立てる要求が、Python 版の見本 (rust/tests/golden/requests/) と同じか。ネットに出ない
use std::path::PathBuf;

use serde_json::{json, Value};
use swim_http::compare::{Scene, IGNORE_VALUES};
use swim_worker::swim::fake::{ok_json, FakeFactory, Recorded};
use swim_worker::swim::transport::HttpResponse;
use swim_worker::swim::{SwimClient, SWIM_DEFAULT_REDIRECT_URL, SWIM_LOGIN_URL, SWIM_PORTAL_URL, SWIM_TOP_URL};

fn golden(scene: &str) -> Scene {
    let p = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(format!("../../tests/golden/requests/{scene}.json"));
    serde_json::from_str(&std::fs::read_to_string(p).unwrap()).unwrap()
}

fn factory() -> FakeFactory {
    FakeFactory::new(|_m, url, _b| {
        if url == SWIM_LOGIN_URL {
            let mut r = ok_json(url, json!({"statusCode": 0, "datas": {"redirectUrl": SWIM_DEFAULT_REDIRECT_URL}}));
            r.set_cookies = 1;
            return Ok(r);
        }
        if url.starts_with(&format!("{SWIM_TOP_URL}/swim/api/informations")) {
            return Ok(ok_json(url, json!({"datas": []})));
        }
        Ok(HttpResponse { ..ok_json(url, json!({"statusCode": 0, "datas": {}})) })
    })
}

fn client(f: &FakeFactory, cookie_file: PathBuf) -> SwimClient {
    SwimClient::new("dummy-user", "dummy-pass", cookie_file, Box::new(f.clone()))
}

fn path_of(url: &str) -> String {
    let u = url::Url::parse(url).unwrap();
    match u.query() {
        Some(q) => format!("{}?{q}", u.path()),
        None => u.path().to_string(),
    }
}

fn assert_matches(scene: &str, got: &[Recorded]) {
    let g = golden(scene);
    assert_eq!(got.len(), g.steps.len(), "{scene}: 要求の数");
    for (i, (r, s)) in got.iter().zip(&g.steps).enumerate() {
        assert_eq!(r.method, s.method, "{scene}[{i}]");
        assert_eq!(path_of(&r.url), s.path, "{scene}[{i}]");
        assert_eq!(url::Url::parse(&r.url).unwrap().host_str().unwrap(), s.host, "{scene}[{i}]");
        let want: Vec<&(String, String)> = s.headers.iter().filter(|(n, _)| !n.starts_with(':')).collect();
        let gn: Vec<&str> = r.headers.iter().map(|(n, _)| n.as_str()).collect();
        let wn: Vec<&str> = want.iter().map(|(n, _)| n.as_str()).collect();
        assert_eq!(gn, wn, "{scene}[{i}] {} の名前の並び", s.path);
        for ((n, v), (_, w)) in r.headers.iter().zip(want.iter().copied()) {
            if !IGNORE_VALUES.contains(&n.as_str()) {
                assert_eq!(v, w, "{scene}[{i}] {} の {n}", s.path);
            }
        }
        if let Some(cl) = s.headers.iter().find(|(n, _)| n == "content-length") {
            let got_cl = r.headers.iter().find(|(n, _)| n == "content-length").unwrap();
            assert_eq!(got_cl.1, cl.1, "{scene}[{i}] content-length");
        }
        assert_eq!(r.body, s.body, "{scene}[{i}] body");
    }
}

#[tokio::test(start_paused = true)]
async fn headers_match_golden_login() {
    let d = tempfile::tempdir().unwrap();
    let f = factory();
    client(&f, d.path().join("c.json")).login().await.unwrap();
    let got = f.take();
    assert_matches("login", &got);
    assert_eq!(got.iter().map(|r| r.session).collect::<Vec<_>>(), vec![0, 0, 0], "ログインは 1 つの一時セッション");
}

#[tokio::test(start_paused = true)]
async fn headers_match_golden_cookie_restore() {
    let d = tempfile::tempdir().unwrap();
    let file = d.path().join("c.json");
    std::fs::write(&file, r#"{"SESSION": "dummy-session"}"#).unwrap();
    let f = factory();
    client(&f, file).login().await.unwrap();
    assert_matches("cookie_restore", &f.take());
}

async fn logged_in(f: &FakeFactory, d: &tempfile::TempDir) -> SwimClient {
    let c = client(f, d.path().join("c.json"));
    c.login().await.unwrap();
    f.take();
    c
}

#[tokio::test(start_paused = true)]
async fn headers_match_golden_browse_f2aspr() {
    let d = tempfile::tempdir().unwrap();
    let f = factory();
    let c = logged_in(&f, &d).await;
    c.open_service_page(&format!("{SWIM_PORTAL_URL}/f2aspr/web/dummy")).await;
    assert_matches("browse_f2aspr", &f.take());
}

#[tokio::test(start_paused = true)]
async fn headers_match_golden_browse_f2dnrq() {
    let d = tempfile::tempdir().unwrap();
    let f = factory();
    let c = logged_in(&f, &d).await;
    c.open_service_page(&format!("{SWIM_PORTAL_URL}/f2dnrq/web/dummy")).await;
    assert_matches("browse_f2dnrq", &f.take());
}

#[tokio::test(start_paused = true)]
async fn headers_match_golden_execute_api() {
    let d = tempfile::tempdir().unwrap();
    let f = factory();
    let c = logged_in(&f, &d).await;
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
    assert_matches("execute_api", &f.take());
}

#[tokio::test(start_paused = true)]
async fn headers_match_golden_fetch_public_get() {
    let d = tempfile::tempdir().unwrap();
    let f = factory();
    let c = client(&f, d.path().join("c.json"));
    let v: Value = c
        .fetch_public_get(&format!("{SWIM_TOP_URL}/swim/api/informations"), &[("lang".into(), "ja".into())])
        .await
        .unwrap();
    assert_eq!(v, json!({"datas": []}));
    assert_matches("fetch_public_get", &f.take());
}
