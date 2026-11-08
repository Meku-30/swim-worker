//! Windows の GUI (今の Python + tkinter の画面と同じ構成。Windows 標準の部品 native-windows-gui)。
//! GUI のスレッドは 1 本。Worker は別のスレッド (runner)、更新のダウンロードも別のスレッドで動き、
//! 知らせは `UiMsg` のキューに積む。GUI は 100ms ごとのタイマーでまとめて取り出す (Python の after と同じ)。
//! モーダルのダイアログの間もタイマーは動くので、状態の借用はダイアログを出す前に必ず手放す
use std::cell::{Cell, RefCell};
use std::process::ExitCode;
use std::rc::Rc;
use std::sync::mpsc::{channel, Receiver, Sender};
use std::sync::Arc;
use std::time::{Duration, Instant};

use native_windows_gui as nwg;

use crate::autostart;
use crate::config::Settings;
use crate::consumer::{ConsumerConfig, ConsumerEvent, SwimApi};
use crate::gui_log::{GuiLogLayer, RotatingFile, LOG_BACKUPS, LOG_MAX_BYTES};
use crate::gui_model::{
    duplicate_message, rollback_message, status_for_runner, status_for_stop, task_state_text, update_prompt_kind,
    update_prompt_message, validate_fields, LogBuffer, LogEdit, PromptKind,
};
use crate::gui_store::secrets::WinCred;
use crate::gui_store::{apply_rollback_marker, Fields, GuiPrefs, RollbackInfo, SettingsStore, Snooze, SNOOZE_HOURS};
use crate::kv::{self, Kv, RedisConfig};
use crate::paths::Paths;
use crate::runner::{Control, Runner, RunnerEvent, StopReason};
use crate::single_instance::{acquire_at, AcquireError};
use crate::swim::transport::WreqFactory;
use crate::swim::SwimClient;
use crate::updater::{self, HelperPaths, Progress, WreqFetcher};

const LOG: &str = "swim_worker.gui";
const VERSION: &str = env!("CARGO_PKG_VERSION");
const TICK_MS: u64 = 100;
const QUIT_GRACEFUL: Duration = Duration::from_secs(30);
const QUIT_HARD: Duration = Duration::from_secs(10);
const UPDATE_GRACEFUL: Duration = Duration::from_secs(60);
const UPDATE_HARD: Duration = Duration::from_secs(15);

static TRAY_GREEN: &[u8] = include_bytes!("../../assets/tray_green.ico");
static TRAY_GRAY: &[u8] = include_bytes!("../../assets/tray_gray.ico");
static TRAY_RED: &[u8] = include_bytes!("../../assets/tray_red.ico");

/// 他のスレッドから GUI への知らせ
enum UiMsg {
    Log(String),
    Runner(RunnerEvent),
    UpdateAvailable(String),
    UpdatePhase(String),
    UpdateBytes { done: u64, total: Option<u64> },
    UpdateDownloaded(String),
    UpdateFailed(String),
}

/// 止まるのを待っている理由
#[derive(Clone, Copy, PartialEq)]
enum Waiting {
    None,
    Quit { since: Instant, hard_sent: bool },
    Update { since: Instant, hard_sent: bool },
}

struct Ui {
    window: nwg::Window,
    status_title: nwg::Label,
    status: nwg::Label,
    toggle: nwg::Button,
    labels: Vec<nwg::Label>,
    inputs: Vec<nwg::TextInput>,
    start: nwg::Button,
    stop: nwg::Button,
    save: nwg::Button,
    update: nwg::Button,
    auto_connect: nwg::CheckBox,
    autostart: nwg::CheckBox,
    auto_update: nwg::CheckBox,
    log_title: nwg::Label,
    log: nwg::TextBox,
    tray: nwg::TrayNotification,
    tray_menu: nwg::Menu,
    tray_show: nwg::MenuItem,
    tray_quit: nwg::MenuItem,
    timer: nwg::AnimationTimer,
    icons: [nwg::Icon; 3],
    // 更新の進み具合の窓
    prog_win: nwg::Window,
    prog_status: nwg::Label,
    prog_bar: nwg::ProgressBar,
    prog_detail: nwg::Label,
    // 自動適用のカウントダウンの窓
    cd_win: nwg::Window,
    cd_title: nwg::Label,
    _cd_current: nwg::Label,
    cd_msg: nwg::Label,
    cd_now: nwg::Button,
    cd_skip: nwg::Button,
    _fonts: Vec<nwg::Font>,
}

const FIELD_ORDER: [(&str, &str, bool); 6] = [
    ("Redis ホスト:", "redis_host", false),
    ("Redis ユーザー名:", "redis_username", false),
    ("Redis パスワード:", "redis_password", true),
    ("SWIM ID:", "swim_username", false),
    ("SWIM パスワード:", "swim_password", true),
    ("Worker 名:", "worker_name", false),
];

struct App {
    ui: Ui,
    paths: Paths,
    tx: Sender<UiMsg>,
    rx: Receiver<UiMsg>,
    cred: WinCred,
    prefs: RefCell<GuiPrefs>,
    snooze: Snooze,
    logbuf: RefCell<LogBuffer>,
    collapsed: Cell<bool>,
    running: Cell<bool>,
    runner: RefCell<Option<Runner>>,
    control: RefCell<Option<Arc<Control>>>,
    runner_alive: Cell<bool>,
    worker_name: RefCell<String>,
    waiting: Cell<Waiting>,
    pending_update: RefCell<Option<String>>,
    prompted: RefCell<Option<String>>,
    updating: Cell<bool>,
    downloaded: RefCell<Option<String>>,
    countdown: Cell<Option<(u32, Instant)>>,
    countdown_version: RefCell<String>,
    started: Instant,
    marker_written: Cell<bool>,
    rollback_checked: Cell<bool>,
    rollback: RefCell<Option<RollbackInfo>>,
    auto_connect_done: Cell<bool>,
    missing_secrets: Cell<bool>,
    quitting: Cell<bool>,
}

