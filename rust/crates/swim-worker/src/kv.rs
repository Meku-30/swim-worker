//! Redis の継ぎ目 (Kv) と本物の接続 (Python の swim_worker/redis_client.py)。
//! 接続の情報 (ホスト・パスワード) はエラーやログに出さない。
//!
//! - TLS (rustls)。CA は REDIS_CA_CERT のファイル、無ければ埋め込み (certs.rs)。ホスト名を検証する
//! - RESP2・AUTH・`CLIENT SETNAME <worker 名>` だけを送る (HELLO・CLIENT SETINFO は Worker の ACL にない)
//! - 接続は 2 本: BLPOP 専用と、それ以外。BLPOP は最大 20 秒サーバー側で止まるので、heartbeat などを
//!   その後ろに並ばせない (redis-py は接続のプールで別の接続を使う)
//! - 接続の失敗・時間切れ・認証の失敗のあとは、次のコマンドでつなぎ直す (名前も付け直す)
use std::time::Duration;

use async_trait::async_trait;
use redis::aio::MultiplexedConnection;
use redis::{
    AsyncConnectionConfig, Cmd, ConnectionAddr, ErrorKind, FromRedisValue, IntoConnectionInfo, RedisConnectionInfo,
    RedisError, ServerErrorKind, TlsCertificates,
};
use tokio::sync::Mutex;

pub mod fake;

/// Python の consumer が待ち方を分けている種類 (consumer.py:583-593,635-652)
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KvErrorKind {
    /// 切断・つながらない (5 秒待つ)
    Connection,
    /// 時間切れ (接続の失敗と同じ扱い)
    Timeout,
    /// 認証の失敗 (60 秒待つ)
    Auth,
    /// ACL の権限不足 (60 秒待つ)
    NoPerm,
    Other,
}

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{message}")]
pub struct KvError {
    pub kind: KvErrorKind,
    pub message: String,
}

impl KvError {
    pub fn new(kind: KvErrorKind, message: impl Into<String>) -> Self {
        Self { kind, message: message.into() }
    }
    /// 接続し直すべき失敗か
    pub fn is_connection(&self) -> bool {
        matches!(self.kind, KvErrorKind::Connection | KvErrorKind::Timeout)
    }
}

impl From<RedisError> for KvError {
    fn from(e: RedisError) -> Self {
        let kind = if e.kind() == ErrorKind::AuthenticationFailed {
            KvErrorKind::Auth
        } else if e.kind() == ErrorKind::Server(ServerErrorKind::NoPerm) {
            KvErrorKind::NoPerm
        } else if e.is_timeout() {
            KvErrorKind::Timeout
        } else if e.is_io_error() || e.is_connection_dropped() || e.is_connection_refusal() {
            KvErrorKind::Connection
        } else {
            KvErrorKind::Other
        };
        KvError::new(kind, e.to_string())
    }
}

#[async_trait]
pub trait Kv: Send + Sync {
    async fn ping(&self) -> Result<(), KvError>;
    /// BLPOP の値 (キーは落とす)。時間内に来なければ None
    async fn blpop(&self, key: &str, timeout_secs: f64) -> Result<Option<String>, KvError>;
    async fn get(&self, key: &str) -> Result<Option<String>, KvError>;
    /// SET key value NX EX secs。置けたら true
    async fn set_nx_ex(&self, key: &str, value: &str, secs: u64) -> Result<bool, KvError>;
    async fn setex(&self, key: &str, secs: u64, value: &[u8]) -> Result<(), KvError>;
    async fn del(&self, key: &str) -> Result<i64, KvError>;
    async fn sismember(&self, set: &str, member: &str) -> Result<bool, KvError>;
    /// SISMEMBER をパイプライン (トランザクションなし) で並べる。個々の失敗は None
    async fn sismember_many(&self, pairs: &[(String, String)]) -> Result<Vec<Option<bool>>, KvError>;
    async fn sadd(&self, set: &str, member: &str) -> Result<(), KvError>;
    async fn smembers(&self, set: &str) -> Result<Vec<String>, KvError>;
    async fn hset(&self, hash: &str, field: &str, value: &str) -> Result<(), KvError>;
}

/// Debug は付けない (パスワードを持つ)
pub struct RedisConfig {
    pub host: String,
    pub port: u16,
    /// ACL のユーザー名。None なら default
    pub username: Option<String>,
    pub password: Option<String>,
    pub ca_pem: Vec<u8>,
    pub client_name: String,
    pub socket_timeout: Duration,
}

impl RedisConfig {
    pub fn from_settings(s: &crate::config::Settings) -> Result<RedisConfig, KvError> {
        let ca_pem = if s.redis_ca_cert.is_empty() {
            crate::certs::REDIS_CA_PEM.as_bytes().to_vec()
        } else {
            std::fs::read(&s.redis_ca_cert)
                .map_err(|e| KvError::new(KvErrorKind::Other, format!("CA 証明書を読めない ({}): {e}", s.redis_ca_cert)))?
        };
        Ok(RedisConfig {
            host: s.redis_host.clone(),
            port: s.redis_port,
            username: (!s.redis_username.is_empty()).then(|| s.redis_username.clone()),
            password: Some(s.redis_password.clone()),
            ca_pem,
            client_name: s.worker_name.clone(),
            socket_timeout: Duration::from_secs_f64(s.redis_socket_timeout),
        })
    }
}

pub struct RedisKv {
    client: redis::Client,
    conn_cfg: AsyncConnectionConfig,
    client_name: String,
    blocking: Mutex<Option<MultiplexedConnection>>,
    general: Mutex<Option<MultiplexedConnection>>,
}

