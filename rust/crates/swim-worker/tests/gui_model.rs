//! 画面の文言・判定 (Windows に依存しない部分。Python の gui_helpers・gui.py と同じ)
use serde_json::Value;
use swim_worker::gui_model::{
    rollback_message, status_for_runner, status_for_stop, task_state_text, update_prompt_kind, validate_fields, LogBuffer,
    LogEdit, PromptKind,
};
use swim_worker::gui_store::Fields;
use swim_worker::runner::{RunnerState, StopReason};

fn golden() -> Value {
    serde_json::from_str(&std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/../../tests/golden/gui_model.json")).unwrap()).unwrap()
}

#[test]
fn validate_fields_matches_python() {
    for c in golden()["validate"].as_array().unwrap() {
        let f = &c["fields"];
        let s = |k: &str| f[k].as_str().unwrap().to_string();
        let fields = Fields {
            redis_host: s("redis_host"),
            redis_username: s("redis_username"),
            redis_password: s("redis_password"),
            swim_username: s("swim_username"),
            swim_password: s("swim_password"),
            worker_name: s("worker_name"),
        };
        assert_eq!(validate_fields(&fields).as_deref(), c["message"].as_str(), "{f}");
    }
}

#[test]
fn task_state_text_matches_python() {
    for c in golden()["task_state"].as_array().unwrap() {
        let a = c["args"].as_array().unwrap();
        let got = task_state_text(a[0].as_str().unwrap(), a[1].as_str().unwrap(), a[2].as_u64().unwrap(), a[3].as_u64().unwrap());
        assert_eq!(got, c["text"].as_str().unwrap());
    }
}

#[test]
fn log_buffer_trims_1000_to_800() {
    let mut b = LogBuffer::default();
    for i in 0..1000 {
        assert!(matches!(b.push(format!("l{i}")), LogEdit::Append(_)));
    }
    match b.push("l1000".into()) {
        LogEdit::Reset(text) => {
            assert_eq!(text.lines().count(), 800);
            assert!(text.starts_with("l201\r\n") && text.ends_with("l1000\r\n"));
        }
        e => panic!("{e:?}"),
    }
    assert!(matches!(b.push("next".into()), LogEdit::Append(ref s) if s == "next\r\n"));
}

#[test]
fn state_texts_japanese() {
    assert_eq!(status_for_runner(&RunnerState::Connecting), "● 起動中...");
    assert_eq!(status_for_runner(&RunnerState::Running), "● 接続中 (タスク待ち)");
    assert_eq!(
        status_for_runner(&RunnerState::Waiting { attempt: 2, delay_secs: 4 }),
        "Redis 再接続待ち (2 回目、4 秒後に再試行)"
    );
    assert_eq!(status_for_stop(&StopReason::User), "停止中");
    assert_eq!(status_for_stop(&StopReason::Duplicate("x".into())), "重複起動エラー");
    assert_eq!(status_for_stop(&StopReason::AuthFailed("x".into())), "Redis 認証エラー (設定欄のユーザー名・パスワードを確認)");
    assert_eq!(status_for_stop(&StopReason::Error("x".into())), "エラーで停止しました (ログを確認)");
}

#[test]
fn prompt_kind() {
    assert_eq!(update_prompt_kind(true, true), None);
    assert_eq!(update_prompt_kind(false, true), Some(PromptKind::Countdown));
    assert_eq!(update_prompt_kind(false, false), Some(PromptKind::Prompt));
}

#[test]
fn rollback_message_mentions_reason_and_disable() {
    let m = rollback_message("1.3.1", "move_failed", true);
    assert!(m.starts_with("v1.3.1 へのアップデートが起動確認に失敗したため"));
    assert!(m.contains("置き換えに失敗"));
    assert!(m.contains("自動更新を停止しました"));
    assert!(m.ends_with("詳細は swim-worker-update.log を確認してください。"));
    assert!(!rollback_message("1.3.1", "", false).contains("置き換え"));
}