fn label(parent: &nwg::Window, text: &str, font: Option<&nwg::Font>) -> nwg::Label {
    let mut l = nwg::Label::default();
    nwg::Label::builder().text(text).font(font).parent(parent).build(&mut l).expect("label");
    l
}

fn button(parent: &nwg::Window, text: &str) -> nwg::Button {
    let mut b = nwg::Button::default();
    nwg::Button::builder().text(text).parent(parent).build(&mut b).expect("button");
    b
}

fn check(parent: &nwg::Window, text: &str, on: bool) -> nwg::CheckBox {
    let mut c = nwg::CheckBox::default();
    let st = if on { nwg::CheckBoxState::Checked } else { nwg::CheckBoxState::Unchecked };
    nwg::CheckBox::builder().text(text).check_state(st).parent(parent).build(&mut c).expect("checkbox");
    c
}

fn icon(bin: &[u8]) -> nwg::Icon {
    let mut i = nwg::Icon::default();
    nwg::Icon::builder().source_bin(Some(bin)).size(Some((16, 16))).build(&mut i).expect("icon");
    i
}

fn sub_window(parent: &nwg::Window, title: &str, size: (i32, i32)) -> nwg::Window {
    let mut w = nwg::Window::default();
    nwg::Window::builder()
        .title(title)
        .size(size)
        .center(true)
        .flags(nwg::WindowFlags::WINDOW)
        .parent(Some(parent.handle))
        .build(&mut w)
        .expect("window");
    w
}

fn build_ui(prefs: &GuiPrefs, autostart_on: bool) -> Ui {
    let mut fonts = Vec::new();
    let mut base = nwg::Font::default();
    let _ = nwg::Font::builder().family("Yu Gothic UI").size(17).build(&mut base);
    nwg::Font::set_global_default(Some(base));
    let mut bold = nwg::Font::default();
    let _ = nwg::Font::builder().family("Yu Gothic UI").size(20).weight(700).build(&mut bold);
    let mut mono = nwg::Font::default();
    let _ = nwg::Font::builder().family("Consolas").size(15).build(&mut mono);

    let icons = [icon(TRAY_GRAY), icon(TRAY_GREEN), icon(TRAY_RED)];
    let mut window = nwg::Window::default();
    nwg::Window::builder()
        .size((520, 640))
        .center(true)
        .title("SWIM Worker")
        .icon(Some(&icons[1]))
        .flags(nwg::WindowFlags::MAIN_WINDOW | nwg::WindowFlags::VISIBLE)
        .build(&mut window)
        .expect("window");

    let status_title = label(&window, "状態", None);
    let status = label(&window, "停止中", Some(&bold));
    let toggle = button(&window, "");
    let mut labels = Vec::new();
    let mut inputs = Vec::new();
    for (text, _, secret) in FIELD_ORDER {
        labels.push(label(&window, text, None));
        let mut t = nwg::TextInput::default();
        nwg::TextInput::builder().password(if secret { Some('*') } else { None }).parent(&window).build(&mut t).expect("input");
        inputs.push(t);
    }
    let start = button(&window, "▶ 起動");
    let stop = button(&window, "■ 停止");
    stop.set_enabled(false);
    let save = button(&window, "設定保存");
    let update = button(&window, "⬆ アップデート");
    update.set_visible(false);
    let auto_connect = check(&window, "起動時に自動接続", false);
    let autostart = check(&window, "Windows起動時に自動起動", autostart_on);
    let auto_update = check(&window, "アップデートを自動適用 (確認ダイアログなし、5秒カウントダウンのみ)", prefs.auto_update());
    let log_title = label(&window, "ログ", None);
    let mut log = nwg::TextBox::default();
    nwg::TextBox::builder()
        .readonly(true)
        .font(Some(&mono))
        .flags(nwg::TextBoxFlags::VISIBLE | nwg::TextBoxFlags::VSCROLL | nwg::TextBoxFlags::AUTOVSCROLL)
        .parent(&window)
        .build(&mut log)
        .expect("log");

    let mut tray = nwg::TrayNotification::default();
    nwg::TrayNotification::builder().parent(&window).icon(Some(&icons[0])).tip(Some("SWIM Worker")).build(&mut tray).expect("tray");
    let mut tray_menu = nwg::Menu::default();
    nwg::Menu::builder().popup(true).parent(&window).build(&mut tray_menu).expect("menu");
    let mut tray_show = nwg::MenuItem::default();
    nwg::MenuItem::builder().text("表示").parent(&tray_menu).build(&mut tray_show).expect("menu");
    let mut tray_quit = nwg::MenuItem::default();
    nwg::MenuItem::builder().text("終了").parent(&tray_menu).build(&mut tray_quit).expect("menu");
    let mut timer = nwg::AnimationTimer::default();
    nwg::AnimationTimer::builder()
        .parent(&window)
        .interval(Duration::from_millis(TICK_MS))
        .active(true)
        .build(&mut timer)
        .expect("timer");

    let prog_win = sub_window(&window, "アップデート中", (420, 150));
    let prog_status = label(&prog_win, "準備中...", Some(&bold));
    let mut prog_bar = nwg::ProgressBar::default();
    nwg::ProgressBar::builder().range(0..100).parent(&prog_win).build(&mut prog_bar).expect("progress");
    let prog_detail = label(&prog_win, "", None);
    prog_status.set_position(20, 15);
    prog_status.set_size(380, 26);
    prog_bar.set_position(20, 50);
    prog_bar.set_size(380, 20);
    prog_detail.set_position(20, 80);
    prog_detail.set_size(380, 24);

    let cd_win = sub_window(&window, "自動アップデート", (420, 170));
    let cd_title = label(&cd_win, "", Some(&bold));
    let _cd_current = label(&cd_win, &format!("現在: v{VERSION}"), None);
    let cd_msg = label(&cd_win, "", None);
    let cd_now = button(&cd_win, "今すぐ");
    let cd_skip = button(&cd_win, &format!("スキップ ({SNOOZE_HOURS}h)"));
    for (c, y) in [(&cd_title, 12), (&_cd_current, 42), (&cd_msg, 70)] {
        c.set_position(20, y);
        c.set_size(380, 24);
    }
    cd_now.set_position(100, 105);
    cd_now.set_size(100, 30);
    cd_skip.set_position(215, 105);
    cd_skip.set_size(120, 30);

    fonts.push(bold);
    fonts.push(mono);
    Ui {
        window,
        status_title,
        status,
        toggle,
        labels,
        inputs,
        start,
        stop,
        save,
        update,
        auto_connect,
        autostart,
        auto_update,
        log_title,
        log,
        tray,
        tray_menu,
        tray_show,
        tray_quit,
        timer,
        icons,
        prog_win,
        prog_status,
        prog_bar,
        prog_detail,
        cd_win,
        cd_title,
        _cd_current,
        cd_msg,
        cd_now,
        cd_skip,
        _fonts: fonts,
    }
}

