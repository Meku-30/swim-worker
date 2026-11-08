//! タスク処理 (Python の swim_worker/consumer.py の TaskConsumer)。
//! Redis のキー・結果の形・待ち時間・ログの文言は Python 版と同じにする。
pub mod result;
pub mod version;

use std::collections::BTreeSet;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use rand::rngs::StdRng;
use rand::SeedableRng;
use serde_json::{json, Map, Value};
use tokio::sync::{mpsc::UnboundedSender, Notify};
use tokio::time::Instant;

use crate::delays::{sleep_secs, uniform, PreTask};
use crate::kv::{Kv, KvError, KvErrorKind};
use crate::paths::Paths;
use crate::swim::{SwimClient, SwimError};
use version::{gate_reason_ja, parse_version, whitelist_names, Version};

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
const LOG: &str = "swim_worker.consumer";

pub const RESULT_TTL: u64 = 3600;
const NOPERM_RETRY_DELAY: f64 = 60.0;
const AUTH_RETRY_DELAY: f64 = 60.0;
const HEARTBEAT_TTL_MULTIPLIER: u64 = 2;
const RESULT_WRITE_RETRY_DELAYS: [f64; 2] = [1.0, 2.0];
const VERSION_CHECK_INTERVAL: u32 = 20;
const PARSE_CACHE_TTL: Duration = Duration::from_secs(60);

pub const LATEST_VERSION_KEY: &str = "swim:latest_worker_version";
pub const AUTO_UPDATE_ENABLED_KEY: &str = "swim:auto_update_enabled";
pub const AUTO_UPDATE_WHITELIST_KEY: &str = "swim:auto_update_whitelist";
pub const STAGED_VERSION_KEY: &str = "swim:latest_worker_version_staged";
pub const PARSE_ENABLED_KEY: &str = "swim:parse_enabled";
pub const PARSE_DISABLED_WORKERS_PREFIX: &str = "swim:parse_disabled_workers";
pub const RELEASES_URL: &str = "https://github.com/Meku-30/swim-worker/releases/latest";

/// SWIM の API (テストで偽物を差すための継ぎ目)
#[async_trait]
pub trait SwimApi: Send + Sync {
    async fn execute_api(&self, url: &str, body: &Value, retry_on_auth_error: bool) -> Result<Value, SwimError>;
    async fn fetch_public_get(&self, url: &str, params: &[(String, String)], headers: &[(String, String)])
        -> Result<Value, SwimError>;
}

#[async_trait]
impl SwimApi for SwimClient {
    async fn execute_api(&self, url: &str, body: &Value, retry: bool) -> Result<Value, SwimError> {
        SwimClient::execute_api(self, url, body, retry).await
    }
    async fn fetch_public_get(&self, url: &str, params: &[(String, String)], headers: &[(String, String)])
        -> Result<Value, SwimError> {
        SwimClient::fetch_public_get(self, url, params, headers).await
    }
}

