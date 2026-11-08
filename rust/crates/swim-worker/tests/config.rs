//! 設定: .env を python-dotenv と同じに読み、環境変数 > .env > 既定値 (pydantic-settings と同じ)
use std::collections::HashMap;
use std::path::PathBuf;

use serde_json::Value;
use swim_worker::config::{ConfigError, Settings};
use swim_worker::dotenv;

fn golden() -> Vec<Value> {
    let p = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/dotenv_cases.json");
    serde_json::from_str(&std::fs::read_to_string(p).unwrap()).unwrap()
}

#[test]
fn dotenv_golden_cases() {
    let cases = golden();
    assert_eq!(cases.len(), 14);
    for c in cases {
        let got = dotenv::parse(&format!("{}\n", c["line"].as_str().unwrap()));
        let want: Vec<(String, String)> = c["values"]
            .as_object()
            .unwrap()
            .iter()
            .map(|(k, v)| (k.clone(), v.as_str().unwrap().to_string()))
            .collect();
        assert_eq!(got, want, "{}", c["line"]);
    }
}

const REQUIRED: &str = "REDIS_HOST=r.example\nREDIS_PASSWORD=p\nSWIM_USERNAME=u\nSWIM_PASSWORD=s\nWORKER_NAME=w-1\n";

#[test]
fn defaults_match_python() {
    let s = Settings::from_sources(&dotenv::parse(REQUIRED), &HashMap::new()).unwrap();
    assert_eq!(s.redis_port, 6380);
    assert_eq!(s.redis_username, "");
    assert_eq!(s.redis_ca_cert, "");
    assert_eq!(s.heartbeat_interval, 30);
    assert_eq!(s.cookie_file, "");
    assert_eq!(s.request_delay_median, 4.0);
    assert_eq!(s.request_delay_p99, 15.0);
    assert_eq!(s.request_delay_clip_min, 1.5);
    assert_eq!(s.request_delay_clip_max, 25.0);
    assert_eq!(s.task_hard_timeout, 300.0);
    assert_eq!(s.redis_socket_timeout, 30.0);
    assert_eq!(s.redis_blpop_timeout, 20.0);
    assert_eq!(s.worker_name, "w-1");
}

#[test]
fn env_overrides_dotenv() {
    let env: HashMap<String, String> = [("WORKER_NAME".into(), "from-env".into()), ("redis_port".into(), "6390".into())].into();
    let s = Settings::from_sources(&dotenv::parse(REQUIRED), &env).unwrap();
    assert_eq!(s.worker_name, "from-env");
    assert_eq!(s.redis_port, 6390, "環境変数の名前は大文字小文字を区別しない");
}

#[test]
fn missing_required_is_error() {
    let err = Settings::from_sources(&dotenv::parse("REDIS_HOST=x\n"), &HashMap::new()).unwrap_err();
    assert!(matches!(err, ConfigError::Missing(ref k) if k == "redis_password"), "{err:?}");
}

#[test]
fn worker_name_rule() {
    for bad in ["", "日本", "a b", &"x".repeat(33)] {
        let env: HashMap<String, String> = [("WORKER_NAME".into(), bad.to_string())].into();
        assert!(matches!(Settings::from_sources(&dotenv::parse(REQUIRED), &env), Err(ConfigError::Invalid { .. })), "{bad:?}");
    }
}

#[test]
fn bad_number_is_error() {
    let env: HashMap<String, String> = [("REDIS_PORT".into(), "abc".into())].into();
    assert!(matches!(Settings::from_sources(&dotenv::parse(REQUIRED), &env), Err(ConfigError::Invalid { .. })));
}
