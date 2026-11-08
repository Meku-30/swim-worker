//! ログの形を Python の logging と同じにする: `2026-11-08 12:34:56,789 [INFO] swim_worker.consumer: 本文`
//! (runbook・journalctl の grep が頼る)。RUST_LOG があればその水準、なければ INFO
use std::fmt;

use tracing::{Event, Subscriber};
use tracing_subscriber::fmt::format::{FormatEvent, FormatFields, Writer};
use tracing_subscriber::fmt::FmtContext;
use tracing_subscriber::registry::LookupSpan;
use tracing_subscriber::EnvFilter;

pub struct PyFormat;

impl<S, N> FormatEvent<S, N> for PyFormat
where
    S: Subscriber + for<'a> LookupSpan<'a>,
    N: for<'a> FormatFields<'a> + 'static,
{
    fn format_event(&self, ctx: &FmtContext<'_, S, N>, mut w: Writer<'_>, event: &Event<'_>) -> fmt::Result {
        let now = chrono::Local::now();
        let meta = event.metadata();
        let level = match *meta.level() {
            tracing::Level::ERROR => "ERROR",
            tracing::Level::WARN => "WARNING",
            tracing::Level::INFO => "INFO",
            _ => "DEBUG",
        };
        write!(w, "{},{:03} [{level}] {}: ", now.format("%Y-%m-%d %H:%M:%S"), now.timestamp_subsec_millis(), meta.target())?;
        ctx.field_format().format_fields(w.by_ref(), event)?;
        writeln!(w)
    }
}

pub fn init() {
    let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    let _ = tracing_subscriber::fmt().with_env_filter(filter).event_format(PyFormat).with_writer(std::io::stderr).try_init();
}
