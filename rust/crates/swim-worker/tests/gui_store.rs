//! GUI の設定の置き場 (Python の settings_store.py・keyring の WinVaultKeyring・gui_helpers.next_rollback_state・updater.SnoozeStore)
use std::collections::BTreeMap;
use std::path::Path;

use chrono::{Duration, TimeZone, Utc};
use serde_json::{json, Value};
use swim_worker::gui_store::secrets::{CredBackend, FakeCred, Keyring};
use swim_worker::gui_store::{
    next_rollback_state, quote_value, read_env_values, update_env_file, GuiPrefs, Snooze, SettingsStore,
};

fn golden() -> Value {
    serde_json::from_str(
        &std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/../../tests/golden/gui_store/env.json")).unwrap(),
    )
    .unwrap()
}

#[test]
fn reads_env_like_python() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join(".env");
    for c in golden()["read"].as_array().unwrap() {
        std::fs::write(&p, c["text"].as_str().unwrap()).unwrap();
        let got: BTreeMap<String, String> = read_env_values(&p).into_iter().collect();
        let want: BTreeMap<String, String> =
            c["values"].as_object().unwrap().iter().map(|(k, v)| (k.clone(), v.as_str().unwrap().into())).collect();
        assert_eq!(got, want, "{}", c["text"]);
    }
}

#[test]
fn updates_env_like_python() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join(".env");
    for c in golden()["update"].as_array().unwrap() {
        let before = c["before"].as_str().unwrap();
        if before.is_empty() {
            let _ = std::fs::remove_file(&p);
        } else {
            std::fs::write(&p, before).unwrap();
        }
        let upd: Vec<(String, Option<String>)> = c["updates"]
            .as_object()
            .unwrap()
            .iter()
            .map(|(k, v)| (k.clone(), v.as_str().map(String::from)))
            .collect();
        update_env_file(&p, &upd).unwrap();
        assert_eq!(std::fs::read_to_string(&p).unwrap(), c["after"].as_str().unwrap(), "{before:?}");
    }
    for q in golden()["quote"].as_array().unwrap() {
        assert_eq!(quote_value(q["in"].as_str().unwrap()), q["out"].as_str().unwrap());
    }
}

/// M1 の見本: Python の keyring は 2 つ目以降を `<ユーザー名>@swim-worker` に動かす
#[test]
fn keyring_matches_python_winvault() {
    let g: Value = serde_json::from_str(
        &std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/../../tests/golden/keyring_windows.json")).unwrap(),
    )
    .unwrap();
    let users: Vec<&str> = g["users"].as_array().unwrap().iter().map(|u| u.as_str().unwrap()).collect();
    let cred = FakeCred::default();
    let kr = Keyring::new(&cred);
    for u in &users {
        kr.set_password("swim-worker", u, "dummy-value-日本").unwrap();
    }
    let mut got: Vec<(String, String)> = cred.dump().into_iter().map(|(t, u, _)| (t, u)).collect();
    got.sort();
    let mut want: Vec<(String, String)> = g["credentials"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| (c["target_name"].as_str().unwrap().into(), c["user_name"].as_str().unwrap().into()))
        .collect();
    want.sort();
    assert_eq!(got, want);
    for u in &users {
        assert_eq!(kr.get_password("swim-worker", u).unwrap().as_deref(), Some("dummy-value-日本"));
    }
    kr.delete_password("swim-worker", users[0]).unwrap();
    assert_eq!(kr.get_password("swim-worker", users[0]).unwrap(), None);
    assert!(kr.get_password("swim-worker", users[1]).unwrap().is_some());
}

/// 書けても読み戻せない資格情報ストア
#[derive(Default)]
struct WriteOnly(FakeCred);

impl CredBackend for WriteOnly {
    fn read(&self, _t: &str) -> std::io::Result<Option<(String, String)>> {
        Ok(None)
    }
    fn write(&self, t: &str, u: &str, v: &str) -> std::io::Result<()> {
        self.0.write(t, u, v)
    }
    fn delete(&self, t: &str) -> std::io::Result<()> {
        self.0.delete(t)
    }
}

fn env_with_passwords(dir: &Path) {
    std::fs::write(
        dir.join(".env"),
        "REDIS_HOST='h'\nREDIS_USERNAME='u'\nREDIS_PASSWORD='rp'\nSWIM_USERNAME='s'\nSWIM_PASSWORD='sp'\nWORKER_NAME='w1'\nAUTO_CONNECT='true'\n",
    )
    .unwrap();
}