fn hwnd(h: &nwg::ControlHandle) -> windows_sys::Win32::Foundation::HWND {
    h.hwnd().map_or(std::ptr::null_mut(), |p| p as _)
}

impl App {
    fn checked(c: &nwg::CheckBox) -> bool {
        c.check_state() == nwg::CheckBoxState::Checked
    }

    fn fields(&self) -> Fields {
        let v: Vec<String> = self.ui.inputs.iter().map(|i| i.text()).collect();
        Fields {
            redis_host: v[0].clone(),
            redis_username: v[1].clone(),
            redis_password: v[2].clone(),
            swim_username: v[3].clone(),
            swim_password: v[4].clone(),
            worker_name: v[5].clone(),
        }
    }

    fn store(&self) -> SettingsStore<'_> {
        SettingsStore::new(self.paths.base().join(".env"), Some(&self.cred))
    }

    fn prefs_path(&self) -> std::path::PathBuf {
        self.paths.data_dir().join("gui_settings.json")
    }

    fn save_prefs(&self) {
        let p = self.prefs.borrow().clone();
        if let Err(e) = p.save(&self.prefs_path()) {
            tracing::warn!(target: LOG, "GUI 設定保存失敗: {e}");
        }
    }

    // ---- 並べ方 (Python の pack の順: 状態 → 設定 → ボタン → チェック → ログ) ----
    fn layout(&self) {
        let ui = &self.ui;
        let (w, h) = ui.window.size();
        let (w, h) = (w as i32, h as i32);
        let inner = (w - 20).max(100);
        ui.status_title.set_position(10, 6);
        ui.status_title.set_size(inner as u32, 20);
        ui.status.set_position(10, 26);
        ui.status.set_size(inner as u32, 28);
        ui.toggle.set_position(10, 60);
        ui.toggle.set_size(150, 28);
        let collapsed = self.collapsed.get();
        ui.toggle.set_text(if collapsed { "▶ 設定を表示" } else { "▼ 設定を隠す" });
        let mut y = 94;
        for (i, (l, t)) in ui.labels.iter().zip(&ui.inputs).enumerate() {
            l.set_visible(!collapsed);
            t.set_visible(!collapsed);
            let ry = y + i as i32 * 30;
            l.set_position(20, ry + 3);
            l.set_size(135, 24);
            t.set_position(160, ry);
            t.set_size((w - 180).max(60) as u32, 26);
        }
        if !collapsed {
            y += 6 * 30 + 4;
        }
        ui.start.set_position(10, y);
        ui.start.set_size(85, 30);
        ui.stop.set_position(100, y);
        ui.stop.set_size(85, 30);
        ui.save.set_position(200, y);
        ui.save.set_size(90, 30);
        ui.update.set_position((w - 175).max(300), y);
        ui.update.set_size(165, 30);
        y += 38;
        ui.auto_connect.set_position(10, y);
        ui.auto_connect.set_size(200, 24);
        ui.autostart.set_position((w - 230).max(215), y);
        ui.autostart.set_size(220, 24);
        y += 26;
        ui.auto_update.set_position(10, y);
        ui.auto_update.set_size(inner as u32, 24);
        y += 30;
        ui.log_title.set_position(10, y);
        ui.log_title.set_size(100, 20);
        y += 20;
        ui.log.set_position(10, y);
        ui.log.set_size(inner as u32, (h - y - 10).max(60) as u32);
    }

    // ---- ログ欄 ----
    fn append_logs(&self, lines: Vec<String>) {
        use windows_sys::Win32::UI::WindowsAndMessaging::SendMessageW;
        const EM_SETSEL: u32 = 0x00B1;
        const EM_REPLACESEL: u32 = 0x00C2;
        const EM_SCROLLCARET: u32 = 0x00B7;
        const WM_GETTEXTLENGTH: u32 = 0x000E;
        let h = hwnd(&self.ui.log.handle);
        let mut buf = self.logbuf.borrow_mut();
        let mut add = String::new();
        let mut reset: Option<String> = None;
        for l in lines {
            match buf.push(l) {
                LogEdit::Append(s) => add.push_str(&s),
                LogEdit::Reset(all) => {
                    reset = Some(all);
                    add.clear();
                }
            }
        }
        drop(buf);
        if let Some(all) = reset {
            self.ui.log.set_text(&all);
        }
        if add.is_empty() || h.is_null() {
            return;
        }
        let w: Vec<u16> = add.encode_utf16().chain(std::iter::once(0)).collect();
        // SAFETY: 自分の作った edit に、0 終わりの文字列を渡す (末尾に足すだけで全文を取り直さない)
        unsafe {
            // 末尾に置いてから足す (EM_SETSEL(-1, -1) は選択を外すだけで、末尾に動かない)
            let len = SendMessageW(h, WM_GETTEXTLENGTH, 0, 0);
            SendMessageW(h, EM_SETSEL, len as usize, len);
            SendMessageW(h, EM_REPLACESEL, 0, w.as_ptr() as isize);
            SendMessageW(h, EM_SCROLLCARET, 0, 0);
        }
    }

    // ---- 状態の表示 ----
    fn set_tray(&self, i: usize) {
        self.ui.tray.set_icon(&self.ui.icons[i]);
    }

    fn set_running_ui(&self, running: bool, status: Option<&str>, tray: Option<usize>) {
        self.running.set(running);
        self.ui.start.set_enabled(!running);
        self.ui.stop.set_enabled(running);
        for i in &self.ui.inputs {
            i.set_enabled(!running);
        }
        if let Some(s) = status {
            self.ui.status.set_text(s);
        }
        self.set_tray(tray.unwrap_or(if running { 1 } else { 0 }));
    }

    // ---- 設定 ----
    fn load_settings(&self) {
        let st = self.store();
        match st.load() {
            Ok((f, auto)) => {
                if let Some(a) = auto {
                    self.ui.auto_connect.set_check_state(if a { nwg::CheckBoxState::Checked } else { nwg::CheckBoxState::Unchecked });
                }
                for (i, (_, key, _)) in FIELD_ORDER.iter().enumerate() {
                    self.ui.inputs[i].set_text(f.get(key));
                }
                self.missing_secrets.set(!st.missing_secrets().is_empty());
            }
            Err(e) => tracing::warn!(target: LOG, "設定の読み込みに失敗: {e}"),
        }
    }

    fn save_settings(&self) -> bool {
        let f = self.fields();
        match self.store().save(&f, Self::checked(&self.ui.auto_connect)) {
            Ok("keyring") => {
                tracing::info!(target: LOG, "設定を保存しました (パスワードは OS の資格情報ストアに保存)");
                true
            }
            Ok(_) => {
                tracing::info!(target: LOG, "設定を保存しました");
                true
            }
            Err(e) => {
                tracing::error!(target: LOG, "設定の保存に失敗: {e}");
                nwg::modal_error_message(&self.ui.window, "エラー", &format!("設定を保存できませんでした:\n{e}"));
                false
            }
        }
    }

    fn on_auto_connect(&self) {
        let v = Self::checked(&self.ui.auto_connect);
        match self.store().set_auto_connect(v) {
            Ok(true) => tracing::info!(target: LOG, "自動接続を{}にしました", if v { "有効" } else { "無効" }),
            Ok(false) => {}
            Err(e) => tracing::warn!(target: LOG, "自動接続の設定を保存できません: {e}"),
        }
    }

    fn exe() -> std::path::PathBuf {
        std::env::current_exe().unwrap_or_default()
    }

    fn on_autostart(&self) {
        let Some(p) = autostart::startup_bat_path() else { return };
        let r = if Self::checked(&self.ui.autostart) {
            autostart::enable(&p, self.paths.base(), &Self::exe())
        } else {
            autostart::disable(&p)
        };
        if let Err(e) = r {
            tracing::warn!(target: LOG, "自動起動の設定に失敗: {e}");
        }
    }

    fn on_auto_update(&self) {
        let v = Self::checked(&self.ui.auto_update);
        self.prefs.borrow_mut().set_auto_update(v);
        self.save_prefs();
        tracing::info!(target: LOG, "アップデート自動適用: {}", if v { "有効" } else { "無効" });
    }

    fn on_toggle(&self) {
        self.collapsed.set(!self.collapsed.get());
        self.layout();
        self.prefs.borrow_mut().set_settings_collapsed(self.collapsed.get());
        self.save_prefs();
    }

    // ---- Worker ----
    fn on_start(&self) {
        if self.quitting.get() {
            return;
        }
        let f = self.fields();
        if let Some(p) = validate_fields(&f) {
            nwg::modal_error_message(&self.ui.window, "エラー", &p);
            return;
        }
        if self.runner_alive.get() {
            nwg::modal_info_message(&self.ui.window, "停止処理中", "前回の停止処理が完了していません。しばらく待ってから再度押してください。");
            return;
        }
        if !self.save_settings() {
            return;
        }
        let pairs: Vec<(String, String)> = vec![
            ("REDIS_HOST".into(), f.redis_host.trim().into()),
            ("REDIS_PORT".into(), "6380".into()),
            ("REDIS_USERNAME".into(), f.redis_username.trim().into()),
            ("REDIS_PASSWORD".into(), f.redis_password.clone()),
            ("SWIM_USERNAME".into(), f.swim_username.trim().into()),
            ("SWIM_PASSWORD".into(), f.swim_password.clone()),
            ("WORKER_NAME".into(), f.worker_name.trim().into()),
        ];
        // 画面の値だけで作る (.env・環境変数を読まない。Python の WorkerRunner と同じ)
        let settings = match Settings::from_sources(&pairs, &Default::default()) {
            Ok(s) => s,
            Err(e) => {
                nwg::modal_error_message(&self.ui.window, "エラー", &e.to_string());
                return;
            }
        };
        let kv: Arc<dyn Kv> = match RedisConfig::from_settings(&settings).and_then(kv::connect) {
            Ok(k) => Arc::new(k),
            Err(e) => {
                nwg::modal_error_message(&self.ui.window, "エラー", &e.to_string());
                return;
            }
        };
        let swim: Arc<dyn SwimApi> = Arc::new(SwimClient::new(
            &settings.swim_username,
            &settings.swim_password,
            self.paths.cookie_file(&settings.cookie_file),
            Box::new(WreqFactory),
        ));
        *self.worker_name.borrow_mut() = settings.worker_name.clone();
        let cfg = ConsumerConfig::from_settings(&settings, self.paths.clone());
        let tx = self.tx.clone();
        self.set_running_ui(true, Some("● 起動中..."), None);
        match Runner::start(kv, swim, cfg, Box::new(move |e| {
            let _ = tx.send(UiMsg::Runner(e));
        })) {
            Ok(r) => {
                *self.control.borrow_mut() = Some(r.control());
                *self.runner.borrow_mut() = Some(r);
                self.runner_alive.set(true);
            }
            Err(e) => {
                self.set_running_ui(false, Some("エラーで停止しました (ログを確認)"), Some(2));
                tracing::error!(target: LOG, "Worker のスレッドを作れません: {e}");
            }
        }
    }

    fn request_stop(&self) {
        self.running.set(false);
        if let Some(c) = self.control.borrow().as_ref() {
            c.request_stop();
        }
        self.set_tray(0);
        self.ui.status.set_text("停止処理中 (処理中のタスクを終えてから止まります)...");
        self.ui.stop.set_enabled(false);
    }

    fn on_runner(&self, e: RunnerEvent) {
        match e {
            RunnerEvent::State(s) => {
                if self.running.get() {
                    self.ui.status.set_text(&status_for_runner(&s));
                }
            }
            RunnerEvent::Consumer(ConsumerEvent::TaskState { state, job_type, total, errors }) => {
                if self.running.get() {
                    self.ui.status.set_text(&task_state_text(state, &job_type, total, errors));
                }
            }
            RunnerEvent::Consumer(ConsumerEvent::UpdateAvailable(v)) => self.on_update_available(v.trim_start_matches('v').to_string()),
            RunnerEvent::Stopped(r) => self.on_stopped(r),
        }
    }

    fn on_stopped(&self, r: StopReason) {
        self.runner_alive.set(false);
        *self.control.borrow_mut() = None;
        if let Some(run) = self.runner.borrow_mut().take() {
            run.join();
        }
        match self.waiting.get() {
            Waiting::Quit { .. } => {
                self.waiting.set(Waiting::None);
                nwg::stop_thread_dispatch();
                return;
            }
            Waiting::Update { .. } => {
                self.waiting.set(Waiting::None);
                self.finish_update();
                return;
            }
            Waiting::None => {}
        }
        if self.quitting.get() {
            return;
        }
        let tray = if matches!(r, StopReason::User) { 0 } else { 2 };
        self.set_running_ui(false, Some(status_for_stop(&r)), Some(tray));
        match r {
            StopReason::User => tracing::info!(target: LOG, "Worker停止"),
            StopReason::Duplicate(_) => {
                let name = self.worker_name.borrow().clone();
                nwg::modal_error_message(&self.ui.window, "SWIM Worker - 重複起動", &duplicate_message(&name));
            }
            StopReason::AuthFailed(_) => self.check_github_update(),
            StopReason::Error(_) => {}
        }
    }

    /// Redis に入れないと Coordinator 経由の更新の知らせが届かないので、GitHub で確かめる
    fn check_github_update(&self) {
        let tx = self.tx.clone();
        let _ = std::thread::Builder::new().name("swim-worker-update-check".into()).spawn(move || {
            let Ok(rt) = tokio::runtime::Builder::new_current_thread().enable_all().build() else { return };
            rt.block_on(async {
                let Ok((client, _)) = swim_http::build_client() else { return };
                if let Some(latest) = updater::fetch_latest_release_version(&client).await {
                    let newer = crate::consumer::version::parse_version(&latest) > crate::consumer::version::parse_version(VERSION);
                    if newer {
                        tracing::warn!(target: "swim_worker.update_check", "Redis に入れないため GitHub で最新版を確認: v{VERSION} → v{latest}");
                        let _ = tx.send(UiMsg::UpdateAvailable(latest));
                    }
                }
            });
        });
    }

    // ---- 終了 ----
    fn force_quit(&self) {
        if self.quitting.get() {
            return;
        }
        self.quitting.set(true);
        if !self.runner_alive.get() {
            nwg::stop_thread_dispatch();
            return;
        }
        self.running.set(false);
        self.ui.status.set_text("終了処理中 (処理中のタスクを終えてから閉じます)...");
        self.ui.start.set_enabled(false);
        self.ui.stop.set_enabled(false);
        if let Some(c) = self.control.borrow().as_ref() {
            c.request_stop();
        }
        self.waiting.set(Waiting::Quit { since: Instant::now(), hard_sent: false });
    }

    fn hide_to_tray(&self) {
        self.ui.window.set_visible(false);
        tracing::info!(target: LOG, "システムトレイに格納しました");
    }

    fn show_window(&self) {
        self.ui.window.set_visible(true);
        self.ui.window.restore();
        self.ui.window.set_focus();
    }

    // ---- 更新 ----
    fn on_update_available(&self, v: String) {
        self.ui.update.set_text(&format!("⬆ アップデート (v{v})"));
        self.ui.update.set_visible(true);
        *self.pending_update.borrow_mut() = Some(v.clone());
        if self.prompted.borrow().as_deref() == Some(v.as_str()) {
            return;
        }
        *self.prompted.borrow_mut() = Some(v.clone());
        let kind = update_prompt_kind(self.snooze.is_snoozed(&v, chrono::Utc::now()), Self::checked(&self.ui.auto_update));
        match kind {
            None => tracing::info!(target: LOG, "アップデート v{v} は snooze 期間中のためプロンプトを抑制"),
            Some(PromptKind::Countdown) => self.start_countdown(&v),
            Some(PromptKind::Prompt) => self.prompt_update(&v),
        }
    }

    fn prompt_update(&self, v: &str) {
        let p = nwg::MessageParams {
            title: "アップデートがあります",
            content: &update_prompt_message(v, VERSION),
            buttons: nwg::MessageButtons::YesNo,
            icons: nwg::MessageIcons::Question,
        };
        if nwg::modal_message(&self.ui.window, &p) == nwg::MessageChoice::Yes {
            let _ = std::fs::remove_file(self.snooze.path());
            self.start_update(v.to_string());
        } else {
            self.snooze.set(v, chrono::Utc::now());
        }
    }

    fn start_countdown(&self, v: &str) {
        *self.countdown_version.borrow_mut() = v.to_string();
        self.ui.cd_title.set_text(&format!("新しいバージョン v{v} が利用可能です"));
        self.ui.cd_msg.set_text("5 秒後に自動でアップデートします...");
        self.countdown.set(Some((5, Instant::now())));
        self.ui.cd_win.set_visible(true);
    }

    fn countdown_tick(&self) {
        let Some((n, since)) = self.countdown.get() else { return };
        if since.elapsed() < Duration::from_secs(1) {
            return;
        }
        let n = n - 1;
        if n == 0 {
            self.countdown_now();
            return;
        }
        self.countdown.set(Some((n, Instant::now())));
        self.ui.cd_msg.set_text(&format!("{n} 秒後に自動でアップデートします..."));
    }

    fn countdown_now(&self) {
        if self.countdown.take().is_none() {
            return;
        }
        self.ui.cd_win.set_visible(false);
        let v = self.countdown_version.borrow().clone();
        let _ = std::fs::remove_file(self.snooze.path());
        self.start_update(v);
    }

    fn countdown_skip(&self) {
        if self.countdown.take().is_none() {
            return;
        }
        self.ui.cd_win.set_visible(false);
        let v = self.countdown_version.borrow().clone();
        self.snooze.set(&v, chrono::Utc::now());
    }

    fn start_update(&self, v: String) {
        if self.updating.get() {
            tracing::debug!(target: LOG, "既にアップデート進行中、重複起動をスキップ");
            return;
        }
        self.updating.set(true);
        self.ui.prog_status.set_text("ダウンロードを準備中...");
        self.ui.prog_detail.set_text("");
        self.ui.prog_bar.set_pos(0);
        self.ui.prog_win.set_visible(true);
        self.ui.window.set_enabled(false);
        tracing::info!(target: LOG, "アップデート v{v} をダウンロード中...");
        let tx = self.tx.clone();
        let dest = updater::new_exe_path(self.paths.base());
        let _ = std::thread::Builder::new().name("swim-worker-update".into()).spawn(move || {
            let r = (|| -> Result<(), String> {
                let rt = tokio::runtime::Builder::new_current_thread().enable_all().build().map_err(|e| e.to_string())?;
                let f = WreqFetcher::new()?;
                let keys = updater::verify::release_pubkeys();
                let t2 = tx.clone();
                let progress = move |p: Progress| {
                    let _ = t2.send(match p {
                        Progress::Phase(s) => UiMsg::UpdatePhase(s.into()),
                        Progress::Bytes { done, total } => UiMsg::UpdateBytes { done, total },
                    });
                };
                let n = rt
                    .block_on(updater::download_and_verify(&f, &updater::download_url(&v), &dest, VERSION, &keys, &progress))
                    .map_err(|e| e.to_string())?;
                tracing::info!(target: LOG, "ダウンロード完了: {} ({:.1} MB)", dest.display(), n as f64 / 1048576.0);
                Ok(())
            })();
            let _ = tx.send(match r {
                Ok(()) => UiMsg::UpdateDownloaded(v),
                Err(e) => UiMsg::UpdateFailed(e),
            });
        });
    }

    fn on_update_downloaded(&self, v: String) {
        self.ui.prog_bar.set_pos(100);
        *self.downloaded.borrow_mut() = Some(v);
        if self.runner_alive.get() {
            self.ui.prog_status.set_text("Worker を停止中 (処理中のタスクを終えています)...");
            self.request_stop();
            self.waiting.set(Waiting::Update { since: Instant::now(), hard_sent: false });
        } else {
            self.finish_update();
        }
    }

    /// 差し替えの .bat に引き継いで終わる
    fn finish_update(&self) {
        let Some(v) = self.downloaded.borrow().clone() else { return };
        self.ui.prog_status.set_text("差し替えスクリプトを起動中...");
        let hp = HelperPaths::for_exe(self.paths.base(), &Self::exe());
        if let Err(e) = updater::launch_helper(&hp, &v) {
            self.update_failed(e.to_string());
            return;
        }
        tracing::info!(target: LOG, "アップデータを起動しました。まもなく再起動します");
        self.ui.prog_status.set_text("再起動中...");
        self.ui.prog_detail.set_text("ヘルパースクリプトに引き継ぎました");
        // 少し後に終わる → exe のロックが外れ、.bat の move が通る
        std::thread::sleep(Duration::from_secs(1));
        self.ui.tray.set_visibility(false);
        std::process::exit(0);
    }

    fn update_failed(&self, e: String) {
        tracing::error!(target: LOG, "アップデート失敗: {e}");
        self.updating.set(false);
        self.ui.prog_win.set_visible(false);
        self.ui.window.set_enabled(true);
        nwg::modal_error_message(&self.ui.window, "アップデート失敗", &format!("アップデートに失敗しました:\n{e}"));
    }

    // ---- 起動直後の後始末 ----
    /// 前回の更新が巻き戻った知らせ (数え上げ・snooze は起動したときに済ませてある)
    fn check_rollback_marker(&self) {
        let Some(info) = self.rollback.borrow_mut().take() else { return };
        let (from, reason, disabled) = (info.from, info.reason, info.disabled);
        nwg::modal_message(
            &self.ui.window,
            &nwg::MessageParams {
                title: "前回のアップデートは失敗しました",
                content: &rollback_message(&from, &reason, disabled),
                buttons: nwg::MessageButtons::Ok,
                icons: nwg::MessageIcons::Warning,
            },
        );
    }

    // ---- 100ms ごと ----
    fn tick(&self) {
        let mut logs = Vec::new();
        let mut msgs = Vec::new();
        while let Ok(m) = self.rx.try_recv() {
            match m {
                UiMsg::Log(l) => logs.push(l),
                other => msgs.push(other),
            }
        }
        if !logs.is_empty() {
            self.append_logs(logs);
        }
        for m in msgs {
            match m {
                UiMsg::Log(_) => {}
                UiMsg::Runner(e) => self.on_runner(e),
                UiMsg::UpdateAvailable(v) => self.on_update_available(v),
                UiMsg::UpdatePhase(s) => self.ui.prog_status.set_text(&s),
                UiMsg::UpdateBytes { done, total } => {
                    let mb = 1048576.0;
                    match total {
                        Some(t) if t > 0 => {
                            self.ui.prog_bar.set_pos((done * 100 / t) as u32);
                            self.ui.prog_detail.set_text(&format!("{:.1} / {:.1} MB", done as f64 / mb, t as f64 / mb));
                        }
                        _ => self.ui.prog_detail.set_text(&format!("{:.1} MB", done as f64 / mb)),
                    }
                }
                UiMsg::UpdateDownloaded(v) => self.on_update_downloaded(v),
                UiMsg::UpdateFailed(e) => self.update_failed(e),
            }
        }
        self.countdown_tick();
        self.wait_tick();
        let up = self.started.elapsed();
        // 更新の .bat への起動の知らせ: 2 秒生きていたら印を書く (Redis につながなくても)
        if !self.marker_written.get() && up >= Duration::from_secs(2) {
            self.marker_written.set(true);
            write_startup_marker(&self.paths);
        }
        if !self.auto_connect_done.get() && up >= Duration::from_millis(500) {
            self.auto_connect_done.set(true);
            if Self::checked(&self.ui.auto_connect) {
                if validate_fields(&self.fields()).is_none() {
                    tracing::info!(target: LOG, "自動接続: Workerを起動します");
                    self.on_start();
                } else {
                    tracing::warn!(target: LOG, "自動接続: 設定が未入力・不正のためスキップしました");
                }
            }
        }
        if !self.rollback_checked.get() && up >= Duration::from_secs(3) {
            self.rollback_checked.set(true);
            self.check_rollback_marker();
            if self.missing_secrets.get() {
                nwg::modal_message(
                    &self.ui.window,
                    &nwg::MessageParams {
                        title: "パスワードの入れ直し",
                        content: "保存していたパスワードを読めませんでした。\nRedis と SWIM のパスワードを入れ直して「保存」を押してください。",
                        buttons: nwg::MessageButtons::Ok,
                        icons: nwg::MessageIcons::Warning,
                    },
                );
            }
        }
    }

    /// 止まるのを待つ (処理中のタスクを終える。時間が過ぎたら中断して、さらに待つ)
    fn wait_tick(&self) {
        let (since, hard_sent, graceful, hard, is_quit) = match self.waiting.get() {
            Waiting::None => return,
            Waiting::Quit { since, hard_sent } => (since, hard_sent, QUIT_GRACEFUL, QUIT_HARD, true),
            Waiting::Update { since, hard_sent } => (since, hard_sent, UPDATE_GRACEFUL, UPDATE_HARD, false),
        };
        let el = since.elapsed();
        if !hard_sent && el >= graceful {
            if let Some(c) = self.control.borrow().as_ref() {
                c.stop_now();
            }
            let w = if is_quit { Waiting::Quit { since, hard_sent: true } } else { Waiting::Update { since, hard_sent: true } };
            self.waiting.set(w);
        } else if hard_sent && el >= graceful + hard {
            self.waiting.set(Waiting::None);
            if is_quit {
                tracing::warn!(target: LOG, "Worker が止まらないまま終了します");
                nwg::stop_thread_dispatch();
            } else {
                tracing::warn!(target: LOG, "Worker が止まらないまま更新を続けます");
                self.finish_update();
            }
        }
    }
}

