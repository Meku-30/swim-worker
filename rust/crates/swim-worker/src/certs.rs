//! Redis の TLS の CA (Python の swim_worker/certs.py と同じ)
pub const REDIS_CA_PEM: &str = include_str!("certs/redis_ca.pem");
