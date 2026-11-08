//! 応答の圧縮 4 種 (gzip・deflate・br・zstd) を展開できることと、accept-encoding の値
use std::io::Write;

use swim_http::{build_client, send, OrderedHeaders};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

const BODY: &str = r#"{"statusCode":0,"datas":{"x":"日本語"}}"#;

fn compress(enc: &str) -> Vec<u8> {
    match enc {
        "gzip" => {
            let mut e = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
            e.write_all(BODY.as_bytes()).unwrap();
            e.finish().unwrap()
        }
        "deflate" => {
            let mut e = flate2::write::ZlibEncoder::new(Vec::new(), flate2::Compression::default());
            e.write_all(BODY.as_bytes()).unwrap();
            e.finish().unwrap()
        }
        "br" => {
            let mut out = Vec::new();
            brotli::BrotliCompress(&mut BODY.as_bytes(), &mut out, &Default::default()).unwrap();
            out
        }
        "zstd" => zstd::encode_all(BODY.as_bytes(), 3).unwrap(),
        _ => unreachable!(),
    }
}

/// 1 回だけ応答する平文の HTTP/1.1 サーバー。受け取った要求の頭を返す
async fn serve_once(enc: &'static str) -> (String, tokio::task::JoinHandle<String>) {
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    let h = tokio::spawn(async move {
        let (mut s, _) = l.accept().await.unwrap();
        let mut buf = vec![0u8; 8192];
        let n = s.read(&mut buf).await.unwrap();
        let req = String::from_utf8_lossy(&buf[..n]).to_string();
        let body = compress(enc);
        let head = format!(
            "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-encoding: {enc}\r\ncontent-length: {}\r\nconnection: close\r\n\r\n",
            body.len()
        );
        s.write_all(head.as_bytes()).await.unwrap();
        s.write_all(&body).await.unwrap();
        req
    });
    (format!("http://{addr}/x"), h)
}

#[tokio::test(flavor = "current_thread")]
async fn decodes_all_four_encodings() {
    let (client, _jar) = build_client().unwrap();
    for enc in ["gzip", "deflate", "br", "zstd"] {
        let (url, h) = serve_once(enc).await;
        // send はエミュレーションの既定のヘッダーを足さないので、accept-encoding も呼び出し側が渡す
        let headers = OrderedHeaders(vec![
            ("accept".into(), "application/json, text/plain, */*".into()),
            ("accept-encoding".into(), "gzip, deflate, br, zstd".into()),
        ]);
        let resp = send(&client, "GET", &url, &headers, None).await.unwrap();
        let v: serde_json::Value = resp.json().await.unwrap();
        assert_eq!(v, serde_json::from_str::<serde_json::Value>(BODY).unwrap(), "{enc}");
        let req = h.await.unwrap().to_ascii_lowercase();
        assert!(req.contains("accept-encoding: gzip, deflate, br, zstd\r\n"), "{enc}: {req}");
    }
}

#[test]
fn python_json_matches_curl_cffi() {
    // Python: json.dumps({"ctrlInfo": {}, "dummy": 1, "s": "日本\"x"}, separators=(",", ":"))
    let v: serde_json::Value = serde_json::from_str(r#"{"ctrlInfo":{},"dummy":1,"s":"日本\"x"}"#).unwrap();
    assert_eq!(swim_http::python_json(&v), r#"{"ctrlInfo":{},"dummy":1,"s":"\u65e5\u672c\"x"}"#);
}
