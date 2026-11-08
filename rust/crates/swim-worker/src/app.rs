//! CLI の起動 (Python の swim_worker/__main__.py)。引数は取らない。
//! 終了コード: 0 通常の停止、1 設定の誤り・Redis に 10 回つながらない、2 重複起動、3 ロックファイルを作れない
use std::process::ExitCode;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Arc;
use std::time::Duration;

use crate::config::Settings;
use crate::consumer::{ConsumerConfig, ConsumerError, SwimApi, TaskConsumer};
use crate::dns_check;
use crate::kv::{self, Kv, RedisConfig};
use crate::paths::Paths;
use crate::single_instance::{acquire_at, AcquireError};
use crate::swim::transport::WreqFactory;
use crate::swim::SwimClient;

const LOG: &str = "__main__";

/// Redis に PING が通るまで、1 秒から倍々 (最大 30 秒) で 10 回まで試す。通らなければ false
pub async fn wait_for_redis(kv: &dyn Kv) -> bool {
    let mut delay = 1.0f64;
    for attempt in 1..=10 {
        match kv.ping().await {
            Ok(()) => {
                tracing::info!(target: LOG, "Redis接続成功 ({attempt}回目)");
                return true;
            }
            Err(e) => {
                if cfg!(target_os = "linux") && dns_check::is_name_resolution_error(&e.message) {
                    tracing::error!(target: LOG, "Redis のホスト名を名前解決できません。{}", dns_check::FIX_HINT);
                }
                if attempt == 10 {
                    tracing::error!(target: LOG, "Redis接続失敗 (10回試行、諦めます): {e}");
                    return false;
                }
                tracing::warn!(target: LOG, "Redis接続失敗 ({attempt}/10)、{delay:.1}秒後にリトライ: {e}");
                tokio::time::sleep(Duration::from_secs_f64(delay)).await;
                delay = (delay * 2.0).min(30.0);
            }
        }
    }
    false
}

/// シグナルの 1 回目は処理中のタスクを終えてから止まる、2 回目はすぐ止まる
pub struct SignalCounter {
    count: AtomicU32,
}

impl Default for SignalCounter {
    fn default() -> Self {
        Self { count: AtomicU32::new(0) }
    }
}

impl SignalCounter {
    pub fn on_signal(&self, c: &TaskConsumer) {
        if self.count.fetch_add(1, Ordering::SeqCst) == 0 {
            c.request_stop();
        } else {
            c.stop();
        }
    }
}

async fn next_signal() {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{signal, SignalKind};
        let mut int = signal(SignalKind::interrupt()).expect("SIGINT");
        let mut term = signal(SignalKind::terminate()).expect("SIGTERM");
        tokio::select! {
            _ = int.recv() => {}
            _ = term.recv() => {}
        }
    }
    #[cfg(windows)]
    {
        let mut c = tokio::signal::windows::ctrl_c().expect("Ctrl+C");
        let mut b = tokio::signal::windows::ctrl_break().expect("Ctrl+Break");
        tokio::select! {
            _ = c.recv() => {}
            _ = b.recv() => {}
        }
    }
}

/// consumer を動かし、シグナルで止める。重複起動なら 2
pub async fn run_consumer(consumer: Arc<TaskConsumer>) -> ExitCode {
    let counter = Arc::new(SignalCounter::default());
    let c2 = consumer.clone();
    let k2 = counter.clone();
    let signals = async move {
        loop {
            next_signal().await;
            k2.on_signal(&c2);
        }
    };
    let r = tokio::select! {
        r = consumer.run() => r,
        _ = signals => unreachable!(),
    };
    match r {
        Ok(()) => ExitCode::SUCCESS,
        Err(ConsumerError::Duplicate(e)) => {
            tracing::error!(target: LOG, "重複起動検知: {e}");
            ExitCode::from(2)
        }
        Err(e) => {
            tracing::error!(target: LOG, "Worker の停止: {e}");
            ExitCode::FAILURE
        }
    }
}

/// 設定・Redis・SWIM のクライアントを用意して動かす (ロックは呼び出し側で取る)
pub async fn main_async(paths: Paths) -> ExitCode {
    let settings = match Settings::load(std::path::Path::new(".env")) {
        Ok(s) => s,
        Err(e) => {
            tracing::error!(target: LOG, "設定を読めません: {e}");
            return ExitCode::FAILURE;
        }
    };
    dns_check::check_at_startup();
    let kv: Arc<dyn Kv> = match RedisConfig::from_settings(&settings).and_then(kv::connect) {
        Ok(k) => Arc::new(k),
        Err(e) => {
            tracing::error!(target: LOG, "Redis の設定を使えません: {e}");
            return ExitCode::FAILURE;
        }
    };
    if !wait_for_redis(kv.as_ref()).await {
        return ExitCode::FAILURE;
    }
    let swim = Arc::new(SwimClient::new(
        &settings.swim_username,
        &settings.swim_password,
        paths.cookie_file(&settings.cookie_file),
        Box::new(WreqFactory),
    ));
    let consumer = Arc::new(TaskConsumer::new(
        kv,
        swim.clone() as Arc<dyn SwimApi>,
        ConsumerConfig::from_settings(&settings, paths),
        None,
    ));
    let code = run_consumer(consumer).await;
    swim.close().await;
    code
}

/// プロセスの入口: 多重起動のロック (Redis につなぐ前に確かめる) → main_async
pub fn main_entry() -> ExitCode {
    crate::logging::init();
    let paths = Paths::current();
    let _lock = match acquire_at(&paths.lock()) {
        Ok(l) => l,
        Err(e @ AcquireError::AlreadyRunning(_)) => {
            tracing::error!(target: LOG, "{e}");
            return ExitCode::from(2);
        }
        Err(AcquireError::Io { path, source }) => {
            tracing::error!(
                target: LOG,
                "ロックファイルを作成できません: {} ({source})。書き込み可能なディレクトリで実行してください",
                path.display()
            );
            return ExitCode::from(3);
        }
    };
    let rt = tokio::runtime::Builder::new_current_thread().enable_all().build().expect("tokio");
    rt.block_on(main_async(paths))
}
