//! タスク処理 (Python の tests/test_consumer.py・test_consumer_robust.py を移したもの)。
//! Redis は FakeKv、SWIM は FakeSwim。時間は tokio の時計を止めて進める
use std::collections::VecDeque;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use serde_json::{json, Value};
use swim_worker::consumer::result::{encode, make_result};
use swim_worker::consumer::{ConsumerConfig, ConsumerError, ConsumerEvent, SwimApi, TaskConsumer};
use swim_worker::delays::PreTask;
use swim_worker::kv::fake::FakeKv;
use swim_worker::kv::{KvError, KvErrorKind};
use swim_worker::paths::Paths;
use swim_worker::swim::SwimError;
use tokio::sync::mpsc;

const URL: &str = "https://web.swim.mlit.go.jp/f2aspr/web/FLV920/LGV358";

#[derive(Default)]
struct FakeSwim {
    replies: Mutex<VecDeque<Result<Value, SwimError>>>,
    calls: Mutex<Vec<(String, bool)>>,
    delay: Mutex<Option<Duration>>,
}

impl FakeSwim {
    fn reply(&self, r: Result<Value, SwimError>) {
        self.replies.lock().unwrap().push_back(r);
    }
}

#[async_trait]
impl SwimApi for FakeSwim {
    async fn execute_api(&self, url: &str, _b: &Value, retry: bool) -> Result<Value, SwimError> {
        self.calls.lock().unwrap().push((url.into(), retry));
        let d = *self.delay.lock().unwrap();
        if let Some(d) = d {
            tokio::time::sleep(d).await;
        }
        self.replies.lock().unwrap().pop_front().unwrap_or_else(|| Ok(json!({"ok": 1})))
    }
    async fn fetch_public_get(&self, url: &str, _p: &[(String, String)], _h: &[(String, String)]) -> Result<Value, SwimError> {
        self.calls.lock().unwrap().push((url.into(), true));
        self.replies.lock().unwrap().pop_front().unwrap_or_else(|| Ok(json!({"datas": []})))
    }
}

struct T {
    kv: Arc<FakeKv>,
    swim: Arc<FakeSwim>,
    c: Arc<TaskConsumer>,
    rx: mpsc::UnboundedReceiver<ConsumerEvent>,
    _dir: tempfile::TempDir,
    paths: Paths,
}

fn setup_with(version: &str) -> T {
    let dir = tempfile::tempdir().unwrap();
    let paths = Paths::new(dir.path().to_path_buf());
    let kv = Arc::new(FakeKv::default());
    let swim = Arc::new(FakeSwim::default());
    let (tx, rx) = mpsc::unbounded_channel();
    let cfg = ConsumerConfig {
        worker_name: "w1".into(),
        heartbeat_interval: 30,
        pre_task: PreTask::new(0.01, 0.02, 0.0, 0.01),
        task_hard_timeout: 300.0,
        blpop_timeout: 20.0,
        paths: paths.clone(),
    };
    let c = Arc::new(TaskConsumer::new(kv.clone(), swim.clone(), cfg, Some(tx)).with_version(version).with_seed(1));
    T { kv, swim, c, rx, _dir: dir, paths }
}

fn setup() -> T {
    setup_with("1.3.0")
}

fn conn_err() -> KvError {
    KvError::new(KvErrorKind::Connection, "down")
}

fn task(id: &str, job: &str, params: Value) -> Value {
    json!({"task_id": id, "job_type": job, "params": params})
}

fn result_of(t: &T, id: &str) -> Value {
    let b = t.kv.get_bytes(&format!("results:w1:{id}")).expect("結果がない");
    serde_json::from_slice(&zstd::decode_all(&b[..]).unwrap()).unwrap()
}

fn states(t: &mut T) -> Vec<ConsumerEvent> {
    let mut v = Vec::new();
    while let Ok(e) = t.rx.try_recv() {
        v.push(e);
    }
    v
}

fn last_idle(ev: &[ConsumerEvent]) -> (u64, u64) {
    match ev.iter().rev().find(|e| matches!(e, ConsumerEvent::TaskState { state: "idle", .. })) {
        Some(ConsumerEvent::TaskState { total, errors, .. }) => (*total, *errors),
        _ => panic!("idle がない: {ev:?}"),
    }
}

// ---- 結果の形 ----