/// GUI に伝えること
#[derive(Debug, Clone, PartialEq)]
pub enum ConsumerEvent {
    UpdateAvailable(String),
    /// state: "processing" | "idle"
    TaskState { state: &'static str, job_type: String, total: u64, errors: u64 },
}

#[derive(Debug, thiserror::Error)]
pub enum ConsumerError {
    /// 同じ Worker 名で別のプロセスが動いている
    #[error("{0}")]
    Duplicate(String),
    #[error("{0}")]
    Kv(#[from] KvError),
}

#[derive(Debug, Clone)]
pub struct ConsumerConfig {
    pub worker_name: String,
    pub heartbeat_interval: u64,
    pub pre_task: PreTask,
    pub task_hard_timeout: f64,
    pub blpop_timeout: f64,
    pub paths: Paths,
}

impl ConsumerConfig {
    pub fn from_settings(s: &crate::config::Settings, paths: Paths) -> Self {
        Self {
            worker_name: s.worker_name.clone(),
            heartbeat_interval: s.heartbeat_interval,
            pre_task: PreTask::new(s.request_delay_median, s.request_delay_p99, s.request_delay_clip_min, s.request_delay_clip_max),
            task_hard_timeout: s.task_hard_timeout,
            blpop_timeout: s.redis_blpop_timeout,
            paths,
        }
    }
}

#[derive(Default)]
struct Inner {
    task_total: u64,
    task_errors: u64,
    notified_version: Option<String>,
    gate_logged: Option<(String, String)>,
    invalid_version_logged: Option<String>,
    parse_cache_until: Option<Instant>,
    parse_enabled: BTreeSet<String>,
}

pub struct TaskConsumer {
    kv: Arc<dyn Kv>,
    swim: Arc<dyn SwimApi>,
    cfg: ConsumerConfig,
    events: Option<UnboundedSender<ConsumerEvent>>,
    running: AtomicBool,
    executing: AtomicBool,
    /// 待っている BLPOP を止める (停止の要求で、処理中のタスクがないとき)
    wake_consume: Notify,
    /// すぐに止める (2 回目のシグナル)
    hard_stop: Notify,
    instance_token: String,
    inner: Mutex<Inner>,
    rng: Mutex<StdRng>,
    version: String,
}

fn lock<T>(m: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

impl TaskConsumer {
    pub fn new(kv: Arc<dyn Kv>, swim: Arc<dyn SwimApi>, cfg: ConsumerConfig, events: Option<UnboundedSender<ConsumerEvent>>) -> Self {
        Self {
            kv,
            swim,
            cfg,
            events,
            running: AtomicBool::new(false),
            executing: AtomicBool::new(false),
            wake_consume: Notify::new(),
            hard_stop: Notify::new(),
            instance_token: uuid::Uuid::new_v4().simple().to_string(),
            inner: Mutex::new(Inner::default()),
            rng: Mutex::new(StdRng::from_os_rng()),
            version: VERSION.to_string(),
        }
    }

    /// テスト用: 自分の版を替える
    pub fn with_version(mut self, v: &str) -> Self {
        self.version = v.into();
        self
    }

    /// テスト用: 乱数の種
    pub fn with_seed(self, seed: u64) -> Self {
        *lock(&self.rng) = StdRng::seed_from_u64(seed);
        self
    }

    pub fn instance_token(&self) -> &str {
        &self.instance_token
    }

    pub fn counts(&self) -> (u64, u64) {
        let i = lock(&self.inner);
        (i.task_total, i.task_errors)
    }

    fn name(&self) -> &str {
        &self.cfg.worker_name
    }

    fn notify_state(&self, state: &'static str, job_type: &str) {
        if let Some(tx) = &self.events {
            let (total, errors) = self.counts();
            let _ = tx.send(ConsumerEvent::TaskState { state, job_type: job_type.into(), total, errors });
        }
    }

    // ---- 登録・版 ----

    pub async fn register(&self) -> Result<(), KvError> {
        if self.kv.sismember("workers:approved", self.name()).await? {
            tracing::info!(target: LOG, "Worker '{}' は承認済み（再登録スキップ）", self.name());
            return Ok(());
        }
        self.kv.sadd("workers:pending", self.name()).await?;
        tracing::info!(target: LOG, "Worker '{}' を登録しました (pending)", self.name());
        Ok(())
    }

    pub async fn report_version(&self) {
        let platform = platform_string();
        let r = async {
            self.kv.hset("worker_versions", self.name(), &self.version).await?;
            self.kv.hset("worker_platforms", self.name(), &platform).await
        }
        .await;
        match r {
            Ok(()) => tracing::info!(target: LOG, "Workerバージョン: v{}, platform: {platform}", self.version),
            Err(e) => tracing::warn!(target: LOG, "バージョン登録エラー: {e}"),
        }
    }

    async fn update_gate(&self, current: Version, latest: Version) -> Option<&'static str> {
        if latest.0 != current.0 {
            return Some("major");
        }
        let (enabled, whitelist) = match (self.kv.get(AUTO_UPDATE_ENABLED_KEY).await, self.kv.get(AUTO_UPDATE_WHITELIST_KEY).await) {
            (Ok(e), Ok(w)) => (e, w),
            (Err(e), _) | (_, Err(e)) => {
                tracing::debug!(target: LOG, "自動更新の設定を読めない: {e}");
                return Some("error");
            }
        };
        if enabled.as_deref() != Some("true") {
            return Some("disabled");
        }
        if let Some(w) = whitelist.filter(|w| !w.trim().is_empty()) {
            if !whitelist_names(&w).iter().any(|n| n == self.name()) {
                return Some("not_in_whitelist");
            }
        }
        None
    }

    async fn staged_version(&self) -> Option<Version> {
        let enabled = self.kv.get(AUTO_UPDATE_ENABLED_KEY).await.ok()?;
        let whitelist = self.kv.get(AUTO_UPDATE_WHITELIST_KEY).await.ok()?;
        if enabled.as_deref() != Some("true") {
            return None;
        }
        let w = whitelist.filter(|w| !w.trim().is_empty())?;
        if !whitelist_names(&w).iter().any(|n| n == self.name()) {
            return None;
        }
        parse_version(&self.kv.get(STAGED_VERSION_KEY).await.ok()?.unwrap_or_default())
    }

    /// Coordinator が記録した最新版と比べ、新しければ (止めていなければ) 知らせる。quiet は定期の確認
    pub async fn check_latest_version(&self, quiet: bool) {
        let raw = match self.kv.get(LATEST_VERSION_KEY).await {
            Ok(r) => r,
            Err(e) => {
                tracing::debug!(target: LOG, "バージョンチェックエラー: {e}");
                return;
            }
        };
        let mut latest = raw.as_deref().filter(|r| !r.is_empty()).and_then(parse_version);
        if let Some(r) = raw.as_deref().filter(|r| !r.is_empty()) {
            let mut i = lock(&self.inner);
            if latest.is_none() && i.invalid_version_logged.as_deref() != Some(r) {
                i.invalid_version_logged = Some(r.into());
                let short: String = r.chars().take(40).collect();
                tracing::warn!(target: LOG, "Coordinator の最新版の値が不正なため無視: {short:?}");
            }
        }
        if let Some(s) = self.staged_version().await {
            if latest.is_none_or(|l| s > l) {
                latest = Some(s);
            }
        }
        let Some(latest) = latest else { return };
        let tag = format!("{}.{}.{}", latest.0, latest.1, latest.2);
        let current = parse_version(&self.version);
        if current.is_none_or(|c| latest <= c) {
            if !quiet {
                tracing::info!(target: LOG, "バージョン最新 (v{})", self.version);
            }
            return;
        }
        let current = current.expect("current");
        if quiet && lock(&self.inner).notified_version.as_deref() == Some(tag.as_str()) {
            return;
        }
        if let Some(reason) = self.update_gate(current, latest).await {
            let mut i = lock(&self.inner);
            let key = (tag.clone(), reason.to_string());
            if i.gate_logged.as_ref() != Some(&key) {
                i.gate_logged = Some(key);
                tracing::info!(target: LOG, "新しいバージョン v{tag} があります (自動更新は保留: {})", gate_reason_ja(reason));
            }
            return;
        }
        lock(&self.inner).notified_version = Some(tag.clone());
        tracing::warn!(target: LOG, "新しいバージョンが利用可能です: v{} → v{tag}  {RELEASES_URL}", self.version);
        if let Some(tx) = &self.events {
            let _ = tx.send(ConsumerEvent::UpdateAvailable(tag));
        }
    }

    // ---- 多重起動の防止 (heartbeat) ----

    fn ttl(&self) -> u64 {
        self.cfg.heartbeat_interval * HEARTBEAT_TTL_MULTIPLIER
    }

    fn hb_key(&self) -> String {
        format!("heartbeat:{}", self.name())
    }

    async fn acquire_instance_lock(&self) -> Result<(), ConsumerError> {
        let key = self.hb_key();
        self.claim_orphaned_lock(&key).await;
        if self.kv.set_nx_ex(&key, &self.instance_token, self.ttl()).await? {
            self.persist_instance_token();
            return Ok(());
        }
        let max_wait = self.ttl() + 10;
        tracing::info!(target: LOG, "heartbeat キーが残存中。前プロセスの TTL 失効を最大{max_wait}秒待機します...");
        let mut elapsed = 0;
        while elapsed < max_wait {
            tokio::time::sleep(Duration::from_secs(5)).await;
            elapsed += 5;
            if self.kv.set_nx_ex(&key, &self.instance_token, self.ttl()).await? {
                self.persist_instance_token();
                tracing::info!(target: LOG, "heartbeat キー取得成功（{elapsed}秒待機）");
                return Ok(());
            }
        }
        Err(ConsumerError::Duplicate(format!(
            "worker_name '{}' は既に別プロセスで稼働中です。 別の名前を使うか、もう一方を停止してください。",
            self.name()
        )))
    }

    /// 前回の自分が残した heartbeat なら消す (保存した token と一致したときだけ)
    async fn claim_orphaned_lock(&self, key: &str) {
        let Ok(last) = std::fs::read_to_string(self.cfg.paths.last_instance_token()) else { return };
        let last = last.trim();
        if last.is_empty() {
            return;
        }
        let Ok(Some(cur)) = self.kv.get(key).await else { return };
        if cur != last {
            return;
        }
        match self.kv.del(key).await {
            Ok(_) => tracing::info!(target: LOG, "前回プロセスの instance lock を解放 (token 一致)"),
            Err(e) => tracing::debug!(target: LOG, "orphan lock 解放失敗 (無視): {e}"),
        }
    }

    fn persist_instance_token(&self) {
        let p = self.cfg.paths.last_instance_token();
        let r = p.parent().map_or(Ok(()), std::fs::create_dir_all).and_then(|_| std::fs::write(&p, &self.instance_token));
        if let Err(e) = r {
            tracing::debug!(target: LOG, "instance token 永続化失敗 (無視): {e}");
        }
    }

    /// 自分の token のとき (または切れているとき) だけ延ばす。GET で確かめてから SETEX (EVAL は ACL にない)
    pub async fn send_heartbeat(&self) -> Result<(), ConsumerError> {
        let key = self.hb_key();
        if let Some(cur) = self.kv.get(&key).await? {
            if cur != self.instance_token {
                return Err(ConsumerError::Duplicate(format!(
                    "heartbeat:{} を別のプロセスが持っています (同じ Worker 名で別の Worker が動いています)",
                    self.name()
                )));
            }
        }
        self.kv.setex(&key, self.ttl(), self.instance_token.as_bytes()).await?;
        Ok(())
    }

    async fn release_instance_lock(&self) {
        let key = self.hb_key();
        let Ok(cur) = self.kv.get(&key).await else { return };
        if cur.as_deref() != Some(self.instance_token.as_str()) {
            return;
        }
        if let Err(e) = self.kv.del(&key).await {
            tracing::debug!(target: LOG, "instance lock 解放エラー (無視): {e}");
        }
    }

    async fn ensure_registered(&self) -> Result<(), KvError> {
        let r = self
            .kv
            .sismember_many(&[("workers:approved".into(), self.name().into()), ("workers:pending".into(), self.name().into())])
            .await?;
        if !r.contains(&Some(true)) {
            self.register().await?;
        }
        Ok(())
    }

    // ---- 解析するか (parsers/__init__.py supports) ----

    async fn refresh_parse_cache(&self) {
        let r: Result<BTreeSet<String>, KvError> = async {
            let mut enabled: BTreeSet<String> = self.kv.smembers(PARSE_ENABLED_KEY).await?.into_iter().collect();
            if !enabled.is_empty() {
                let jobs: Vec<String> = enabled.iter().cloned().collect();
                let pairs: Vec<(String, String)> =
                    jobs.iter().map(|j| (format!("{PARSE_DISABLED_WORKERS_PREFIX}:{j}"), self.name().to_string())).collect();
                let flags = self.kv.sismember_many(&pairs).await?;
                for (j, f) in jobs.iter().zip(flags) {
                    // 個々の失敗は「除外なし」
                    if f == Some(true) {
                        enabled.remove(j);
                    }
                }
            }
            Ok(enabled)
        }
        .await;
        let mut i = lock(&self.inner);
        if let Ok(e) = r {
            i.parse_enabled = e;
        }
        // Redis のエラーのときは前回の値のまま (安全側)
        i.parse_cache_until = Some(Instant::now() + PARSE_CACHE_TTL);
    }

    async fn parse_supported(&self, job_type: &str) -> bool {
        let stale = lock(&self.inner).parse_cache_until.is_none_or(|t| Instant::now() >= t);
        if stale {
            self.refresh_parse_cache().await;
        }
        lock(&self.inner).parse_enabled.contains(job_type)
    }

    // ---- タスク ----

    fn make_result(&self, task_id: &str, status: &str, data: Option<Value>, error: Option<String>, fmt: Option<&str>, kind: Option<&str>) -> Value {
        result::make_result(task_id, self.name(), status, data, error, fmt, kind, result::now_iso())
    }

    fn result_key(&self, task_id: &str) -> String {
        format!("results:{}:{task_id}", self.name())
    }

    /// 結果を書く。接続の一時的な失敗は 1 秒・2 秒あけて 3 回まで。書けたら true
    async fn write_result(&self, task_id: &str, result: &Value) -> bool {
        let payload = result::encode(result);
        let key = self.result_key(task_id);
        let delays: [Option<f64>; 3] = [Some(RESULT_WRITE_RETRY_DELAYS[0]), Some(RESULT_WRITE_RETRY_DELAYS[1]), None];
        for (attempt, delay) in delays.iter().enumerate() {
            match self.kv.setex(&key, RESULT_TTL, &payload).await {
                Ok(()) => return true,
                Err(e) if e.is_connection() => match delay {
                    None => {
                        tracing::error!(target: LOG, "結果の書き込みに失敗 ({} 回試行): {task_id} — {e}", attempt + 1);
                        return false;
                    }
                    Some(d) => {
                        tracing::warn!(target: LOG, "結果の書き込みに失敗、{d:.0}秒後に再試行: {task_id} — {e}");
                        sleep_secs(*d).await;
                    }
                },
                Err(e) => {
                    tracing::error!(target: LOG, "結果の書き込みに失敗: {task_id} — {e}");
                    return false;
                }
            }
        }
        false
    }

    async fn finish_task(&self, task_id: &str, result: &Value, success: bool) {
        let written = self.write_result(task_id, result).await;
        {
            let mut i = lock(&self.inner);
            i.task_total += 1;
            if !(success && written) {
                i.task_errors += 1;
            }
        }
        self.notify_state("idle", "");
    }

    async fn run_capability_test(&self, task_id: &str, params: &Value) -> Value {
        let tests = params.get("tests").and_then(Value::as_array).cloned().unwrap_or_default();
        let mut results = Map::new();
        for t in tests {
            let jt = t.get("job_type").and_then(Value::as_str).unwrap_or("").to_string();
            let url = t.get("url").and_then(Value::as_str).unwrap_or("").to_string();
            let body = t.get("body").filter(|b| py_truthy(b)).cloned().unwrap_or_else(|| json!({}));
            // テストの間に短い待ち (一気に送らない)
            let d = uniform(&mut *lock(&self.rng), 1.0, 3.0);
            sleep_secs(d).await;
            // 403 は「未権限」としてすぐ返す (再ログイン・Cookie の破棄をしない)
            let entry = match self.swim.execute_api(&url, &body, false).await {
                Ok(_) => {
                    tracing::info!(target: LOG, "capability OK: {jt}");
                    json!({"ok": true, "error": null, "reason": null})
                }
                Err(e @ SwimError::Unauthorized { .. }) => {
                    tracing::info!(target: LOG, "capability NG (未権限): {jt} — {e}");
                    json!({"ok": false, "error": truncate(&e.to_string(), 300), "reason": "unauthorized"})
                }
                Err(e) => {
                    tracing::warn!(target: LOG, "capability 一時障害: {jt} — {e}");
                    json!({"ok": false, "error": truncate(&e.to_string(), 300), "reason": "transient"})
                }
            };
            results.insert(jt, entry);
        }
        let ok = results.values().filter(|r| r["ok"] == json!(true)).count();
        let short: String = task_id.chars().take(8).collect();
        tracing::info!(target: LOG, "capability_test 完了: {short} (ok={ok}, ng={})", results.len() - ok);
        self.make_result(task_id, "success", Some(json!({"capabilities": results})), None, None, None)
    }

    async fn run_api_task(&self, task_id: &str, job_type: &str, params: &Value) -> (Value, bool) {
        let d = self.cfg.pre_task.sample(&mut *lock(&self.rng));
        tracing::debug!(target: LOG, "リクエスト前遅延: {d:.1}秒");
        sleep_secs(d).await;
        let data = async {
            let url = params.get("url").and_then(Value::as_str).ok_or_else(|| "'url'".to_string())?;
            let method = params.get("method").and_then(Value::as_str).unwrap_or("POST");
            if method == "GET" {
                let p = str_pairs(params.get("params"));
                let h = str_pairs(params.get("headers"));
                self.swim.fetch_public_get(url, &p, &h).await.map_err(SwimOrMsg::Swim)
            } else {
                let body = params.get("body").ok_or_else(|| "'body'".to_string())?;
                self.swim.execute_api(url, body, true).await.map_err(SwimOrMsg::Swim)
            }
        }
        .await;
        let data = match data {
            Ok(d) => d,
            Err(SwimOrMsg::Swim(SwimError::RedirectOutside(m))) => {
                let e = SwimError::RedirectOutside(m).to_string();
                tracing::error!(target: LOG, "タスク失敗: {task_id} — {e}");
                return (self.make_result(task_id, "error", None, Some(e), None, Some("redirect_outside")), false);
            }
            Err(e) => {
                let e = e.to_string();
                tracing::error!(target: LOG, "タスク失敗: {task_id} — {e}");
                return (self.make_result(task_id, "error", None, Some(e), None, None), false);
            }
        };
        let mut result = self.make_result(task_id, "success", Some(data.clone()), None, None, None);
        if self.parse_supported(job_type).await {
            match data {
                Value::Object(m) => match swim_parsers::parsers::parse_for_job_type(job_type, m, Some(params)) {
                    Ok(recs) => {
                        let parsed = Value::Array(recs.into_iter().map(Value::Object).collect());
                        result = self.make_result(task_id, "success", Some(parsed), None, Some("parsed"), None);
                    }
                    Err(e) => tracing::warn!(target: LOG, "Worker パース失敗、raw 送信にフォールバック ({job_type}): {}", e.0),
                },
                _ => tracing::warn!(target: LOG, "Worker パース失敗、raw 送信にフォールバック ({job_type}): AttributeError"),
            }
        }
        tracing::info!(target: LOG, "タスク成功: {task_id}");
        (result, true)
    }

    /// タスクを実行し、結果を Redis に書く
    pub async fn execute_task(&self, task: &Value) {
        let task_id = task.get("task_id").and_then(Value::as_str).filter(|s| !s.is_empty());
        let job_type = task.get("job_type").and_then(Value::as_str).filter(|s| !s.is_empty());
        let (Some(task_id), Some(job_type)) = (task_id, job_type) else {
            let s: String = task.to_string().chars().take(200).collect();
            tracing::error!(target: LOG, "不正なタスク（task_id/job_type欠落）: {s}");
            return;
        };
        let empty = json!({});
        let params = task.get("params").filter(|p| py_truthy(p)).unwrap_or(&empty);
        tracing::info!(target: LOG, "タスク実行開始: {task_id} (type={job_type})");
        self.notify_state("processing", job_type);
        let (result, success) = if job_type == "capability_test" {
            (self.run_capability_test(task_id, params).await, true)
        } else {
            self.run_api_task(task_id, job_type, params).await
        };
        self.finish_task(task_id, &result, success).await;
    }

    /// 取り出した 1 件を実行する。強制の時間切れでも error の結果を書く
    async fn run_one(&self, raw: &str) -> Result<(), String> {
        let task: Value = serde_json::from_str(raw).map_err(|e| e.to_string())?;
        let limit = Duration::from_secs_f64(self.cfg.task_hard_timeout);
        if tokio::time::timeout(limit, self.execute_task(&task)).await.is_err() {
            let task_id = task.get("task_id").and_then(Value::as_str).filter(|s| !s.is_empty());
            tracing::error!(
                target: LOG,
                "タスク強制タイムアウト ({:.0}秒超過、consume_loop継続): {}",
                self.cfg.task_hard_timeout,
                task_id.unwrap_or("None")
            );
            match task_id {
                Some(id) => {
                    let r = self.make_result(
                        id,
                        "error",
                        None,
                        Some(format!("Worker 側の強制タイムアウト ({:.0}秒)", self.cfg.task_hard_timeout)),
                        None,
                        None,
                    );
                    self.finish_task(id, &r, false).await;
                }
                None => {
                    let mut i = lock(&self.inner);
                    i.task_total += 1;
                    i.task_errors += 1;
                    drop(i);
                    self.notify_state("idle", "");
                }
            }
        }
        Ok(())
    }

    // ---- ループ ----

    async fn heartbeat_loop(&self) -> Result<(), ConsumerError> {
        let mut counter = 0u32;
        // 停止の要求のあとも、処理中のタスクが終わるまでは続ける
        while self.running.load(Ordering::SeqCst) || self.executing.load(Ordering::SeqCst) {
            let r: Result<(), ConsumerError> = async {
                self.send_heartbeat().await?;
                self.ensure_registered().await?;
                counter += 1;
                if counter >= VERSION_CHECK_INTERVAL {
                    counter = 0;
                    self.check_latest_version(true).await;
                    // Redis が空になったときのために版も入れ直す
                    self.report_version().await;
                }
                Ok(())
            }
            .await;
            match r {
                Err(e @ ConsumerError::Duplicate(_)) => return Err(e),
                Err(ConsumerError::Kv(e)) if e.kind == KvErrorKind::Auth => {
                    tracing::error!(
                        target: LOG,
                        "Redis の認証に失敗（ハートビート、REDIS_USERNAME / REDIS_PASSWORD を確認）、{AUTH_RETRY_DELAY:.0}秒後にリトライ: {e}"
                    );
                    sleep_secs(AUTH_RETRY_DELAY).await;
                    continue;
                }
                Err(ConsumerError::Kv(e)) if e.is_connection() => {
                    tracing::warn!(target: LOG, "Redis接続エラー（ハートビート）、5秒後にリトライ: {e}");
                    sleep_secs(5.0).await;
                    continue;
                }
                Err(e) => tracing::warn!(target: LOG, "ハートビート送信失敗: {e}"),
                Ok(()) => {}
            }
            tokio::time::sleep(Duration::from_secs(self.cfg.heartbeat_interval)).await;
        }
        Ok(())
    }

    async fn consume_loop(&self) {
        let queue = format!("tasks:{}", self.name());
        while self.running.load(Ordering::SeqCst) {
            let item = tokio::select! {
                r = self.kv.blpop(&queue, self.cfg.blpop_timeout) => r,
                _ = self.wake_consume.notified() => break,
            };
            match item {
                Ok(None) => continue,
                Ok(Some(raw)) => {
                    // ここから結果を書き終えるまでは、停止の要求でも中断しない
                    self.executing.store(true, Ordering::SeqCst);
                    let r = self.run_one(&raw).await;
                    self.executing.store(false, Ordering::SeqCst);
                    if let Err(e) = r {
                        tracing::error!(target: LOG, "コンシューマーエラー: {e}");
                        sleep_secs(1.0).await;
                    }
                }
                Err(e) if e.kind == KvErrorKind::NoPerm => {
                    tracing::error!(
                        target: LOG,
                        "Redis の権限がありません (REDIS_USERNAME と管理者の ACL 設定を確認)、{NOPERM_RETRY_DELAY:.0}秒後にリトライ: {e}"
                    );
                    sleep_secs(NOPERM_RETRY_DELAY).await;
                }
                Err(e) if e.kind == KvErrorKind::Auth => {
                    tracing::error!(
                        target: LOG,
                        "Redis の認証に失敗（コンシューマー、REDIS_USERNAME / REDIS_PASSWORD を確認）、{AUTH_RETRY_DELAY:.0}秒後にリトライ: {e}"
                    );
                    sleep_secs(AUTH_RETRY_DELAY).await;
                }
                Err(e) if e.is_connection() => {
                    tracing::warn!(target: LOG, "Redis接続エラー（コンシューマー）、5秒後にリトライ: {e}");
                    sleep_secs(5.0).await;
                }
                Err(e) => {
                    tracing::error!(target: LOG, "コンシューマーエラー: {e}");
                    sleep_secs(1.0).await;
                }
            }
        }
    }

    /// 起動から停止まで。`on_started` は起動の印を書いたあと 1 回呼ぶ
    pub async fn run(&self) -> Result<(), ConsumerError> {
        self.running.store(true, Ordering::SeqCst);
        self.acquire_instance_lock().await?;
        // 前回の停止中に残ったタスクは捨てる (Coordinator が時間切れで配り直している)
        match self.kv.del(&format!("tasks:{}", self.name())).await {
            Ok(n) if n > 0 => tracing::info!(target: LOG, "起動時に古いタスクキューをクリアしました"),
            Ok(_) => {}
            Err(e) if e.is_connection() => tracing::warn!(target: LOG, "起動時キュークリア失敗（続行）: {e}"),
            Err(e) => return Err(e.into()),
        }
        self.register().await?;
        self.report_version().await;
        self.check_latest_version(false).await;
        tracing::info!(target: LOG, "Worker '{}' 起動", self.name());
        // 起動成功の印 (Redis に繋がり登録まで済んだ)。差し替えの成否の判定が見る
        let marker = self.cfg.paths.startup_marker();
        if let Err(e) = marker.parent().map_or(Ok(()), std::fs::create_dir_all).and_then(|_| std::fs::write(&marker, &self.version)) {
            tracing::debug!(target: LOG, "startup marker 作成失敗 (無視): {e}");
        }
        if !self.running.load(Ordering::SeqCst) {
            self.release_instance_lock().await;
            tracing::info!(target: LOG, "Worker '{}' 停止", self.name());
            return Ok(());
        }
        let r = tokio::select! {
            r = self.heartbeat_loop() => r,
            _ = self.consume_loop() => Ok(()),
            _ = self.hard_stop.notified() => Ok(()),
        };
        self.running.store(false, Ordering::SeqCst);
        self.release_instance_lock().await;
        tracing::info!(target: LOG, "Worker '{}' 停止", self.name());
        r
    }

    /// 停止の要求: 新しいタスクは取らず、処理中のタスクは結果を書いてから止まる
    pub fn request_stop(&self) {
        if !self.running.swap(false, Ordering::SeqCst) {
            return;
        }
        if self.executing.load(Ordering::SeqCst) {
            tracing::info!(target: LOG, "停止要求: 処理中のタスクを終えてから停止します");
        } else {
            tracing::info!(target: LOG, "停止要求: 停止します");
            self.wake_consume.notify_one();
        }
    }

    /// すぐに止める (処理中のタスクも中断する)
    pub fn stop(&self) {
        self.running.store(false, Ordering::SeqCst);
        self.hard_stop.notify_one();
    }
}

enum SwimOrMsg {
    Swim(SwimError),
    Msg(String),
}

impl From<String> for SwimOrMsg {
    fn from(s: String) -> Self {
        SwimOrMsg::Msg(s)
    }
}

impl std::fmt::Display for SwimOrMsg {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SwimOrMsg::Swim(e) => write!(f, "{e}"),
            SwimOrMsg::Msg(m) => write!(f, "{m}"),
        }
    }
}

