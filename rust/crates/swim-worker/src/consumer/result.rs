//! Coordinator に返す結果 (consumer.py _make_result・_encode_result)
use serde_json::{json, Map, Value};

/// datetime.now(timezone.utc).isoformat() と同じ形 (マイクロ秒 6 桁、+00:00)
pub fn now_iso() -> String {
    chrono::Utc::now().format("%Y-%m-%dT%H:%M:%S%.6f+00:00").to_string()
}

#[allow(clippy::too_many_arguments)] // Python の _make_result と同じ引数
pub fn make_result(
    task_id: &str,
    worker_name: &str,
    status: &str,
    data: Option<Value>,
    error: Option<String>,
    format: Option<&str>,
    error_kind: Option<&str>,
    completed_at: String,
) -> Value {
    let mut m = Map::new();
    m.insert("task_id".into(), json!(task_id));
    m.insert("worker_name".into(), json!(worker_name));
    m.insert("status".into(), json!(status));
    m.insert("data".into(), data.unwrap_or(Value::Null));
    m.insert("error".into(), error.map_or(Value::Null, Value::String));
    m.insert("completed_at".into(), json!(completed_at));
    if let Some(f) = format {
        m.insert("format".into(), json!(f));
    }
    if let Some(k) = error_kind {
        m.insert("error_kind".into(), json!(k));
    }
    Value::Object(m)
}

/// 最小の JSON (Python の json.dumps(separators=(",", ":")) と同じ、ensure_ascii) を zstd level 6 で圧縮
pub fn encode(result: &Value) -> Vec<u8> {
    zstd::encode_all(swim_http::python_json(result).as_bytes(), 6).expect("zstd")
}
