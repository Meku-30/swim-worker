#![windows_subsystem = "windows"]
//! 試作: Slint (ソフトウェア描画) + tray-icon で今の GUI と同じ部品を並べる (何もしない)
use std::cell::RefCell;
use std::rc::Rc;

slint::include_modules!();

const LOG_MAX_LINES: usize = 1000;
const LOG_KEEP_LINES: usize = 800;

fn main() {
    // ソフトウェア描画を選ぶ (GPU の描画系を読み込まない)
    std::env::set_var("SLINT_BACKEND", "winit-software");
    let ui = MainWindow::new().unwrap();

    let rgba = vec![128u8; 32 * 32 * 4];
    let icon = tray_icon::Icon::from_rgba(rgba, 32, 32).unwrap();
    let menu = tray_icon::menu::Menu::new();
    menu.append(&tray_icon::menu::MenuItem::new("表示", true, None)).unwrap();
    menu.append(&tray_icon::menu::MenuItem::new("終了", true, None)).unwrap();
    let _tray = tray_icon::TrayIconBuilder::new()
        .with_tooltip("SWIM Worker")
        .with_icon(icon)
        .with_menu(Box::new(menu))
        .build()
        .unwrap();

    let lines: Rc<RefCell<Vec<String>>> = Rc::new(RefCell::new(Vec::new()));
    let weak = ui.as_weak();
    let timer = slint::Timer::default();
    let mut n = 0u64;
    timer.start(slint::TimerMode::Repeated, std::time::Duration::from_secs(1), move || {
        n += 1;
        let mut l = lines.borrow_mut();
        l.push(format!("12:00:00 タスク完了: collect_notams ({n})"));
        if l.len() > LOG_MAX_LINES {
            let drop = l.len() - LOG_KEEP_LINES;
            l.drain(..drop);
        }
        if let Some(ui) = weak.upgrade() {
            ui.set_log_text(l.join("\n").into());
        }
    });
    ui.run().unwrap();
}