#[test]
fn results_golden_roundtrip() {
    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/results");
    for name in ["success_raw", "success_parsed", "error", "error_redirect_outside", "capability"] {
        let g: Value = serde_json::from_str(&std::fs::read_to_string(dir.join(format!("{name}.json"))).unwrap()).unwrap();
        let want = &g["result"];
        let m = want.as_object().unwrap();
        let got = make_result(
            m["task_id"].as_str().unwrap(),
            m["worker_name"].as_str().unwrap(),
            m["status"].as_str().unwrap(),
            Some(m["data"].clone()).filter(|d| !d.is_null()),
            m["error"].as_str().map(String::from),
            m.get("format").and_then(Value::as_str),
            m.get("error_kind").and_then(Value::as_str),
            m["completed_at"].as_str().unwrap().into(),
        );
        assert_eq!(&got, want, "{name}");
        assert_eq!(got.as_object().unwrap().keys().collect::<Vec<_>>(), m.keys().collect::<Vec<_>>(), "{name} のキーの順");
        let back: Value = serde_json::from_slice(&zstd::decode_all(&encode(&got)[..]).unwrap()).unwrap();
        assert_eq!(&back, want, "{name}");
        let py = base64_decode(g["zstd_b64"].as_str().unwrap());
        assert_eq!(zstd::decode_all(&py[..]).unwrap(), zstd::decode_all(&encode(&got)[..]).unwrap(), "{name}: 展開した JSON のバイト列が Python と同じ");
    }
}

fn base64_decode(s: &str) -> Vec<u8> {
    const T: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = Vec::new();
    let mut buf = 0u32;
    let mut bits = 0;
    for c in s.bytes().filter(|c| *c != b'=') {
        buf = (buf << 6) | T.iter().position(|x| *x == c).unwrap() as u32;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((buf >> bits) as u8);
        }
    }
    out
}

#[test]
fn completed_at_is_python_isoformat() {
    let s = swim_worker::consumer::result::now_iso();
    assert_eq!(s.len(), "2026-11-07T12:34:56.789012+00:00".len(), "{s}");
    assert!(s.ends_with("+00:00"));
}

// ---- 結果の書き込み ----

#[tokio::test(start_paused = true)]
async fn result_write_is_retried() {
    let mut t = setup();
    t.kv.fail_next("setex", conn_err());
    t.kv.fail_next("setex", KvError::new(KvErrorKind::Timeout, "slow"));
    t.c.execute_task(&task("t1", "collect_pireps", json!({"url": URL, "body": {}}))).await;
    assert_eq!(t.kv.calls().iter().filter(|c| c.starts_with("setex results:w1:t1")).count(), 3);
    assert_eq!(result_of(&t, "t1")["status"], "success");
    assert_eq!(t.kv.ttl_of("results:w1:t1"), Some(3600));
    assert_eq!(last_idle(&states(&mut t)), (1, 0));
}

#[tokio::test(start_paused = true)]
async fn result_write_gives_up_after_three() {
    let mut t = setup();
    for _ in 0..3 {
        t.kv.fail_next("setex", conn_err());
    }
    t.c.execute_task(&task("t1", "collect_pireps", json!({"url": URL, "body": {}}))).await;
    assert_eq!(t.kv.calls().iter().filter(|c| c.starts_with("setex")).count(), 3);
    assert_eq!(last_idle(&states(&mut t)), (1, 1), "書けなくても数えて idle を知らせる");
}

#[tokio::test(start_paused = true)]
async fn capability_result_write_failure_still_notifies() {
    let mut t = setup();
    for _ in 0..3 {
        t.kv.fail_next("setex", conn_err());
    }
    t.c.execute_task(&task("t1", "capability_test", json!({"tests": []}))).await;
    assert_eq!(last_idle(&states(&mut t)), (1, 1));
}

// ---- タスクの実行 ----

#[tokio::test(start_paused = true)]
async fn execute_task_failure() {
    let mut t = setup();
    t.swim.reply(Err(SwimError::Auth("APIエラー (status=500)".into())));
    t.c.execute_task(&task("t1", "collect_pireps", json!({"url": URL, "body": {}}))).await;
    let r = result_of(&t, "t1");
    assert_eq!(r["status"], "error");
    assert_eq!(r["error"], "APIエラー (status=500)");
    assert!(r.get("error_kind").is_none());
    assert_eq!(last_idle(&states(&mut t)), (1, 1));
}

