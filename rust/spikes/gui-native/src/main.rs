#![windows_subsystem = "windows"]
//! 試作: Windows の標準の部品だけで今の GUI と同じ部品を並べる (何もしない)
use std::cell::RefCell;
use std::rc::Rc;

use native_windows_gui as nwg;

const LOG_MAX_LINES: usize = 1000;
const LOG_KEEP_LINES: usize = 800;

fn main() {
    nwg::init().expect("init");
    let mut font = nwg::Font::default();
    nwg::Font::builder().family("Yu Gothic UI").size(16).build(&mut font).ok();
    nwg::Font::set_global_default(Some(font));

    let mut window = nwg::Window::default();
    nwg::Window::builder().size((520, 640)).position((200, 100)).title("SWIM Worker").build(&mut window).unwrap();

    let labels = ["Redis ホスト:", "Redis ユーザー名:", "Redis パスワード:", "SWIM ID:", "SWIM パスワード:", "Worker 名:"];
    let mut keep_labels = Vec::new();
    let mut keep_inputs = Vec::new();
    for (i, text) in labels.iter().enumerate() {
        let y = 20 + i as i32 * 30;
        let mut l = nwg::Label::default();
        nwg::Label::builder().text(text).position((20, y)).size((140, 24)).parent(&window).build(&mut l).unwrap();
        let mut t = nwg::TextInput::default();
        nwg::TextInput::builder()
            .position((170, y))
            .size((320, 24))
            .password(if text.contains("パスワード") { Some('*') } else { None })
            .parent(&window)
            .build(&mut t)
            .unwrap();
        keep_labels.push(l);
        keep_inputs.push(t);
    }
    let mut buttons = Vec::new();
    for (i, text) in ["▶ 起動", "■ 停止", "設定保存", "⬆ アップデート"].iter().enumerate() {
        let mut b = nwg::Button::default();
        nwg::Button::builder().text(text).position((20 + i as i32 * 120, 210)).size((110, 30)).parent(&window).build(&mut b).unwrap();
        buttons.push(b);
    }
    let mut checks = Vec::new();
    for (i, text) in ["起動時に自動接続", "Windows起動時に自動起動", "アップデートを自動適用 (確認ダイアログなし、5秒カウントダウンのみ)"].iter().enumerate() {
        let mut c = nwg::CheckBox::default();
        nwg::CheckBox::builder().text(text).position((20, 250 + i as i32 * 26)).size((470, 24)).parent(&window).build(&mut c).unwrap();
        checks.push(c);
    }
    let mut log = nwg::TextBox::default();
    nwg::TextBox::builder().position((20, 340)).size((470, 250)).readonly(true).parent(&window).build(&mut log).unwrap();

    let mut icon = nwg::Icon::default();
    nwg::Icon::builder().source_system(Some(nwg::OemIcon::Information)).build(&mut icon).unwrap();
    let mut tray = nwg::TrayNotification::default();
    nwg::TrayNotification::builder().parent(&window).icon(Some(&icon)).tip(Some("SWIM Worker")).build(&mut tray).unwrap();

    let mut timer = nwg::AnimationTimer::default();
    nwg::AnimationTimer::builder().parent(&window).interval(std::time::Duration::from_secs(1)).active(true).build(&mut timer).unwrap();

    let lines: Rc<RefCell<Vec<String>>> = Rc::new(RefCell::new(Vec::new()));
    let n = Rc::new(RefCell::new(0u64));
    let window_h = window.handle;
    let timer_h = timer.handle;
    let log = Rc::new(log);
    let log2 = log.clone();
    let handler = nwg::full_bind_event_handler(&window_h, move |evt, _data, handle| match evt {
        nwg::Event::OnTimerTick if handle == timer_h => {
            *n.borrow_mut() += 1;
            let mut l = lines.borrow_mut();
            let line = format!("12:00:00 タスク完了: collect_notams ({})", n.borrow());
            log2.append(&format!("{line}\r\n"));
            l.push(line);
            if l.len() > LOG_MAX_LINES {
                // 今の GUI と同じく、超えたら古い行を消して 800 行に戻す
                let drop = l.len() - LOG_KEEP_LINES;
                l.drain(..drop);
                log2.set_text(&(l.join("\r\n") + "\r\n"));
            }
        }
        nwg::Event::OnWindowClose if handle == window_h => nwg::stop_thread_dispatch(),
        _ => {}
    });
    nwg::dispatch_thread_events();
    nwg::unbind_event_handler(&handler);
    drop((keep_labels, keep_inputs, buttons, checks, tray, timer, log));
}
