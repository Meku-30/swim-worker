//! Worker の設定。名前・既定値は Python 版 (swim_worker/config.py) と同じ。
//! 決め方も pydantic-settings と同じ: 環境変数 (名前の大文字小文字は問わない) > `.env` > 既定値
use std::collections::HashMap;
use std::path::Path;

use thiserror::Error;

#[derive(Debug, Error)]
pub enum ConfigError {
    #[error("設定 {0} がありません")]
    Missing(String),
    #[error("設定 {key} の値が不正です: {reason}")]
    Invalid { key: String, reason: String },
}

#[derive(Debug, Clone, PartialEq)]
pub struct Settings {
    pub redis_host: String,
    pub redis_port: u16,
    /// ACL のユーザー名。空なら default ユーザー
    pub redis_username: String,
    pub redis_password: String,
    /// CA のファイル。空なら埋め込みの CA
    pub redis_ca_cert: String,
    pub swim_username: String,
    pub swim_password: String,
    pub worker_name: String,
    pub heartbeat_interval: u64,
    pub cookie_file: String,
    pub request_delay_median: f64,
    pub request_delay_p99: f64,
    pub request_delay_clip_min: f64,
    pub request_delay_clip_max: f64,
    pub task_hard_timeout: f64,
    pub redis_socket_timeout: f64,
    pub redis_blpop_timeout: f64,
}

/// Worker 名は Redis の CLIENT SETNAME とキー名に使うため ASCII に限る (config.py の WORKER_NAME_RE)
pub const WORKER_NAME_RULE_MESSAGE: &str =
    "Worker 名は半角英数字・ピリオド・アンダースコア・ハイフンのみ (1〜32 文字) です";

pub fn valid_worker_name(s: &str) -> bool {
    (1..=32).contains(&s.len()) && s.bytes().all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
}

impl Settings {
    /// カレントディレクトリの `.env` (無ければ無視) と環境変数から作る
    pub fn load(env_file: &Path) -> Result<Settings, ConfigError> {
        let dotenv = std::fs::read_to_string(env_file).map(|t| crate::dotenv::parse(&t)).unwrap_or_default();
        let env: HashMap<String, String> = std::env::vars().collect();
        Self::from_sources(&dotenv, &env)
    }

    pub fn from_sources(dotenv: &[(String, String)], env: &HashMap<String, String>) -> Result<Settings, ConfigError> {
        let get = |name: &str| -> Option<String> {
            env.iter()
                .find(|(k, _)| k.eq_ignore_ascii_case(name))
                .or_else(|| dotenv.iter().rev().find(|(k, _)| k.eq_ignore_ascii_case(name)).map(|(k, v)| (k, v)))
                .map(|(_, v)| v.clone())
        };
        let req = |name: &str| get(name).ok_or_else(|| ConfigError::Missing(name.to_string()));
        let num = |name: &str, default: f64| -> Result<f64, ConfigError> {
            match get(name) {
                None => Ok(default),
                Some(v) => v.trim().parse::<f64>().map_err(|e| ConfigError::Invalid { key: name.into(), reason: e.to_string() }),
            }
        };
        let int = |name: &str, default: u64| -> Result<u64, ConfigError> {
            match get(name) {
                None => Ok(default),
                Some(v) => v.trim().parse::<u64>().map_err(|e| ConfigError::Invalid { key: name.into(), reason: e.to_string() }),
            }
        };
        // 足りないものは config.py の欄の順で最初の 1 つを返す
        let redis_host = req("redis_host")?;
        let port = int("redis_port", 6380)?;
        let redis_password = req("redis_password")?;
        let swim_username = req("swim_username")?;
        let swim_password = req("swim_password")?;
        let worker_name = req("worker_name")?;
        if !valid_worker_name(&worker_name) {
            return Err(ConfigError::Invalid { key: "worker_name".into(), reason: WORKER_NAME_RULE_MESSAGE.into() });
        }
        Ok(Settings {
            redis_host,
            redis_port: u16::try_from(port).map_err(|e| ConfigError::Invalid { key: "redis_port".into(), reason: e.to_string() })?,
            redis_username: get("redis_username").unwrap_or_default(),
            redis_password,
            redis_ca_cert: get("redis_ca_cert").unwrap_or_default(),
            swim_username,
            swim_password,
            worker_name,
            heartbeat_interval: int("heartbeat_interval", 30)?,
            cookie_file: get("cookie_file").unwrap_or_default(),
            request_delay_median: num("request_delay_median", 4.0)?,
            request_delay_p99: num("request_delay_p99", 15.0)?,
            request_delay_clip_min: num("request_delay_clip_min", 1.5)?,
            request_delay_clip_max: num("request_delay_clip_max", 25.0)?,
            task_hard_timeout: num("task_hard_timeout", 300.0)?,
            redis_socket_timeout: num("redis_socket_timeout", 30.0)?,
            redis_blpop_timeout: num("redis_blpop_timeout", 20.0)?,
        })
    }
}