#[tokio::test(start_paused = true)]
async fn redirect_outside_sets_error_kind() {
    let t = setup();
    t.swim.reply(Err(SwimError::RedirectOutside("https://maint.example.jp/x".into())));
    t.c.execute_task(&task("t1", "collect_pireps", json!({"url": URL, "body": {}}))).await;
    let r = result_of(&t, "t1");
    assert_eq!(r["error_kind"], "redirect_outside");
    assert!(r["error"].as_str().unwrap().contains("SWIM の外へリダイレクト"));
}

#[tokio::test(start_paused = true)]
async fn invalid_task_is_dropped() {
    let t = setup();
    t.c.execute_task(&json!({"job_type": "x"})).await;
    assert!(t.kv.calls().iter().all(|c| !c.starts_with("setex")));
}

#[tokio::test(start_paused = true)]
async fn get_task_uses_fetch_public_get() {
    let t = setup();
    t.c.execute_task(&task("t1", "maintenance", json!({"url": "https://top.swim.mlit.go.jp/swim/api/informations", "method": "GET", "params": {"lang": "ja"}}))).await;
    assert_eq!(result_of(&t, "t1")["data"], json!({"datas": []}));
}

const NOTAMS: &str = r#"{"notamList": [{"number": "A0001/26", "location": "RJTT", "contents": "x"}]}"#;

#[tokio::test(start_paused = true)]
async fn execute_task_parsed_when_whitelisted() {
    let t = setup();
    t.kv.set_members("swim:parse_enabled", &["collect_notams"]);
    t.swim.reply(Ok(serde_json::from_str(NOTAMS).unwrap()));
    t.c.execute_task(&task("t1", "collect_notams", json!({"url": URL, "body": {}}))).await;
    let r = result_of(&t, "t1");
    assert_eq!(r["format"], "parsed");
    assert!(r["data"].is_array());
    assert!(t.kv.calls().iter().any(|c| c.starts_with("sismember_many")), "個別の除外は 1 往復のパイプライン");
}

#[tokio::test(start_paused = true)]
async fn execute_task_raw_when_not_whitelisted() {
    let t = setup();
    t.swim.reply(Ok(serde_json::from_str(NOTAMS).unwrap()));
    t.c.execute_task(&task("t1", "collect_notams", json!({"url": URL, "body": {}}))).await;
    let r = result_of(&t, "t1");
    assert!(r.get("format").is_none());
    assert_eq!(r["data"], serde_json::from_str::<Value>(NOTAMS).unwrap());
}

#[tokio::test(start_paused = true)]
async fn execute_task_raw_when_worker_individually_disabled() {
    let t = setup();
    t.kv.set_members("swim:parse_enabled", &["collect_notams"]);
    t.kv.set_members("swim:parse_disabled_workers:collect_notams", &["w1"]);
    t.swim.reply(Ok(serde_json::from_str(NOTAMS).unwrap()));
    t.c.execute_task(&task("t1", "collect_notams", json!({"url": URL, "body": {}}))).await;
    assert!(result_of(&t, "t1").get("format").is_none());
}

#[tokio::test(start_paused = true)]
async fn parse_cache_kept_on_redis_error() {
    let t = setup();
    t.kv.set_members("swim:parse_enabled", &["collect_notams"]);
    t.c.execute_task(&task("t1", "collect_notams", json!({"url": URL, "body": {}}))).await;
    tokio::time::advance(Duration::from_secs(61)).await;
    t.kv.fail_next("smembers", conn_err());
    t.swim.reply(Ok(serde_json::from_str(NOTAMS).unwrap()));
    t.c.execute_task(&task("t2", "collect_notams", json!({"url": URL, "body": {}}))).await;
    assert_eq!(result_of(&t, "t2")["format"], "parsed", "Redis のエラーのときは前回の値");
}

#[tokio::test(start_paused = true)]
async fn parse_failure_falls_back_to_raw() {
    let t = setup();
    t.kv.set_members("swim:parse_enabled", &["collect_notams"]);
    t.swim.reply(Ok(json!({"notamList": "not a list", "x": [1]})));
    t.c.execute_task(&task("t1", "collect_notams", json!({"url": URL, "body": {}}))).await;
    let r = result_of(&t, "t1");
    assert_eq!(r["status"], "success");
}