fn py_truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

fn truncate(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// dict の値を (名前, 文字列) の並びに (params・headers)
fn str_pairs(v: Option<&Value>) -> Vec<(String, String)> {
    match v {
        Some(Value::Object(m)) => m
            .iter()
            .map(|(k, v)| (k.clone(), v.as_str().map_or_else(|| v.to_string(), str::to_string)))
            .collect(),
        _ => Vec::new(),
    }
}

/// "OS 種別 + 版 + アーキ" (ダッシュボードの表示用。例 "Linux 6.1.0-rpi7 arm64")。アーキの書き方は OS 間でそろえる
pub fn platform_string() -> String {
    let (os, release, machine) = uname();
    let machine = match machine.as_str() {
        "AMD64" | "x64" => "x86_64".to_string(),
        "aarch64" | "ARM64" => "arm64".to_string(),
        m => m.to_string(),
    };
    format!("{os} {release} {machine}").trim().to_string()
}

#[cfg(unix)]
fn uname() -> (String, String, String) {
    // SAFETY: utsname は uname が埋める。失敗したら空のまま
    unsafe {
        let mut u: libc::utsname = std::mem::zeroed();
        if libc::uname(&mut u) != 0 {
            return (std::env::consts::OS.into(), String::new(), std::env::consts::ARCH.into());
        }
        let s = |a: &[libc::c_char]| std::ffi::CStr::from_ptr(a.as_ptr()).to_string_lossy().into_owned();
        (s(&u.sysname), s(&u.release), s(&u.machine))
    }
}

#[cfg(not(unix))]
fn uname() -> (String, String, String) {
    // Windows の版 (10/11) の判定は M2c (GUI) で入れる
    ("Windows".into(), String::new(), std::env::consts::ARCH.into())
}
