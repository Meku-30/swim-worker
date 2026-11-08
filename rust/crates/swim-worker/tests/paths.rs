//! 置き場所 (Python の swim_worker/paths.py の frozen のときと同じ) と多重起動の防止
use std::path::PathBuf;

use swim_worker::paths;
use swim_worker::single_instance::{acquire_at, AcquireError};

#[test]
fn paths_match_python_layout() {
    let base = PathBuf::from("/opt/swim-worker");
    let p = paths::Paths::new(base.clone());
    assert_eq!(p.data_dir(), base.join("data"));
    assert_eq!(p.startup_marker(), base.join("data/.startup_ok"));
    assert_eq!(p.last_instance_token(), base.join("data/.last_instance_token"));
    assert_eq!(p.cookie_file(""), base.join("data/.swim_cookies.json"));
    assert_eq!(p.cookie_file("/tmp/c.json"), PathBuf::from("/tmp/c.json"));
    assert_eq!(p.lock(), base.join("data/swim-worker.lock"));
}

#[test]
fn base_dir_override_for_tests() {
    // SWIM_WORKER_BASE は開発・テスト用 (本番は実行ファイルのフォルダ)
    let p = paths::Paths::from_env_or_exe(Some("/x/y".into()));
    assert_eq!(p.data_dir(), PathBuf::from("/x/y/data"));
}

#[test]
fn second_lock_is_already_running() {
    let d = tempfile::tempdir().unwrap();
    let lock = d.path().join("data/swim-worker.lock");
    let first = acquire_at(&lock).unwrap();
    assert!(matches!(acquire_at(&lock), Err(AcquireError::AlreadyRunning(_))));
    drop(first);
    assert!(acquire_at(&lock).is_ok(), "放したら取れる");
}

#[test]
fn lock_writes_pid() {
    let d = tempfile::tempdir().unwrap();
    let lock = d.path().join("data/swim-worker.lock");
    let l = acquire_at(&lock).unwrap();
    // Windows はロック中のファイルを別の取っ手から読めない (Python の msvcrt.locking も同じ) ので、放してから読む
    drop(l);
    assert_eq!(std::fs::read_to_string(&lock).unwrap(), format!("{}\n", std::process::id()));
}