#[test]
fn migrates_env_password_only_when_read_back() {
    let d = tempfile::tempdir().unwrap();
    env_with_passwords(d.path());
    let wo = WriteOnly::default();
    let st = SettingsStore::new(d.path().join(".env"), Some(&wo));
    let (f, auto) = st.load().unwrap();
    assert_eq!(f.redis_password, "rp");
    assert_eq!(auto, Some(true));
    let env = std::fs::read_to_string(d.path().join(".env")).unwrap();
    assert!(env.contains("REDIS_PASSWORD='rp'"), "読み戻せないので .env に残す: {env}");

    let ok = FakeCred::default();
    let st = SettingsStore::new(d.path().join(".env"), Some(&ok));
    let (f, _) = st.load().unwrap();
    assert_eq!((f.redis_password.as_str(), f.swim_password.as_str()), ("rp", "sp"));
    let env = std::fs::read_to_string(d.path().join(".env")).unwrap();
    assert!(!env.contains("PASSWORD"), "移せたら .env から消す: {env}");
    let id = read_env_values(&d.path().join(".env")).into_iter().find(|(k, _)| k == "SWIM_WORKER_SECRET_ID").unwrap().1;
    assert_eq!(id.len(), 32);
    // 次の起動は資格情報マネージャーから読む
    let (f, _) = SettingsStore::new(d.path().join(".env"), Some(&ok)).load().unwrap();
    assert_eq!(f.swim_password, "sp");
}

#[test]
fn save_keeps_passwords_out_of_env_and_python_shape() {
    let d = tempfile::tempdir().unwrap();
    std::fs::write(d.path().join(".env"), "# 手で足した行\nEXTRA=1\n").unwrap();
    let cred = FakeCred::default();
    let st = SettingsStore::new(d.path().join(".env"), Some(&cred));
    let f = swim_worker::gui_store::Fields {
        redis_host: " h ".into(),
        redis_password: " p w ".into(),
        worker_name: "w1".into(),
        ..Default::default()
    };
    assert_eq!(st.save(&f, false).unwrap(), "keyring");
    let env = std::fs::read_to_string(d.path().join(".env")).unwrap();
    assert!(env.starts_with("# 手で足した行\nEXTRA=1\n"), "{env}");
    assert!(env.contains("REDIS_HOST='h'\n") && env.contains("REDIS_PORT='6380'\n") && env.contains("AUTO_CONNECT='false'\n"));
    assert!(!env.contains("PASSWORD"));
    let (back, _) = st.load().unwrap();
    assert_eq!(back.redis_password, " p w ", "パスワードは strip しない");
}

#[test]
fn save_without_keyring_writes_env() {
    let d = tempfile::tempdir().unwrap();
    let st = SettingsStore::new(d.path().join(".env"), None);
    let f = swim_worker::gui_store::Fields { swim_password: "it's".into(), ..Default::default() };
    assert_eq!(st.save(&f, true).unwrap(), "env");
    assert!(std::fs::read_to_string(d.path().join(".env")).unwrap().contains("SWIM_PASSWORD='it\\'s'"));
}

#[test]
fn secret_unreadable_notice() {
    let d = tempfile::tempdir().unwrap();
    std::fs::write(d.path().join(".env"), "SWIM_WORKER_SECRET_ID='0123456789abcdef0123456789abcdef'\nREDIS_HOST='h'\n").unwrap();
    let st = SettingsStore::new(d.path().join(".env"), None);
    st.load().unwrap();
    assert_eq!(st.missing_secrets(), vec!["redis_password", "swim_password"]);
}

#[test]
fn gui_prefs_keeps_unknown_keys() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("data/gui_settings.json");
    std::fs::create_dir_all(p.parent().unwrap()).unwrap();
    std::fs::write(&p, r#"{"auto_update": true, "future_key": [1, 2]}"#).unwrap();
    let mut g = GuiPrefs::load(&p);
    assert!(g.auto_update());
    g.set_settings_collapsed(true);
    g.save(&p).unwrap();
    let v: Value = serde_json::from_str(&std::fs::read_to_string(&p).unwrap()).unwrap();
    assert_eq!(v["future_key"], json!([1, 2]));
    assert_eq!(v["settings_collapsed"], json!(true));
    // 壊れたファイルは空として読む
    std::fs::write(&p, "{broken").unwrap();
    assert!(!GuiPrefs::load(&p).auto_update());
}

#[test]
fn rollback_count_disables_auto_update_after_2() {
    let mut g = GuiPrefs::from_value(json!({"auto_update": true}));
    assert!(!next_rollback_state(&mut g, "v1.3.1"));
    assert!(g.auto_update());
    assert!(next_rollback_state(&mut g, "v1.3.1"), "同じ版で 2 回 → 自動適用を止める");
    assert!(!g.auto_update());
    assert_eq!(g.value()["rollback_count"], json!({"v1.3.1": 2}));
}