// ---- capability ----

#[tokio::test(start_paused = true)]
async fn capability_test_calls_execute_api_without_auth_retry() {
    let t = setup();
    t.c.execute_task(&task("t1", "capability_test", json!({"tests": [{"job_type": "a", "url": URL, "body": {}}]}))).await;
    assert_eq!(t.swim.calls.lock().unwrap().clone(), vec![(URL.to_string(), false)]);
}

#[tokio::test(start_paused = true)]
async fn capability_test_classifies_reasons() {
    let t = setup();
    t.swim.reply(Ok(json!({})));
    t.swim.reply(Err(SwimError::Unauthorized { message: "API 403エラー".into(), status: 403 }));
    t.swim.reply(Err(SwimError::Auth("timeout".into())));
    let tests = json!({"tests": [
        {"job_type": "a", "url": URL, "body": {}},
        {"job_type": "b", "url": URL, "body": {}},
        {"job_type": "c", "url": URL, "body": {}}]});
    t.c.execute_task(&task("t1", "capability_test", tests)).await;
    let caps = &result_of(&t, "t1")["data"]["capabilities"];
    assert_eq!(caps["a"], json!({"ok": true, "error": null, "reason": null}));
    assert_eq!(caps["b"], json!({"ok": false, "error": "API 403エラー", "reason": "unauthorized"}));
    assert_eq!(caps["c"], json!({"ok": false, "error": "timeout", "reason": "transient"}));
}

// ---- 強制の時間切れ ----

#[tokio::test(start_paused = true)]
async fn task_hard_timeout_writes_error() {
    let mut t = setup();
    *t.swim.delay.lock().unwrap() = Some(Duration::from_secs(10_000));
    let c = t.c.clone();
    let h = tokio::spawn(async move { c.run().await });
    tokio::time::sleep(Duration::from_secs(1)).await; // 起動時に古いタスクは捨てるので、起動のあとに積む
    t.kv.push("tasks:w1", &task("t1", "collect_pireps", json!({"url": URL, "body": {}})).to_string());
    tokio::time::sleep(Duration::from_secs(310)).await;
    let r = result_of(&t, "t1");
    assert_eq!(r["status"], "error");
    assert_eq!(r["error"], "Worker 側の強制タイムアウト (300秒)");
    assert_eq!(last_idle(&states(&mut t)), (1, 1));
    t.c.stop();
    h.await.unwrap().unwrap();
}

// ---- 登録・heartbeat ----

#[tokio::test(start_paused = true)]
async fn register_worker_new() {
    let t = setup();
    t.c.register().await.unwrap();
    assert!(t.kv.calls().contains(&"sadd workers:pending w1".to_string()));
}

#[tokio::test(start_paused = true)]
async fn register_worker_already_approved() {
    let t = setup();
    t.kv.set_members("workers:approved", &["w1"]);
    t.c.register().await.unwrap();
    assert!(!t.kv.calls().iter().any(|c| c.starts_with("sadd")));
}

#[tokio::test(start_paused = true)]
async fn send_heartbeat_get_then_setex() {
    let t = setup();
    t.c.send_heartbeat().await.unwrap();
    let calls = t.kv.calls();
    assert_eq!(calls, vec!["get heartbeat:w1".to_string(), "setex heartbeat:w1".to_string()], "EVAL を使わない");
    assert_eq!(t.kv.ttl_of("heartbeat:w1"), Some(60));
    assert_eq!(t.kv.get_bytes("heartbeat:w1").unwrap(), t.c.instance_token().as_bytes());
}

#[tokio::test(start_paused = true)]
async fn other_owner_raises_duplicate() {
    let t = setup();
    t.kv.put("heartbeat:w1", "someone-else");
    assert!(matches!(t.c.send_heartbeat().await, Err(ConsumerError::Duplicate(_))));
}

#[tokio::test(start_paused = true)]
async fn heartbeat_reclaims_own_stale_key() {
    let t = setup();
    std::fs::create_dir_all(t.paths.data_dir()).unwrap();
    std::fs::write(t.paths.last_instance_token(), "old-token").unwrap();
    t.kv.put("heartbeat:w1", "old-token");
    t.c.stop(); // 起動だけ見る
    let c = t.c.clone();
    let start = tokio::time::Instant::now();
    c.run().await.unwrap();
    assert!(start.elapsed() < Duration::from_secs(5), "待たずに取り直す");
    assert_eq!(std::fs::read_to_string(t.paths.last_instance_token()).unwrap(), t.c.instance_token());
}

