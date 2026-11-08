//! SwimClient の振る舞い (Python の tests/test_auth.py の要点)。ネットに出ない
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use rand::rngs::StdRng;
use rand::SeedableRng;
use serde_json::{json, Value};
use swim_worker::delays::{post_response, PreTask};
use swim_worker::swim::fake::{ok_json, FakeFactory};
use swim_worker::swim::transport::HttpResponse;
use swim_worker::swim::{
    validate_swim_url, SwimClient, SwimError, SWIM_DEFAULT_REDIRECT_URL, SWIM_LOGIN_URL, SWIM_PORTAL_URL,
};

fn login_ok(url: &str) -> HttpResponse {
    let mut r = ok_json(url, json!({"statusCode": 0, "datas": {"redirectUrl": SWIM_DEFAULT_REDIRECT_URL}}));
    r.set_cookies = 1;
    r
}

fn status(url: &str, code: u16) -> HttpResponse {
    HttpResponse { status: code, final_url: url.into(), body: b"{}".to_vec(), set_cookies: 0 }
}

fn api() -> String {
    format!("{SWIM_PORTAL_URL}/service/api/x")
}

#[test]
fn delays_match_golden() {
    let p = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/delays.json");
    let g: Value = serde_json::from_str(&std::fs::read_to_string(p).unwrap()).unwrap();
    let pre = PreTask::new(4.0, 15.0, 1.5, 25.0);
    assert!((pre.mu - g["pre_task"]["mu"].as_f64().unwrap()).abs() < 1e-12);
    assert!((pre.sigma - g["pre_task"]["sigma"].as_f64().unwrap()).abs() < 1e-12);
    let mut rng = StdRng::seed_from_u64(1);
    let n = 100_000;
    let mut a: Vec<f64> = (0..n).map(|_| pre.sample(&mut rng)).collect();
    let mut b: Vec<f64> = (0..n).map(|_| post_response(&mut rng)).collect();
    a.sort_by(f64::total_cmp);
    b.sort_by(f64::total_cmp);
    for (xs, key) in [(&a, "pre_task"), (&b, "post_response")] {
        for q in ["5", "25", "50", "75", "95", "99"] {
            let want = g[key]["quantiles"][q].as_f64().unwrap();
            let got = xs[(n * q.parse::<usize>().unwrap() / 100).min(n - 1)];
            assert!((got - want).abs() / want < 0.05, "{key} {q}%: want {want} got {got}");
        }
    }
}

#[test]
fn only_swim_hosts_allowed() {
    for ok in ["https://web.swim.mlit.go.jp/x", "https://top.swim.mlit.go.jp:443/a?b", "HTTPS://WEB.SWIM.MLIT.GO.JP/"] {
        assert!(validate_swim_url(ok).is_ok(), "{ok}");
    }
    for bad in [
        "", "http://web.swim.mlit.go.jp/", "https://swim.mlit.go.jp/", "https://evil.example/",
        "https://web.swim.mlit.go.jp.evil.example/", "https://u@web.swim.mlit.go.jp/", "https://web.swim.mlit.go.jp:8443/",
        "https://web.swim.mlit.go.jp/a b", "https://web.swim.mlit.go.jp\\@evil/", "https://-a.swim.mlit.go.jp/",
    ] {
        assert!(matches!(validate_swim_url(bad), Err(SwimError::Disallowed(_))), "{bad:?}");
    }
}

#[tokio::test(start_paused = true)]
async fn login_backoff_blocks_after_failure() {
    let d = tempfile::tempdir().unwrap();
    let f = FakeFactory::new(|_, url, _| Ok(if url == SWIM_LOGIN_URL { status(url, 500) } else { ok_json(url, json!({})) }));
    let c = SwimClient::new("u", "p", d.path().join("c.json"), Box::new(f.clone()));
    assert!(matches!(c.login().await, Err(SwimError::Auth(m)) if m.contains("status=500")));
    let n = f.take().len();
    assert!(matches!(c.login().await, Err(SwimError::Auth(m)) if m.contains("ログイン抑制中")));
    assert!(f.take().is_empty(), "抑制中は SWIM に送らない (1 回目は {n} 件)");
    tokio::time::advance(std::time::Duration::from_secs(61)).await;
    assert!(matches!(c.login().await, Err(SwimError::Auth(m)) if m.contains("status=500")), "60 秒後にまた試す");
}