fn write_startup_marker(paths: &Paths) {
    let m = paths.startup_marker();
    if let Err(e) = m.parent().map_or(Ok(()), std::fs::create_dir_all).and_then(|_| std::fs::write(&m, VERSION)) {
        tracing::debug!(target: LOG, "startup marker 作成失敗 (無視): {e}");
    }
}

fn init_logging(paths: &Paths, tx: Sender<UiMsg>) {
    use tracing_subscriber::layer::SubscriberExt;
    let file = RotatingFile::new(paths.base().join("swim-worker.log"), LOG_MAX_BYTES, LOG_BACKUPS);
    let tx = std::sync::Mutex::new(tx);
    let layer = GuiLogLayer::new(file, Box::new(move |l| {
        let _ = tx.lock().map(|t| t.send(UiMsg::Log(l)));
    }));
    let _ = tracing::subscriber::set_global_default(tracing_subscriber::registry().with(layer));
}

/// GUI の入口
pub fn main() -> ExitCode {
    let paths = Paths::current();
    let (tx, rx) = channel();
    init_logging(&paths, tx.clone());
    tracing::info!(target: LOG, "swim-worker 起動開始");
    if let Err(e) = nwg::init() {
        tracing::error!(target: LOG, "画面を初期化できません: {e}");
        return ExitCode::FAILURE;
    }
    // 同じ PC での多重起動はファイルのロックで止める (2 つ目はダイアログを出して終わる)
    let _lock = match acquire_at(&paths.lock()) {
        Ok(l) => l,
        Err(AcquireError::AlreadyRunning(p)) => {
            tracing::warn!(target: LOG, "多重起動検知: 別の swim-worker プロセスが既に起動しています (lock: {})", p.display());
            nwg::error_message(
                "SWIM Worker",
                "SWIM Worker は既に起動しています。\n\nタスクバーまたはシステムトレイ (レーダー型アイコン) を確認してください。",
            );
            return ExitCode::from(2);
        }
        Err(e) => {
            tracing::error!(target: LOG, "{e}");
            nwg::error_message("SWIM Worker", &e.to_string());
            return ExitCode::from(3);
        }
    };
    updater::cleanup_stale_update_files(paths.base(), std::time::SystemTime::now());
    let snooze = Snooze::new(paths.data_dir().join("update_snooze.json"));
    snooze.cleanup_expired(chrono::Utc::now()); // 期限切れ・壊れたものだけ片付ける
    // 前回の更新が巻き戻っていたら、Worker が新版を知るより前に数えて snooze を置く (知らせは 3 秒後)
    let rollback = apply_rollback_marker(&paths.data_dir(), &snooze, chrono::Utc::now());
    let prefs = GuiPrefs::load(&paths.data_dir().join("gui_settings.json"));
    let bat = autostart::startup_bat_path();
    let autostart_on = bat.as_deref().is_some_and(autostart::is_enabled);
    // exe を動かしたら自動起動の .bat を今の場所に書き直す
    if let (Some(p), Ok(exe)) = (bat.as_deref(), std::env::current_exe()) {
        if let Ok(true) = autostart::rewrite_if_stale(p, paths.base(), &exe) {
            tracing::info!(target: LOG, "自動起動ファイルを今の exe に合わせて書き直しました: {}", exe.display());
        }
    }
    let ui = build_ui(&prefs, autostart_on);
    let collapsed = prefs.settings_collapsed();
    let app = Rc::new(App {
        ui,
        paths,
        tx,
        rx,
        cred: WinCred,
        prefs: RefCell::new(prefs),
        snooze,
        logbuf: RefCell::new(LogBuffer::default()),
        collapsed: Cell::new(collapsed),
        running: Cell::new(false),
        runner: RefCell::new(None),
        control: RefCell::new(None),
        runner_alive: Cell::new(false),
        worker_name: RefCell::new(String::new()),
        waiting: Cell::new(Waiting::None),
        pending_update: RefCell::new(None),
        prompted: RefCell::new(None),
        updating: Cell::new(false),
        downloaded: RefCell::new(None),
        countdown: Cell::new(None),
        countdown_version: RefCell::new(String::new()),
        started: Instant::now(),
        marker_written: Cell::new(false),
        rollback_checked: Cell::new(false),
        rollback: RefCell::new(rollback),
        auto_connect_done: Cell::new(false),
        missing_secrets: Cell::new(false),
        quitting: Cell::new(false),
    });
    app.layout();
    app.load_settings();
    tracing::info!(
        target: LOG,
        "GUI 起動 (v{VERSION}) / アップデート自動適用: {}",
        if App::checked(&app.ui.auto_update) { "有効" } else { "無効" }
    );

    let handlers: Vec<nwg::EventHandler> = [app.ui.window.handle, app.ui.prog_win.handle, app.ui.cd_win.handle]
        .iter()
        .map(|h| {
            let a = app.clone();
            nwg::full_bind_event_handler(h, move |evt, data, handle| on_event(&a, evt, &data, handle))
        })
        .collect();
    nwg::dispatch_thread_events();
    for h in handlers {
        nwg::unbind_event_handler(&h);
    }
    if let Some(c) = app.control.borrow().as_ref() {
        c.stop_now();
    }
    app.ui.tray.set_visibility(false);
    ExitCode::SUCCESS
}