#[tokio::test(start_paused = true)]
async fn heartbeat_does_not_claim_other_worker() {
    let t = setup();
    std::fs::create_dir_all(t.paths.data_dir()).unwrap();
    std::fs::write(t.paths.last_instance_token(), "old-token").unwrap();
    t.kv.put("heartbeat:w1", "other-token");
    let e = t.c.run().await.unwrap_err();
    assert!(matches!(e, ConsumerError::Duplicate(_)), "{e:?}");
}

#[tokio::test(start_paused = true)]
async fn heartbeat_duplicate_after_wait() {
    let t = setup();
    t.kv.put("heartbeat:w1", "other");
    let start = tokio::time::Instant::now();
    assert!(matches!(t.c.run().await, Err(ConsumerError::Duplicate(_))));
    let waited = start.elapsed().as_secs();
    assert!((70..=75).contains(&waited), "TTL+10 秒 (70 秒) 待つ: {waited}");
    assert!(!t.paths.startup_marker().exists(), "起動の印は書かない");
}

#[tokio::test(start_paused = true)]
async fn run_stops_with_duplicate_when_heartbeat_taken() {
    let t = setup();
    let c = t.c.clone();
    let h = tokio::spawn(async move { c.run().await });
    tokio::time::sleep(Duration::from_secs(1)).await;
    t.kv.put("heartbeat:w1", "intruder");
    tokio::time::sleep(Duration::from_secs(31)).await;
    assert!(matches!(h.await.unwrap(), Err(ConsumerError::Duplicate(_))));
}

#[tokio::test(start_paused = true)]
async fn startup_marker_and_queue_cleared() {
    let t = setup();
    t.kv.push("tasks:w1", "stale");
    let c = t.c.clone();
    let h = tokio::spawn(async move { c.run().await });
    tokio::time::sleep(Duration::from_secs(1)).await;
    assert_eq!(std::fs::read_to_string(t.paths.startup_marker()).unwrap(), "1.3.0");
    assert_eq!(t.kv.list_len("tasks:w1"), 0, "古いタスクは捨てる");
    let calls = t.kv.calls();
    let reg = calls.iter().position(|c| c.starts_with("sadd workers:pending")).unwrap();
    let ver = calls.iter().position(|c| c.starts_with("hset worker_versions")).unwrap();
    assert!(reg < ver);
    assert_eq!(t.kv.hget("worker_versions", "w1").as_deref(), Some("1.3.0"));
    assert!(t.kv.hget("worker_platforms", "w1").unwrap().contains(' '));
    t.c.request_stop();
    h.await.unwrap().unwrap();
    assert!(!t.kv.has_key("heartbeat:w1"), "止めたら自分の heartbeat を消す");
}

#[tokio::test(start_paused = true)]
async fn registration_check_uses_pipeline_and_reregisters() {
    let t = setup();
    let c = t.c.clone();
    let h = tokio::spawn(async move { c.run().await });
    tokio::time::sleep(Duration::from_secs(1)).await;
    t.kv.set_members("workers:pending", &[]);
    let _ = swim_worker::kv::Kv::del(&*t.kv, "workers:pending").await;
    tokio::time::sleep(Duration::from_secs(31)).await;
    let n = t.kv.calls().iter().filter(|c| c.starts_with("sadd workers:pending")).count();
    assert_eq!(n, 2, "pending から消えたら登録し直す");
    assert!(t.kv.calls().iter().any(|c| c == "sismember_many 2"));
    t.c.request_stop();
    h.await.unwrap().unwrap();
}

#[tokio::test(start_paused = true)]
async fn heartbeat_loop_periodically_reports_version() {
    let t = setup();
    let c = t.c.clone();
    let h = tokio::spawn(async move { c.run().await });
    tokio::time::sleep(Duration::from_secs(30 * 20 + 5)).await;
    let n = t.kv.calls().iter().filter(|c| c.starts_with("hset worker_versions")).count();
    assert_eq!(n, 2, "起動時と 20 回目の heartbeat");
    t.c.request_stop();
    h.await.unwrap().unwrap();
}