/// ここではつながない (最初のコマンドでつなぐ。redis-py と同じ)。失敗するのは設定が不正なときだけ
pub fn connect(cfg: RedisConfig) -> Result<RedisKv, KvError> {
    let mut rci = RedisConnectionInfo::default().set_skip_set_lib_name();
    if let Some(u) = &cfg.username {
        rci = rci.set_username(u);
    }
    if let Some(p) = &cfg.password {
        rci = rci.set_password(p);
    }
    let addr = ConnectionAddr::TcpTls { host: cfg.host.clone(), port: cfg.port, insecure: false, tls_params: None };
    let info = addr.into_connection_info()?.set_redis_settings(rci);
    let client = redis::Client::build_with_tls(info, TlsCertificates { client_tls: None, root_cert: Some(cfg.ca_pem) })?;
    let conn_cfg = AsyncConnectionConfig::new()
        .set_connection_timeout(Some(cfg.socket_timeout))
        .set_response_timeout(Some(cfg.socket_timeout));
    Ok(RedisKv { client, conn_cfg, client_name: cfg.client_name, blocking: Mutex::new(None), general: Mutex::new(None) })
}

impl RedisKv {
    async fn open(&self) -> Result<MultiplexedConnection, KvError> {
        let mut c = self.client.get_multiplexed_async_connection_with_config(&self.conn_cfg).await?;
        // Coordinator は CLIENT LIST の名前で Worker の接続元を見る。つなぎ直すたびに付ける
        redis::cmd("CLIENT").arg("SETNAME").arg(&self.client_name).query_async::<()>(&mut c).await?;
        Ok(c)
    }

    async fn run<T: FromRedisValue>(&self, slot: &Mutex<Option<MultiplexedConnection>>, cmd: &Cmd) -> Result<T, KvError> {
        let mut guard = slot.lock().await;
        if guard.is_none() {
            *guard = Some(self.open().await?);
        }
        let mut c = guard.as_ref().expect("接続").clone();
        drop(guard);
        let r = cmd.query_async::<T>(&mut c).await.map_err(KvError::from);
        if let Err(e) = &r {
            if e.is_connection() || e.kind == KvErrorKind::Auth {
                // 次のコマンドでつなぎ直す (時間切れの接続は応答がずれるおそれがあるので捨てる)
                *slot.lock().await = None;
            }
        }
        r
    }

    async fn general<T: FromRedisValue>(&self, cmd: &Cmd) -> Result<T, KvError> {
        self.run(&self.general, cmd).await
    }
}

#[async_trait]
impl Kv for RedisKv {
    async fn ping(&self) -> Result<(), KvError> {
        self.general::<String>(&redis::cmd("PING")).await.map(|_| ())
    }
    async fn blpop(&self, key: &str, timeout_secs: f64) -> Result<Option<String>, KvError> {
        let got: Option<(String, String)> = self.run(&self.blocking, redis::cmd("BLPOP").arg(key).arg(timeout_secs)).await?;
        Ok(got.map(|(_, v)| v))
    }
    async fn get(&self, key: &str) -> Result<Option<String>, KvError> {
        self.general(redis::cmd("GET").arg(key)).await
    }
    async fn set_nx_ex(&self, key: &str, value: &str, secs: u64) -> Result<bool, KvError> {
        let r: Option<String> = self.general(redis::cmd("SET").arg(key).arg(value).arg("NX").arg("EX").arg(secs)).await?;
        Ok(r.is_some())
    }
    async fn setex(&self, key: &str, secs: u64, value: &[u8]) -> Result<(), KvError> {
        self.general(redis::cmd("SETEX").arg(key).arg(secs).arg(value)).await
    }
    async fn del(&self, key: &str) -> Result<i64, KvError> {
        self.general(redis::cmd("DEL").arg(key)).await
    }
    async fn sismember(&self, set: &str, member: &str) -> Result<bool, KvError> {
        self.general(redis::cmd("SISMEMBER").arg(set).arg(member)).await
    }
    async fn sismember_many(&self, pairs: &[(String, String)]) -> Result<Vec<Option<bool>>, KvError> {
        if pairs.is_empty() {
            return Ok(Vec::new());
        }
        let mut p = redis::pipe();
        for (s, m) in pairs {
            p.cmd("SISMEMBER").arg(s).arg(m);
        }
        let mut guard = self.general.lock().await;
        if guard.is_none() {
            *guard = Some(self.open().await?);
        }
        let mut c = guard.as_ref().expect("接続").clone();
        drop(guard);
        let r: Result<Vec<redis::Value>, KvError> = p.query_async(&mut c).await.map_err(KvError::from);
        match r {
            Ok(vals) => Ok(vals
                .into_iter()
                .map(|v| match v {
                    redis::Value::Int(n) => Some(n == 1),
                    redis::Value::Boolean(b) => Some(b),
                    _ => None,
                })
                .collect()),
            Err(e) => {
                if e.is_connection() || e.kind == KvErrorKind::Auth {
                    *self.general.lock().await = None;
                }
                Err(e)
            }
        }
    }
    async fn sadd(&self, set: &str, member: &str) -> Result<(), KvError> {
        self.general::<i64>(redis::cmd("SADD").arg(set).arg(member)).await.map(|_| ())
    }
    async fn smembers(&self, set: &str) -> Result<Vec<String>, KvError> {
        self.general(redis::cmd("SMEMBERS").arg(set)).await
    }
    async fn hset(&self, hash: &str, field: &str, value: &str) -> Result<(), KvError> {
        self.general::<i64>(redis::cmd("HSET").arg(hash).arg(field).arg(value)).await.map(|_| ())
    }
}
