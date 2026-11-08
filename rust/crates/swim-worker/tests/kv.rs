//! Redis の継ぎ目 (Kv)。本物の Redis が要るものは #[ignore] (SWIM_TEST_REDIS_* を渡したときだけ)
use std::path::PathBuf;

use swim_worker::kv::fake::FakeKv;
use swim_worker::kv::{Kv, KvError, KvErrorKind};

#[test]
fn embedded_ca_matches_python() {
    let py = std::fs::read_to_string(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../swim_worker/certs.py")).unwrap();
    let pem = swim_worker::certs::REDIS_CA_PEM;
    assert!(pem.starts_with("-----BEGIN CERTIFICATE-----"));
    assert!(py.contains(pem.trim_end()), "certs.py の CA_CERT_PEM と同じであること");
}

#[tokio::test]
async fn fake_kv_basic_ops() {
    let kv = FakeKv::default();
    assert_eq!(kv.get("a").await.unwrap(), None);
    assert!(kv.set_nx_ex("hb", "tok", 60).await.unwrap());
    assert!(!kv.set_nx_ex("hb", "other", 60).await.unwrap(), "NX");
    assert_eq!(kv.get("hb").await.unwrap().as_deref(), Some("tok"));
    kv.setex("r", 3600, b"bytes").await.unwrap();
    assert_eq!(kv.get_bytes("r"), Some(b"bytes".to_vec()));
    assert_eq!(kv.ttl_of("r"), Some(3600));
    kv.push("tasks:w", "t1");
    assert_eq!(kv.blpop("tasks:w", 1.0).await.unwrap().as_deref(), Some("t1"));
    assert_eq!(kv.blpop("tasks:w", 0.01).await.unwrap(), None);
    kv.sadd("s", "m").await.unwrap();
    assert!(kv.sismember("s", "m").await.unwrap());
    assert_eq!(
        kv.sismember_many(&[("s".into(), "m".into()), ("s".into(), "x".into())]).await.unwrap(),
        vec![Some(true), Some(false)]
    );
    assert_eq!(kv.smembers("s").await.unwrap(), vec!["m".to_string()]);
    kv.hset("h", "f", "v").await.unwrap();
    assert_eq!(kv.hget("h", "f"), Some("v".into()));
    assert_eq!(kv.del("s").await.unwrap(), 1);
}

#[tokio::test]
async fn fake_kv_injects_errors() {
    let kv = FakeKv::default();
    kv.fail_next("get", KvError::new(KvErrorKind::Connection, "down"));
    assert_eq!(kv.get("a").await.unwrap_err().kind, KvErrorKind::Connection);
    assert!(kv.get("a").await.is_ok(), "1 回だけ");
}

#[test]
fn kv_error_kinds_from_redis_errors() {
    use redis::{ErrorKind, RedisError, ServerErrorKind};
    let k = |e: RedisError| KvError::from(e).kind;
    assert_eq!(k(RedisError::from(std::io::Error::other("x"))), KvErrorKind::Connection);
    assert_eq!(k(RedisError::from((ErrorKind::AuthenticationFailed, "auth"))), KvErrorKind::Auth);
    assert_eq!(k(RedisError::from((ErrorKind::Server(ServerErrorKind::NoPerm), "NOPERM"))), KvErrorKind::NoPerm);
    assert_eq!(k(RedisError::from((ErrorKind::Server(ServerErrorKind::ResponseError), "ERR"))), KvErrorKind::Other);
}

#[tokio::test]
#[ignore = "本物の Redis (TLS・ACL) が要る。SWIM_TEST_REDIS_HOST などを渡す"]
async fn real_redis_tls_acl() {
    let env = |k: &str| std::env::var(k).unwrap();
    let name = env("SWIM_TEST_WORKER");
    let cfg = swim_worker::kv::RedisConfig {
        host: env("SWIM_TEST_REDIS_HOST"),
        port: env("SWIM_TEST_REDIS_PORT").parse().unwrap(),
        username: std::env::var("SWIM_TEST_REDIS_USER").ok(),
        password: std::env::var("SWIM_TEST_REDIS_PASSWORD").ok(),
        ca_pem: std::fs::read(env("SWIM_TEST_REDIS_CA")).unwrap(),
        client_name: name.clone(),
        socket_timeout: std::time::Duration::from_secs(30),
    };
    let kv = swim_worker::kv::connect(cfg).unwrap();
    kv.del(&format!("heartbeat:{name}")).await.unwrap();
    assert!(kv.set_nx_ex(&format!("heartbeat:{name}"), "t", 5).await.unwrap());
    assert_eq!(kv.blpop(&format!("tasks:{name}"), 1.0).await.unwrap(), None);
    assert_eq!(kv.sismember_many(&[("workers:approved".into(), name.clone())]).await.unwrap().len(), 1);
}
