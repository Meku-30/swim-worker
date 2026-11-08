//! GUI から使う Worker の動かし役 (Python の worker_runner.py)。
//! Redis に入れない間は、認証の失敗・重複起動を除いて 1 秒から倍々 (上限 300 秒) で待って入り直す。
//! 終わったら `RunnerEvent::Stopped` を必ず 1 回送る。
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::sync::Notify;

use crate::consumer::{ConsumerConfig, ConsumerError, ConsumerEvent, SwimApi, TaskConsumer};
use crate::kv::{Kv, KvError, KvErrorKind};

const LOG: &str = "swim_worker.worker_runner";
const INITIAL_BACKOFF: u64 = 1;
const MAX_BACKOFF: u64 = 300;

#[derive(Debug, Clone, PartialEq)]
pub enum RunnerState {
    Connecting,
    /// Redis 再接続待ち (attempt 回目、delay_secs 秒後に再試行)
    Waiting { attempt: u32, delay_secs: u64 },
    /// 接続中 (タスク待ち)
    Running,
}

#[derive(Debug, Clone, PartialEq)]
pub enum StopReason {
    User,
    AuthFailed(String),
    Duplicate(String),
    Error(String),
}

#[derive(Debug, Clone, PartialEq)]
pub enum RunnerEvent {
    State(RunnerState),
    Consumer(ConsumerEvent),
    Stopped(StopReason),
}

/// 外 (GUI のスレッド) から止めるための札
#[derive(Default)]
pub struct Control {
    stop: AtomicBool,
    wake: Notify,
    consumer: Mutex<Option<Arc<TaskConsumer>>>,
}

impl Control {
    /// 処理中のタスクを終えてから止める
    pub fn request_stop(&self) {
        self.stop.store(true, Ordering::SeqCst);
        self.wake.notify_one();
        if let Some(c) = self.consumer.lock().unwrap_or_else(|e| e.into_inner()).as_ref() {
            c.request_stop();
        }
    }

    /// すぐに止める
    pub fn stop_now(&self) {
        self.stop.store(true, Ordering::SeqCst);
        self.wake.notify_one();
        if let Some(c) = self.consumer.lock().unwrap_or_else(|e| e.into_inner()).as_ref() {
            c.stop();
        }
    }

    fn stopping(&self) -> bool {
        self.stop.load(Ordering::SeqCst)
    }

    fn set_consumer(&self, c: Option<Arc<TaskConsumer>>) {
        *self.consumer.lock().unwrap_or_else(|e| e.into_inner()) = c;
    }
}

pub type EventSink = Box<dyn Fn(RunnerEvent) + Send + Sync>;

fn is_auth(e: &KvError) -> bool {
    matches!(e.kind, KvErrorKind::Auth | KvErrorKind::NoPerm)
}

/// Worker を 1 回動かす (止まったら終わり)。テストはこれを直接呼ぶ
pub async fn run_loop(kv: Arc<dyn Kv>, swim: Arc<dyn SwimApi>, cfg: ConsumerConfig, ctl: Arc<Control>, emit: EventSink) {
    let reason = run_inner(kv, swim, cfg, &ctl, &emit).await;
    ctl.set_consumer(None);
    emit(RunnerEvent::Stopped(reason));
}

async fn run_inner(kv: Arc<dyn Kv>, swim: Arc<dyn SwimApi>, cfg: ConsumerConfig, ctl: &Control, emit: &EventSink) -> StopReason {
    let mut delay = INITIAL_BACKOFF;
    let mut attempt = 0u32;
    emit(RunnerEvent::State(RunnerState::Connecting));
    while !ctl.stopping() {
        if let Err(e) = kv.ping().await {
            if is_auth(&e) {
                tracing::error!(target: LOG, "エラー: {e}");
                return StopReason::AuthFailed(e.to_string());
            }
            attempt += 1;
            if !backoff(ctl, emit, attempt, &mut delay, &e.to_string()).await {
                break;
            }
            continue;
        }
        tracing::info!(target: LOG, "Redis接続成功");
        emit(RunnerEvent::State(RunnerState::Running));
        delay = INITIAL_BACKOFF;
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let consumer = Arc::new(TaskConsumer::new(kv.clone(), swim.clone(), cfg.clone(), Some(tx)));
        ctl.set_consumer(Some(consumer.clone()));
        if ctl.stopping() {
            break;
        }
        let run = consumer.run();
        tokio::pin!(run);
        let r = loop {
            tokio::select! {
                r = &mut run => break r,
                Some(ev) = rx.recv() => emit(RunnerEvent::Consumer(ev)),
            }
        };
        while let Ok(ev) = rx.try_recv() {
            emit(RunnerEvent::Consumer(ev));
        }
        ctl.set_consumer(None);
        match r {
            Ok(()) => break,
            Err(ConsumerError::Duplicate(m)) => {
                tracing::error!(target: LOG, "重複起動検知: {m}");
                return StopReason::Duplicate(m);
            }
            Err(ConsumerError::Kv(e)) if is_auth(&e) => {
                tracing::error!(target: LOG, "エラー: {e}");
                return StopReason::AuthFailed(e.to_string());
            }
            Err(ConsumerError::Kv(e)) if e.is_connection() => {
                attempt += 1;
                if !backoff(ctl, emit, attempt, &mut delay, &e.to_string()).await {
                    break;
                }
            }
            Err(e) => {
                tracing::error!(target: LOG, "エラー: {e}");
                return StopReason::Error(e.to_string());
            }
        }
    }
    StopReason::User
}

/// 待つ。止める要求が来たら false
async fn backoff(ctl: &Control, emit: &EventSink, attempt: u32, delay: &mut u64, err: &str) -> bool {
    tracing::warn!(target: LOG, "Redis に接続できません ({attempt} 回目)。{} 秒後に再試行: {err}", *delay);
    emit(RunnerEvent::State(RunnerState::Waiting { attempt, delay_secs: *delay }));
    let woke = tokio::select! {
        _ = tokio::time::sleep(Duration::from_secs(*delay)) => false,
        _ = ctl.wake.notified() => true,
    };
    *delay = (*delay * 2).min(MAX_BACKOFF);
    !(woke || ctl.stopping())
}

/// 専用のスレッドで tokio current_thread を回す (GUI のスレッドとは別)
pub struct Runner {
    ctl: Arc<Control>,
    thread: Option<std::thread::JoinHandle<()>>,
}

impl Runner {
    pub fn start(kv: Arc<dyn Kv>, swim: Arc<dyn SwimApi>, cfg: ConsumerConfig, emit: EventSink) -> std::io::Result<Runner> {
        let ctl = Arc::new(Control::default());
        let c2 = ctl.clone();
        let thread = std::thread::Builder::new().name("swim-worker".into()).spawn(move || {
            match tokio::runtime::Builder::new_current_thread().enable_all().build() {
                Ok(rt) => rt.block_on(run_loop(kv, swim, cfg, c2, emit)),
                Err(e) => emit(RunnerEvent::Stopped(StopReason::Error(e.to_string()))),
            }
        })?;
        Ok(Runner { ctl, thread: Some(thread) })
    }

    pub fn control(&self) -> Arc<Control> {
        self.ctl.clone()
    }

    pub fn request_stop(&self) {
        self.ctl.request_stop();
    }

    pub fn join(mut self) {
        if let Some(t) = self.thread.take() {
            let _ = t.join();
        }
    }
}