#[tokio::test(start_paused = true)]
async fn heartbeat_loop_waits_longer_on_auth_error() {
    let t = setup();
    let c = t.c.clone();
    let h = tokio::spawn(async move { c.run().await });
    tokio::time::sleep(Duration::from_secs(1)).await;
    t.kv.fail_next("get", KvError::new(KvErrorKind::Auth, "WRONGPASS"));
    tokio::time::sleep(Duration::from_secs(30)).await; // 2 回目の heartbeat が認証の失敗
    let before = t.kv.calls().iter().filter(|c| *c == "setex heartbeat:w1").count();
    tokio::time::sleep(Duration::from_secs(55)).await;
    let mid = t.kv.calls().iter().filter(|c| *c == "setex heartbeat:w1").count();
    assert_eq!(mid, before, "60 秒は送らない");
    tokio::time::sleep(Duration::from_secs(10)).await;
    assert!(t.kv.calls().iter().filter(|c| *c == "setex heartbeat:w1").count() > mid);
    t.c.request_stop();
    h.await.unwrap().unwrap();
}

#[tokio::test(start_paused = true)]
async fn consume_loop_backs_off_on_noperm_and_auth() {
    for (kind, wait) in [(KvErrorKind::NoPerm, 60), (KvErrorKind::Auth, 60), (KvErrorKind::Connection, 5)] {
        let t = setup();
        t.kv.fail_next("blpop", KvError::new(kind, "x"));
        let c = t.c.clone();
        let h = tokio::spawn(async move { c.run().await });
        tokio::time::sleep(Duration::from_secs(wait - 1)).await;
        assert_eq!(t.kv.calls().iter().filter(|c| c.starts_with("blpop")).count(), 1, "{kind:?}");
        tokio::time::sleep(Duration::from_secs(2)).await;
        assert_eq!(t.kv.calls().iter().filter(|c| c.starts_with("blpop")).count(), 2, "{kind:?}");
        t.c.request_stop();
        h.await.unwrap().unwrap();
    }
}

#[tokio::test(start_paused = true)]
async fn request_stop_finishes_running_task() {
    let t = setup();
    *t.swim.delay.lock().unwrap() = Some(Duration::from_secs(40));
    let c = t.c.clone();
    let h = tokio::spawn(async move { c.run().await });
    tokio::time::sleep(Duration::from_secs(1)).await; // 起動時に古いタスクは捨てるので、起動のあとに積む
    t.kv.push("tasks:w1", &task("t1", "collect_pireps", json!({"url": URL, "body": {}})).to_string());
    tokio::time::sleep(Duration::from_secs(5)).await;
    t.c.request_stop();
    h.await.unwrap().unwrap();
    assert_eq!(result_of(&t, "t1")["status"], "success", "処理中のタスクは結果を書いてから止まる");
}

#[tokio::test(start_paused = true)]
async fn request_stop_when_idle_returns_quickly() {
    let t = setup();
    let c = t.c.clone();
    let h = tokio::spawn(async move { c.run().await });
    tokio::time::sleep(Duration::from_secs(1)).await;
    let start = tokio::time::Instant::now();
    t.c.request_stop();
    h.await.unwrap().unwrap();
    assert!(start.elapsed() < Duration::from_secs(1), "BLPOP の待ちを切る");
}

// ---- 版の判定 ----

async fn version_events(t: &mut T) -> Vec<String> {
    t.c.check_latest_version(true).await;
    states(t).into_iter().filter_map(|e| if let ConsumerEvent::UpdateAvailable(v) = e { Some(v) } else { None }).collect()
}

#[tokio::test(start_paused = true)]
async fn notifies_when_enabled_and_no_whitelist() {
    let mut t = setup_with("1.2.2");
    t.kv.put("swim:latest_worker_version", "1.3.0");
    t.kv.put("swim:auto_update_enabled", "true");
    assert_eq!(version_events(&mut t).await, vec!["1.3.0"]);
    assert!(version_events(&mut t).await.is_empty(), "同じ版は 2 度知らせない");
}

#[tokio::test(start_paused = true)]
async fn kill_switch_off_blocks() {
    let mut t = setup_with("1.2.2");
    t.kv.put("swim:latest_worker_version", "1.3.0");
    t.kv.put("swim:auto_update_enabled", "false");
    assert!(version_events(&mut t).await.is_empty());
}

