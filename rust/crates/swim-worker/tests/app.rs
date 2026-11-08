//! 起動・終了コード・止め方・DNS の確認 (Python の __main__.py・dns_check.py)
use std::path::PathBuf;
use std::process::Command;
use std::sync::Arc;
use std::time::Duration;

use swim_worker::app::{wait_for_redis, SignalCounter};
use swim_worker::dns_check::{blocked_nameservers, DENIED_NETWORKS};
use swim_worker::kv::fake::FakeKv;
use swim_worker::kv::{KvError, KvErrorKind};

const BIN: &str = env!("CARGO_BIN_EXE_swim-worker");

#[tokio::test(start_paused = true)]
async fn exit_1_after_10_connect_failures() {
    let kv = FakeKv::default();
    for _ in 0..10 {
        kv.fail_next("ping", KvError::new(KvErrorKind::Connection, "refused"));
    }
    let start = tokio::time::Instant::now();
    assert!(!wait_for_redis(&kv).await);
    // 1+2+4+8+16+30+30+30+30 秒
    assert_eq!(start.elapsed().as_secs(), 151);
}

#[tokio::test(start_paused = true)]
async fn connects_after_retries() {
    let kv = FakeKv::default();
    kv.fail_next("ping", KvError::new(KvErrorKind::Connection, "refused"));
    kv.fail_next("ping", KvError::new(KvErrorKind::Connection, "refused"));
    assert!(wait_for_redis(&kv).await);
    assert_eq!(kv.calls().iter().filter(|c| c.starts_with("ping")).count(), 3);
}

#[test]
fn exit_1_when_settings_missing() {
    let d = tempfile::tempdir().unwrap();
    let st = Command::new(BIN).current_dir(d.path()).env_clear().env("SWIM_WORKER_BASE", d.path()).status().unwrap();
    assert_eq!(st.code(), Some(1));
}

#[test]
fn exit_2_when_already_running() {
    let d = tempfile::tempdir().unwrap();
    let _held = swim_worker::single_instance::acquire_at(&d.path().join("data/swim-worker.lock")).unwrap();
    let st = Command::new(BIN).current_dir(d.path()).env_clear().env("SWIM_WORKER_BASE", d.path()).status().unwrap();
    assert_eq!(st.code(), Some(2));
}

#[test]
fn exit_3_when_lock_dir_unwritable() {
    let d = tempfile::tempdir().unwrap();
    // data がディレクトリでなくファイルなので、ロックファイルを作れない
    std::fs::write(d.path().join("data"), "x").unwrap();
    let st = Command::new(BIN).current_dir(d.path()).env_clear().env("SWIM_WORKER_BASE", d.path()).status().unwrap();
    assert_eq!(st.code(), Some(3));
}

#[test]
fn log_lines_look_like_python() {
    let d = tempfile::tempdir().unwrap();
    let out = Command::new(BIN).current_dir(d.path()).env_clear().env("SWIM_WORKER_BASE", d.path()).output().unwrap();
    let err = String::from_utf8_lossy(&out.stderr);
    let line = err.lines().next().unwrap_or("");
    // 2026-11-08 12:34:56,789 [ERROR] __main__: 設定を読めません: ...
    let re = regex_lite(line);
    assert!(re, "{line}");
}

fn regex_lite(l: &str) -> bool {
    let b = l.as_bytes();
    l.len() > 30
        && b[4] == b'-' && b[7] == b'-' && b[10] == b' ' && b[13] == b':' && b[16] == b':' && b[19] == b','
        && l[24..].starts_with("[ERROR] __main__: 設定を読めません")
}

#[tokio::test(start_paused = true)]
async fn first_signal_graceful_second_immediate() {
    use swim_worker::consumer::{ConsumerConfig, SwimApi, TaskConsumer};
    struct NoSwim;
    #[async_trait::async_trait]
    impl SwimApi for NoSwim {
        async fn execute_api(&self, _: &str, _: &serde_json::Value, _: bool) -> Result<serde_json::Value, swim_worker::swim::SwimError> {
            tokio::time::sleep(Duration::from_secs(1000)).await;
            Ok(serde_json::json!({}))
        }
        async fn fetch_public_get(&self, _: &str, _: &[(String, String)], _: &[(String, String)]) -> Result<serde_json::Value, swim_worker::swim::SwimError> {
            Ok(serde_json::json!({}))
        }
    }
    let d = tempfile::tempdir().unwrap();
    let kv = Arc::new(FakeKv::default());
    let cfg = ConsumerConfig {
        worker_name: "w1".into(),
        heartbeat_interval: 30,
        pre_task: swim_worker::delays::PreTask::new(0.01, 0.02, 0.0, 0.01),
        task_hard_timeout: 300.0,
        blpop_timeout: 20.0,
        paths: swim_worker::paths::Paths::new(PathBuf::from(d.path())),
    };
    let c = Arc::new(TaskConsumer::new(kv.clone(), Arc::new(NoSwim), cfg, None));
    let c2 = c.clone();
    let h = tokio::spawn(async move { c2.run().await });
    tokio::time::sleep(Duration::from_secs(1)).await;
    kv.push("tasks:w1", r#"{"task_id": "t1", "job_type": "collect_pireps", "params": {"url": "https://web.swim.mlit.go.jp/x", "body": {}}}"#);
    tokio::time::sleep(Duration::from_secs(2)).await;
    let s = SignalCounter::default();
    s.on_signal(&c); // 1 回目: 処理中のタスクを待つ
    tokio::time::sleep(Duration::from_secs(10)).await;
    assert!(!h.is_finished(), "1 回目は処理中のタスクを待つ");
    s.on_signal(&c); // 2 回目: すぐ止まる
    tokio::time::sleep(Duration::from_millis(10)).await;
    assert!(h.is_finished());
}

#[test]
fn denied_networks_match_unit() {
    let unit = std::fs::read_to_string(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../scripts/swim-worker.service")).unwrap();
    let in_unit: Vec<&str> = unit
        .lines()
        .filter_map(|l| l.strip_prefix("IPAddressDeny="))
        .flat_map(str::split_whitespace)
        .collect();
    assert_eq!(in_unit, DENIED_NETWORKS);
}

#[test]
fn blocked_nameservers_cases() {
    let resolv = "nameserver 127.0.0.53\nnameserver 192.0.2.1\nnameserver 172.16.5.5\nnameserver 172.20.0.1\nnameserver fe80::1%eth0\n# nameserver 172.16.0.9\n";
    let dropin = "[Service]\nIPAddressAllow=172.16.5.5 203.0.113.7\n";
    assert_eq!(blocked_nameservers(resolv, dropin), vec!["172.20.0.1", "fe80::1"]);
    assert!(blocked_nameservers(resolv, "IPAddressAllow=172.16.0.0/12 fe80::/10").is_empty());
}

#[test]
fn name_resolution_errors() {
    use swim_worker::dns_check::is_name_resolution_error;
    assert!(is_name_resolution_error("failed to lookup address information: Name or service not known"));
    assert!(!is_name_resolution_error("Connection refused"));
}