#[test]
fn snooze_24h_and_other_version_clears() {
    let d = tempfile::tempdir().unwrap();
    let s = Snooze::new(d.path().join("data/update_snooze.json"));
    let now = Utc.with_ymd_and_hms(2026, 11, 8, 0, 0, 0).unwrap();
    s.set("1.3.1", now);
    assert!(s.is_snoozed("1.3.1", now + Duration::hours(23)));
    assert!(!s.is_snoozed("1.3.1", now + Duration::hours(24)), "期限切れ");
    assert!(!s.path().exists(), "期限切れは消す");
    s.set("1.3.1", now);
    assert!(!s.is_snoozed("1.3.2", now), "別の版");
    assert!(!s.path().exists());
    // Python が書いた形 (isoformat、+00:00) を読める
    std::fs::write(s.path(), r#"{"version": "1.3.1", "until": "2026-11-09T00:00:00.123456+00:00"}"#).unwrap();
    assert!(s.is_snoozed("1.3.1", now));
}

/// 本物の資格情報マネージャーで、試しのサービス名に書いて読み、消す (CI の Windows)
#[cfg(windows)]
#[test]
fn real_windows_credential_roundtrip() {
    use swim_worker::gui_store::secrets::WinCred;
    let cred = WinCred;
    let kr = Keyring::new(&cred);
    let svc = format!("swim-worker-test-{}", std::process::id());
    kr.set_password(&svc, "id:REDIS_PASSWORD", "値-1").unwrap();
    kr.set_password(&svc, "id:SWIM_PASSWORD", "値-2").unwrap();
    assert_eq!(kr.get_password(&svc, "id:REDIS_PASSWORD").unwrap().as_deref(), Some("値-1"));
    assert_eq!(kr.get_password(&svc, "id:SWIM_PASSWORD").unwrap().as_deref(), Some("値-2"));
    kr.delete_password(&svc, "id:REDIS_PASSWORD").unwrap();
    kr.delete_password(&svc, "id:SWIM_PASSWORD").unwrap();
    assert_eq!(kr.get_password(&svc, "id:SWIM_PASSWORD").unwrap(), None);
}

/// 最終レビュー I-1: 起動時の片付けは期限切れ・壊れたものだけ消す (待ちの間の snooze は残す)
#[test]
fn cleanup_expired_keeps_valid_snooze() {
    let d = tempfile::tempdir().unwrap();
    let s = Snooze::new(d.path().join("data/update_snooze.json"));
    let now = Utc.with_ymd_and_hms(2026, 11, 8, 0, 0, 0).unwrap();
    s.set("1.3.1", now);
    s.cleanup_expired(now + Duration::hours(1));
    assert!(s.is_snoozed("1.3.1", now + Duration::hours(1)), "待ちの間は残す");
    s.cleanup_expired(now + Duration::hours(25));
    assert!(!s.path().exists(), "期限切れは消す");
}

/// 最終レビュー I-2: 巻き戻しの記録は起動したらすぐ数えて snooze を置き、記録を消す (ダイアログは後で)
#[test]
fn rollback_marker_applied_at_startup() {
    use swim_worker::gui_store::apply_rollback_marker;
    let d = tempfile::tempdir().unwrap();
    let data = d.path().join("data");
    std::fs::create_dir_all(&data).unwrap();
    // cmd の echo が書く形 (末尾の空白・CRLF)
    std::fs::write(data.join(".update_rollback.json"), "{\"rolled_back_from\":\"v1.3.1\",\"reason\":\"move_failed\"} \r\n").unwrap();
    std::fs::write(data.join("gui_settings.json"), r#"{"auto_update": true, "rollback_count": {"1.3.1": 1}}"#).unwrap();
    let s = Snooze::new(data.join("update_snooze.json"));
    let now = Utc.with_ymd_and_hms(2026, 11, 8, 0, 0, 0).unwrap();
    let r = apply_rollback_marker(&data, &s, now).unwrap();
    assert_eq!((r.from.as_str(), r.reason.as_str(), r.disabled), ("1.3.1", "move_failed", true));
    assert!(!data.join(".update_rollback.json").exists());
    assert!(s.is_snoozed("1.3.1", now));
    let g = GuiPrefs::load(&data.join("gui_settings.json"));
    assert!(!g.auto_update());
    assert!(apply_rollback_marker(&data, &s, now).is_none(), "記録が無ければ何もしない");
}

/// 最終レビュー I-6: UTF-8 でない .env は空として扱わず、読み・保存をやめる (中身を捨てない)
#[test]
fn non_utf8_env_is_an_error_and_left_untouched() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join(".env");
    let raw = b"# \x93\xfa\x96\x7b\x8c\xea\nREDIS_HOST='h'\n".to_vec(); // CP932 のコメント
    std::fs::write(&p, &raw).unwrap();
    let cred = FakeCred::default();
    let st = SettingsStore::new(p.clone(), Some(&cred));
    assert!(st.load().is_err());
    assert!(st.save(&swim_worker::gui_store::Fields::default(), true).is_err());
    assert_eq!(std::fs::read(&p).unwrap(), raw);
}