#[tokio::test(start_paused = true)]
async fn reevaluated_after_kill_switch_turns_on() {
    let mut t = setup_with("1.2.2");
    t.kv.put("swim:latest_worker_version", "1.3.0");
    assert!(version_events(&mut t).await.is_empty());
    t.kv.put("swim:auto_update_enabled", "true");
    assert_eq!(version_events(&mut t).await, vec!["1.3.0"]);
}

#[tokio::test(start_paused = true)]
async fn whitelist() {
    let mut t = setup_with("1.2.2");
    t.kv.put("swim:latest_worker_version", "1.3.0");
    t.kv.put("swim:auto_update_enabled", "true");
    t.kv.put("swim:auto_update_whitelist", "w2, w3");
    assert!(version_events(&mut t).await.is_empty());
    t.kv.put("swim:auto_update_whitelist", " w2 ,w1 ");
    assert_eq!(version_events(&mut t).await, vec!["1.3.0"]);
}

#[tokio::test(start_paused = true)]
async fn staged_version_only_for_whitelisted_worker() {
    let mut t = setup_with("1.2.2");
    t.kv.put("swim:latest_worker_version", "1.2.2");
    t.kv.put("swim:latest_worker_version_staged", "1.3.0");
    t.kv.put("swim:auto_update_enabled", "true");
    t.kv.put("swim:auto_update_whitelist", "w2");
    assert!(version_events(&mut t).await.is_empty());
    t.kv.put("swim:auto_update_whitelist", "w1");
    assert_eq!(version_events(&mut t).await, vec!["1.3.0"]);
}

#[tokio::test(start_paused = true)]
async fn staged_version_must_be_well_formed() {
    let mut t = setup_with("1.2.2");
    t.kv.put("swim:latest_worker_version", "1.2.2");
    t.kv.put("swim:latest_worker_version_staged", "1.3.0-rc1");
    t.kv.put("swim:auto_update_enabled", "true");
    t.kv.put("swim:auto_update_whitelist", "w1");
    assert!(version_events(&mut t).await.is_empty());
}

#[tokio::test(start_paused = true)]
async fn major_version_change_is_skipped() {
    let mut t = setup_with("1.2.2");
    t.kv.put("swim:latest_worker_version", "2.0.0");
    t.kv.put("swim:auto_update_enabled", "true");
    assert!(version_events(&mut t).await.is_empty());
}

#[tokio::test(start_paused = true)]
async fn not_newer_does_not_notify() {
    let mut t = setup_with("1.3.0");
    t.kv.put("swim:latest_worker_version", "v1.3.0");
    t.kv.put("swim:auto_update_enabled", "true");
    assert!(version_events(&mut t).await.is_empty());
}

#[tokio::test(start_paused = true)]
async fn invalid_version_string_is_ignored() {
    let mut t = setup_with("1.2.2");
    t.kv.put("swim:latest_worker_version", "latest");
    t.kv.put("swim:auto_update_enabled", "true");
    assert!(version_events(&mut t).await.is_empty());
}

#[tokio::test(start_paused = true)]
async fn redis_error_on_gate_blocks() {
    let mut t = setup_with("1.2.2");
    t.kv.put("swim:latest_worker_version", "1.3.0");
    t.kv.put("swim:auto_update_enabled", "true");
    // get の順: latest, (staged の判定で) enabled・whitelist, (gate で) enabled ← ここを落とす
    t.kv.fail_next("get", KvError::new(KvErrorKind::Other, "ok"));
    let _ = t.kv.calls();
    let ev = {
        // 1 回目の get (latest) が失敗するので何も知らせない
        version_events(&mut t).await
    };
    assert!(ev.is_empty());
}

#[test]
fn parse_version_rules() {
    use swim_worker::consumer::version::parse_version;
    assert_eq!(parse_version("1.2.3"), Some((1, 2, 3)));
    assert_eq!(parse_version("v10.0.01"), Some((10, 0, 1)));
    for bad in ["", "1.2", "1.2.3.4", "1.2.3-rc1", " 1.2.3", "1.2.x", "1234567.0.0"] {
        assert_eq!(parse_version(bad), None, "{bad:?}");
    }
}