#[tokio::test(start_paused = true)]
async fn execute_api_403_relogin_once() {
    let d = tempfile::tempdir().unwrap();
    let calls = Arc::new(AtomicUsize::new(0));
    let c2 = calls.clone();
    let f = FakeFactory::new(move |_, url, _| {
        Ok(if url == SWIM_LOGIN_URL {
            login_ok(url)
        } else if url.ends_with("/service/api/x") {
            if c2.fetch_add(1, Ordering::SeqCst) == 0 { status(url, 403) } else { ok_json(url, json!({"ok": 1})) }
        } else {
            ok_json(url, json!({}))
        })
    });
    let c = SwimClient::new("u", "p", d.path().join("c.json"), Box::new(f.clone()));
    assert_eq!(c.execute_api(&api(), &json!({}), true).await.unwrap(), json!({"ok": 1}));
    let logins = f.take().iter().filter(|r| r.url == SWIM_LOGIN_URL).count();
    assert_eq!(logins, 2, "最初のログイン + 403 の再ログイン 1 回");
    assert_eq!(calls.load(Ordering::SeqCst), 2);
}

#[tokio::test(start_paused = true)]
async fn execute_api_capability_403_no_relogin() {
    let d = tempfile::tempdir().unwrap();
    let f = FakeFactory::new(|_, url, _| Ok(if url == SWIM_LOGIN_URL { login_ok(url) } else if url.ends_with("/x") { status(url, 403) } else { ok_json(url, json!({})) }));
    let c = SwimClient::new("u", "p", d.path().join("c.json"), Box::new(f.clone()));
    let e = c.execute_api(&api(), &json!({}), false).await.unwrap_err();
    assert!(matches!(e, SwimError::Unauthorized { status: 403, .. }), "{e:?}");
    assert_eq!(f.take().iter().filter(|r| r.url == SWIM_LOGIN_URL).count(), 1, "再ログインしない");
}

#[tokio::test(start_paused = true)]
async fn redirect_outside_sets_error_kind() {
    let d = tempfile::tempdir().unwrap();
    let f = FakeFactory::new(|_, url, _| {
        Ok(if url == SWIM_LOGIN_URL {
            login_ok(url)
        } else if url.ends_with("/x") {
            HttpResponse { final_url: "https://maint.example.jp/info?token=secret".into(), ..ok_json(url, json!({})) }
        } else {
            ok_json(url, json!({}))
        })
    });
    let c = SwimClient::new("u", "p", d.path().join("c.json"), Box::new(f));
    match c.execute_api(&api(), &json!({}), true).await {
        Err(SwimError::RedirectOutside(shown)) => {
            assert_eq!(shown, "https://maint.example.jp/info", "クエリは出さない");
        }
        other => panic!("{other:?}"),
    }
}

#[tokio::test(start_paused = true)]
async fn cookies_saved_private_and_restorable() {
    let d = tempfile::tempdir().unwrap();
    let file = d.path().join("data/c.json");
    let f = FakeFactory::new(|_, url, _| Ok(if url == SWIM_LOGIN_URL { login_ok(url) } else { ok_json(url, json!({})) }));
    SwimClient::new("u", "p", file.clone(), Box::new(f)).login().await.unwrap();
    let saved: Value = serde_json::from_str(&std::fs::read_to_string(&file).unwrap()).unwrap();
    assert_eq!(saved, json!({"SESSION": "dummy-session"}), "Python と同じ {{名前: 値}} の形");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(std::fs::metadata(&file).unwrap().permissions().mode() & 0o777, 0o600);
    }
}

#[tokio::test(start_paused = true)]
async fn disallowed_url_is_not_sent() {
    let d = tempfile::tempdir().unwrap();
    let f = FakeFactory::new(|_, url, _| Ok(ok_json(url, json!({}))));
    let c = SwimClient::new("u", "p", d.path().join("c.json"), Box::new(f.clone()));
    assert!(matches!(c.execute_api("https://evil.example/", &json!({}), true).await, Err(SwimError::Disallowed(_))));
    assert!(f.take().is_empty());
}
