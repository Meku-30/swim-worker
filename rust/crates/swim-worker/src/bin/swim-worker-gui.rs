#![cfg_attr(windows, windows_subsystem = "windows")]
//! Windows の GUI 版 (今の Python の GUI と同じ画面)
#[cfg(windows)]
fn main() -> std::process::ExitCode {
    swim_worker::gui::main()
}

#[cfg(not(windows))]
fn main() {
    eprintln!("swim-worker-gui は Windows 専用です (Linux は swim-worker を使います)");
    std::process::exit(1);
}
