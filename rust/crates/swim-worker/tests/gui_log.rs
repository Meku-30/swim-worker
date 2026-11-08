//! GUI のログ (Python の RotatingFileHandler 5MB × 3 世代と、画面のログ欄の形)
use std::sync::{Arc, Mutex};

use swim_worker::gui_log::{GuiLogLayer, RotatingFile};
use tracing_subscriber::layer::SubscriberExt;

#[test]
fn rotates_at_limit_keeping_3_backups() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("swim-worker.log");
    let mut f = RotatingFile::new(p.clone(), 100, 3);
    for i in 0..30 {
        f.write_line(&format!("line {i:03} xxxxxxxxxxxxxxxxxxxx"));
    }
    assert!(p.exists());
    for n in 1..=3 {
        assert!(d.path().join(format!("swim-worker.log.{n}")).exists(), "{n}");
    }
    assert!(!d.path().join("swim-worker.log.4").exists());
    assert!(std::fs::metadata(&p).unwrap().len() <= 100);
    assert!(std::fs::read_to_string(&p).unwrap().contains("line 029"));
}

#[test]
fn layer_writes_python_format_to_file_and_short_to_sink() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("swim-worker.log");
    let seen = Arc::new(Mutex::new(Vec::<String>::new()));
    let s2 = seen.clone();
    let layer = GuiLogLayer::new(RotatingFile::new(p.clone(), 5 * 1024 * 1024, 3), Box::new(move |l| s2.lock().unwrap().push(l)));
    let sub = tracing_subscriber::registry().with(layer);
    tracing::subscriber::with_default(sub, || {
        tracing::info!(target: "swim_worker.consumer", "タスク完了: {}", "collect_notams");
        tracing::debug!(target: "swim_worker.consumer", "出さない");
    });
    let file = std::fs::read_to_string(&p).unwrap();
    let re_ok = file.lines().count() == 1 && file.contains(" [INFO] swim_worker.consumer: タスク完了: collect_notams");
    assert!(re_ok, "{file}");
    let s = seen.lock().unwrap();
    assert_eq!(s.len(), 1);
    // 画面は "%H:%M:%S 本文"
    assert_eq!(s[0].len(), "12:34:56 タスク完了: collect_notams".len(), "{}", s[0]);
    assert!(s[0].ends_with(" タスク完了: collect_notams"));
}