fn on_event(app: &App, evt: nwg::Event, data: &nwg::EventData, handle: nwg::ControlHandle) {
    use nwg::Event as E;
    let ui = &app.ui;
    match evt {
        E::OnTimerTick if handle == ui.timer.handle => app.tick(),
        E::OnButtonClick if handle == ui.start.handle => app.on_start(),
        E::OnButtonClick if handle == ui.stop.handle => app.request_stop(),
        E::OnButtonClick if handle == ui.save.handle => {
            app.save_settings();
        }
        E::OnButtonClick if handle == ui.toggle.handle => app.on_toggle(),
        E::OnButtonClick if handle == ui.update.handle => {
            let v = app.pending_update.borrow().clone();
            if let Some(v) = v {
                app.prompt_update(&v);
            }
        }
        E::OnButtonClick if handle == ui.auto_connect.handle => app.on_auto_connect(),
        E::OnButtonClick if handle == ui.autostart.handle => app.on_autostart(),
        E::OnButtonClick if handle == ui.auto_update.handle => app.on_auto_update(),
        E::OnButtonClick if handle == ui.cd_now.handle => app.countdown_now(),
        E::OnButtonClick if handle == ui.cd_skip.handle => app.countdown_skip(),
        E::OnResize | E::OnWindowMaximize if handle == ui.window.handle => app.layout(),
        E::OnMinMaxInfo if handle == ui.window.handle => data.on_min_max().set_min_size(420, 400),
        E::OnWindowMinimize if handle == ui.window.handle => app.hide_to_tray(),
        E::OnWindowClose if handle == ui.window.handle => {
            if let nwg::EventData::OnWindowClose(d) = data {
                d.close(false);
            }
            if app.running.get() {
                app.hide_to_tray();
            } else {
                app.force_quit();
            }
        }
        E::OnWindowClose if handle == ui.prog_win.handle => {
            // 進行中は閉じられない
            if let nwg::EventData::OnWindowClose(d) = data {
                d.close(false);
            }
        }
        E::OnWindowClose if handle == ui.cd_win.handle => {
            if let nwg::EventData::OnWindowClose(d) = data {
                d.close(false);
            }
            app.countdown_skip();
        }
        E::OnMousePress(nwg::MousePressEvent::MousePressLeftUp) if handle == ui.tray.handle => app.show_window(),
        E::OnContextMenu if handle == ui.tray.handle => {
            let (x, y) = nwg::GlobalCursor::position();
            ui.tray_menu.popup(x, y);
        }
        E::OnMenuItemSelected if handle == ui.tray_show.handle => app.show_window(),
        E::OnMenuItemSelected if handle == ui.tray_quit.handle => app.force_quit(),
        _ => {}
    }
}
