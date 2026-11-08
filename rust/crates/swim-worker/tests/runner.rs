//! GUI から使う Worker の動かし役 (Python の worker_runner.py)
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use serde_json::{json, Value};
use swim_worker::consumer::{ConsumerConfig, SwimApi};
use swim_worker::delays::PreTask;
use swim_worker::kv::fake::FakeKv;
use swim_worker::kv::{Kv, KvError, KvErrorKind};
use swim_worker::paths::Paths;
use swim_worker::runner::{run_loop, Control, RunnerEvent, RunnerState, StopReason};
use swim_worker::swim::SwimError;

struct NoSwim;

#[async_trait]
impl SwimApi for NoSwim {
    async fn execute_api(&self, _u: &str, _b: &Value, _r: bool) -> Result<Value, SwimError> {
        Ok(json!({}))
    }
    async fn fetch_public_get(&self, _u: &str, _p: &[(String, String)], _h: &[(String, String)]) -> Result<Value, SwimError> {
        Ok(json!({}))
    }
}

fn cfg(dir: &tempfile::TempDir) -> ConsumerConfig {
    ConsumerConfig {
        worker_name: "w1".into(),
        heartbeat_interval: 30,
        pre_task: PreTask::new(0.01, 0.02, 0.0, 0.01),
        task_hard_timeout: 300.0,
        blpop_timeout: 1.0,
        paths: Paths::new(dir.path().to_path_buf()),
    }
}

fn events() -> (Arc<Mutex<Vec<RunnerEvent>>>, impl Fn(RunnerEvent) + Send + Sync + 'static) {
    let v = Arc::new(Mutex::new(Vec::new()));
    let v2 = v.clone();
    (v, move |e| v2.lock().unwrap().push(e))
}

fn waits(ev: &[RunnerEvent]) -> Vec<u64> {
    ev.iter()
        .filter_map(|e| match e {
            RunnerEvent::State(RunnerState::Waiting { delay_secs, .. }) => Some(*delay_secs),
            _ => None,
        })
        .collect()
}

fn stopped(ev: &[RunnerEvent]) -> Option<StopReason> {
    ev.iter().rev().find_map(|e| match e {
        RunnerEvent::Stopped(r) => Some(r.clone()),
        _ => None,
    })
}

#[tokio::test(start_paused = true)]
async fn runner_retries_with_backoff_until_300s() {
    let d = tempfile::tempdir().unwrap();
    let kv = Arc::new(FakeKv::default());
    for _ in 0..11 {
        kv.fail_next("ping", KvError::new(KvErrorKind::Connection, "refused"));
    }
    let ctl = Arc::new(Control::default());
    let (ev, sink) = events();
    let c2 = ctl.clone();
    let h = tokio::spawn(run_loop(kv.clone() as Arc<dyn Kv>, Arc::new(NoSwim), cfg(&d), c2, Box::new(sink)));
    // 11 回失敗して 12 回目でつながる → 起動の印
    for _ in 0..2000 {
        tokio::time::sleep(Duration::from_secs(1)).await;
        if d.path().join("data/.startup_ok").exists() {
            break;
        }
    }
    ctl.request_stop();
    h.await.unwrap();
    let ev = ev.lock().unwrap();
    assert_eq!(waits(&ev), vec![1, 2, 4, 8, 16, 32, 64, 128, 256, 300, 300]);
    assert_eq!(stopped(&ev), Some(StopReason::User));
}

#[tokio::test(start_paused = true)]
async fn runner_stops_without_retry_on_auth_error() {
    let d = tempfile::tempdir().unwrap();
    let kv = Arc::new(FakeKv::default());
    kv.fail_next("ping", KvError::new(KvErrorKind::Auth, "WRONGPASS"));
    let (ev, sink) = events();
    run_loop(kv as Arc<dyn Kv>, Arc::new(NoSwim), cfg(&d), Arc::new(Control::default()), Box::new(sink)).await;
    let ev = ev.lock().unwrap();
    assert!(waits(&ev).is_empty());
    assert!(matches!(stopped(&ev), Some(StopReason::AuthFailed(_))));
}

#[tokio::test(start_paused = true)]
async fn runner_stops_without_retry_on_duplicate() {
    let d = tempfile::tempdir().unwrap();
    let kv = Arc::new(FakeKv::default());
    // 別のプロセスが同じ名前の heartbeat を持っている
    kv.put("heartbeat:w1", "other-token");
    let (ev, sink) = events();
    run_loop(kv as Arc<dyn Kv>, Arc::new(NoSwim), cfg(&d), Arc::new(Control::default()), Box::new(sink)).await;
    let ev = ev.lock().unwrap();
    assert!(matches!(stopped(&ev), Some(StopReason::Duplicate(_))), "{ev:?}");
}

#[tokio::test(start_paused = true)]
async fn runner_request_stop_while_waiting_ends_promptly() {
    let d = tempfile::tempdir().unwrap();
    let kv = Arc::new(FakeKv::default());
    for _ in 0..5 {
        kv.fail_next("ping", KvError::new(KvErrorKind::Connection, "refused"));
    }
    let ctl = Arc::new(Control::default());
    let (ev, sink) = events();
    let h = tokio::spawn(run_loop(kv as Arc<dyn Kv>, Arc::new(NoSwim), cfg(&d), ctl.clone(), Box::new(sink)));
    tokio::time::sleep(Duration::from_millis(1500)).await;
    let t = tokio::time::Instant::now();
    ctl.request_stop();
    h.await.unwrap();
    assert!(t.elapsed() < Duration::from_secs(1), "待ちの途中でも止まる");
    assert_eq!(stopped(&ev.lock().unwrap()), Some(StopReason::User));
}

#[test]
fn platform_string_windows_11_from_build_22000() {
    use swim_worker::consumer::platform_from_parts;
    let g: Value = serde_json::from_str(
        &std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/../../tests/golden/platform.json")).unwrap(),
    )
    .unwrap();
    for c in g.as_array().unwrap() {
        let got = platform_from_parts(c["system"].as_str().unwrap(), c["release"].as_str().unwrap(),
            c["version"].as_str().unwrap(), c["machine"].as_str().unwrap());
        assert_eq!(got, c["expected"].as_str().unwrap(), "{c}");
    }
}
