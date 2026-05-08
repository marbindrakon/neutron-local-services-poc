//! Catalog HMAC envelope, validation, and inotify-driven reload.
//!
//! On-disk format. The catalog is a single text file at
//! `/var/lib/neutron-local-services/_proxy/catalog.json`, written by
//! the agent via `tmp + rename` so the worker's `inotify` sees an
//! atomic `IN_MOVED_TO`.
//!
//! The first line is the lowercase hex HMAC-SHA256 of everything
//! after the line break. The remainder is the JSON payload.
//!
//! ```text
//! <64 hex chars HMAC-SHA256>\n<json payload bytes>
//! ```
//!
//! This split puts the HMAC outside the payload (no canonical-JSON
//! gymnastics for agent or worker) and keeps the file plain enough
//! to inspect with `head -1` / `tail -n +2`.
//!
//! The schema itself lives in `nls-proxy-wire::catalog` so the priv
//! helper can deserialize the same bytes for its own BindListener
//! authorization decisions.

use std::collections::HashSet;
use std::net::IpAddr;
use std::path::Path;

use anyhow::{anyhow, bail, Context, Result};
use hmac::{Hmac, Mac};
use sha2::Sha256;

pub use nls_proxy_wire::catalog::{
    Backend, Catalog, Entry, HcCommon, HealthCheck, LbAlgo,
};

type HmacSha256 = Hmac<Sha256>;

/// Spawn an inotify-driven watcher on the catalog directory.
/// On every `IN_MOVED_TO` (atomic rename land) or modify event,
/// reparse + reverify and publish the latest `Catalog` over a
/// `watch` channel.
///
/// On parse / HMAC error, the watcher logs + bumps the metrics
/// counter but keeps the previous good `Catalog` published —
/// "last-good state" semantics.
pub fn spawn_watcher(
    path: std::path::PathBuf,
    key: Vec<u8>,
    metrics: std::sync::Arc<crate::metrics::WorkerMetrics>,
) -> tokio::sync::watch::Receiver<std::sync::Arc<Catalog>> {
    use notify::{Event, EventKind, RecursiveMode, Watcher};
    use std::sync::Arc;

    let initial = match load_and_verify(&path, &key) {
        Ok(c) => {
            metrics.note_load_ok(c.generation);
            Arc::new(c)
        }
        Err(e) => {
            tracing::warn!(error = %e, "initial catalog load failed; starting empty");
            Arc::new(Catalog {
                version: 1,
                generation: 0,
                entries: Vec::new(),
            })
        }
    };
    let (tx, rx) = tokio::sync::watch::channel(initial);

    let dir = path
        .parent()
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| std::path::PathBuf::from("."));
    let file_name = path
        .file_name()
        .map(|f| f.to_owned())
        .unwrap_or_default();

    std::thread::Builder::new()
        .name("nls-catalog-watcher".into())
        .spawn(move || {
            let (notify_tx, notify_rx) = std::sync::mpsc::channel::<notify::Result<Event>>();
            let mut watcher = match notify::recommended_watcher(move |res| {
                let _ = notify_tx.send(res);
            }) {
                Ok(w) => w,
                Err(e) => {
                    tracing::error!(error = %e, "create notify watcher");
                    return;
                }
            };
            if let Err(e) = watcher.watch(&dir, RecursiveMode::NonRecursive) {
                tracing::error!(error = %e, dir = %dir.display(), "watch directory");
                return;
            }
            tracing::info!(dir = %dir.display(), "catalog watcher running");

            for ev in notify_rx {
                let ev = match ev {
                    Ok(e) => e,
                    Err(e) => {
                        tracing::warn!(error = %e, "notify event error");
                        continue;
                    }
                };
                let touches_us = ev
                    .paths
                    .iter()
                    .any(|p| p.file_name() == Some(file_name.as_os_str()));
                if !touches_us {
                    continue;
                }
                if !matches!(
                    ev.kind,
                    EventKind::Create(_) | EventKind::Modify(_) | EventKind::Any
                ) {
                    continue;
                }
                match load_and_verify(&path, &key) {
                    Ok(cat) => {
                        let cur = tx.borrow().clone();
                        if cat.generation < cur.generation {
                            tracing::warn!(
                                got = cat.generation,
                                cur = cur.generation,
                                "catalog generation regressed; ignoring"
                            );
                            metrics.note_load_err();
                            continue;
                        }
                        metrics.note_load_ok(cat.generation);
                        if tx.send(Arc::new(cat)).is_err() {
                            return;
                        }
                    }
                    Err(e) => {
                        tracing::warn!(error = %e, "catalog reload failed; keeping last good");
                        metrics.note_load_err();
                    }
                }
            }
        })
        .expect("spawn catalog watcher");

    rx
}

/// Parse the on-disk file into a verified, validated `Catalog`.
pub fn load_and_verify(path: &Path, key: &[u8]) -> Result<Catalog> {
    let bytes = std::fs::read(path).with_context(|| format!("read {}", path.display()))?;
    let (hmac_hex, payload) = split_hmac_payload(&bytes)?;
    verify_hmac(key, hmac_hex, payload).context("HMAC verification")?;
    let catalog: Catalog = serde_json::from_slice(payload).context("parse catalog payload")?;
    validate(&catalog).context("catalog validation")?;
    Ok(catalog)
}

fn split_hmac_payload(bytes: &[u8]) -> Result<(&str, &[u8])> {
    let nl = bytes
        .iter()
        .position(|&b| b == b'\n')
        .ok_or_else(|| anyhow!("missing newline after HMAC line"))?;
    let hmac_line = std::str::from_utf8(&bytes[..nl]).context("HMAC line not UTF-8")?;
    let payload = &bytes[nl + 1..];
    Ok((hmac_line, payload))
}

fn verify_hmac(key: &[u8], hmac_hex: &str, payload: &[u8]) -> Result<()> {
    if hmac_hex.len() != 64 {
        bail!("HMAC line is {} chars, expected 64", hmac_hex.len());
    }
    let expected = hex::decode(hmac_hex).context("HMAC line not hex")?;
    let mut mac = HmacSha256::new_from_slice(key).context("init hmac")?;
    mac.update(payload);
    mac.verify_slice(&expected).map_err(|_| anyhow!("HMAC mismatch"))
}

fn validate(catalog: &Catalog) -> Result<()> {
    if catalog.version != 1 {
        bail!("unsupported catalog version: {}", catalog.version);
    }
    let mut listener_keys: HashSet<(String, IpAddr, u16, nls_proxy_wire::Proto)> = HashSet::new();
    for (i, entry) in catalog.entries.iter().enumerate() {
        validate_uuid(&entry.net_id).with_context(|| format!("entries[{i}].net_id"))?;
        validate_uuid(&entry.service_id)
            .with_context(|| format!("entries[{i}].service_id"))?;
        validate_unicast_routable(entry.vip)
            .with_context(|| format!("entries[{i}].vip {}", entry.vip))?;
        if entry.port == 0 {
            bail!("entries[{i}].port is 0");
        }
        if entry.backends.is_empty() {
            bail!("entries[{i}].backends is empty");
        }
        for (j, b) in entry.backends.iter().enumerate() {
            validate_backend_addr(b.addr)
                .with_context(|| format!("entries[{i}].backends[{j}].addr {}", b.addr))?;
            if b.port == 0 {
                bail!("entries[{i}].backends[{j}].port is 0");
            }
        }
        let key = (entry.net_id.clone(), entry.vip, entry.port, entry.proto);
        if !listener_keys.insert(key) {
            bail!(
                "duplicate listener (net_id={}, vip={}, port={}, proto={:?})",
                entry.net_id,
                entry.vip,
                entry.port,
                entry.proto
            );
        }
        validate_health_check(&entry.health_check)
            .with_context(|| format!("entries[{i}].health_check"))?;
    }
    Ok(())
}

fn validate_health_check(hc: &HealthCheck) -> Result<()> {
    match hc {
        HealthCheck::HttpGet { path, .. } | HealthCheck::HttpsGet { path, .. } => {
            validate_http_origin_form_path(path)
        }
        _ => Ok(()),
    }
}

/// Reject HTTP request-target paths that aren't origin-form or that
/// contain CR/LF/NUL — anything that could break out of the request
/// line we interpolate into. RFC 7230 origin-form: must start with
/// `/`, may carry a `?query`. We allow only printable ASCII (0x21..=0x7E).
fn validate_http_origin_form_path(path: &str) -> Result<()> {
    if !path.starts_with('/') {
        bail!("HTTP HC path must be origin-form (start with '/'); got {path:?}");
    }
    for (i, b) in path.bytes().enumerate() {
        if !(0x21..=0x7E).contains(&b) {
            bail!("HTTP HC path has disallowed byte 0x{b:02x} at offset {i}");
        }
    }
    Ok(())
}

fn validate_uuid(s: &str) -> Result<()> {
    // Loose UUID v4-ish check: 36 chars, dashes at expected slots,
    // remainder hex. We don't pull in the `uuid` crate just for this.
    let bytes = s.as_bytes();
    if bytes.len() != 36 {
        bail!("UUID wrong length: {}", s);
    }
    for (i, b) in bytes.iter().enumerate() {
        match i {
            8 | 13 | 18 | 23 => {
                if *b != b'-' {
                    bail!("UUID malformed: {}", s);
                }
            }
            _ => {
                if !b.is_ascii_hexdigit() {
                    bail!("UUID has non-hex char: {}", s);
                }
            }
        }
    }
    Ok(())
}

fn validate_unicast_routable(addr: IpAddr) -> Result<()> {
    if addr.is_unspecified() {
        bail!("address is unspecified (0.0.0.0 / ::)");
    }
    if addr.is_loopback() {
        bail!("address is loopback");
    }
    if addr.is_multicast() {
        bail!("address is multicast");
    }
    match addr {
        IpAddr::V4(v4) => {
            if v4.is_broadcast() {
                bail!("address is broadcast");
            }
        }
        IpAddr::V6(v6) => {
            // Reject link-local: we don't want listener
            // VIPs in fe80::/10 since scope-id semantics don't fit
            // our model.
            let segs = v6.segments();
            if (segs[0] & 0xffc0) == 0xfe80 {
                bail!("address is IPv6 link-local");
            }
        }
    }
    Ok(())
}

/// Backend addresses are looser than VIPs: loopback is a perfectly
/// valid backend (operator's local DNS/NTP/KMS resolver listening
/// on 127.x.y.z reachable from host root netns). We still reject
/// 0.0.0.0/::, multicast, broadcast, and IPv6 link-local because
/// those don't form a valid `connect()` target either way.
fn validate_backend_addr(addr: IpAddr) -> Result<()> {
    if addr.is_unspecified() {
        bail!("address is unspecified (0.0.0.0 / ::)");
    }
    if addr.is_multicast() {
        bail!("address is multicast");
    }
    match addr {
        IpAddr::V4(v4) => {
            if v4.is_broadcast() {
                bail!("address is broadcast");
            }
        }
        IpAddr::V6(v6) => {
            let segs = v6.segments();
            if (segs[0] & 0xffc0) == 0xfe80 {
                bail!("address is IPv6 link-local");
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key() -> Vec<u8> {
        b"test-hmac-key-do-not-use-in-prod".to_vec()
    }

    fn sign_payload(key: &[u8], payload: &[u8]) -> String {
        let mut mac = HmacSha256::new_from_slice(key).unwrap();
        mac.update(payload);
        hex::encode(mac.finalize().into_bytes())
    }

    fn write_catalog(dir: &std::path::Path, payload: &[u8], key: &[u8]) -> std::path::PathBuf {
        let p = dir.join("catalog.json");
        let hmac = sign_payload(key, payload);
        let mut bytes = Vec::new();
        bytes.extend_from_slice(hmac.as_bytes());
        bytes.push(b'\n');
        bytes.extend_from_slice(payload);
        std::fs::write(&p, bytes).unwrap();
        p
    }

    fn good_payload() -> Vec<u8> {
        let json = serde_json::json!({
            "version": 1,
            "generation": 1,
            "entries": [
                {
                    "net_id": "00112233-4455-6677-8899-aabbccddeeff",
                    "service_id": "11223344-5566-7788-99aa-bbccddeeff00",
                    "nonce": "n",
                    "nonce_path": "/tmp/n",
                    "vip": "169.254.169.10",
                    "port": 53,
                    "proto": "udp",
                    "backends": [{"addr": "192.0.2.10", "port": 53, "weight": 1}],
                    "health_check": {"type": "udp_dns_query", "query": "example.com"}
                }
            ]
        });
        serde_json::to_vec(&json).unwrap()
    }

    #[test]
    fn load_and_verify_happy_path() {
        let dir = tempfile::tempdir().unwrap();
        let p = write_catalog(dir.path(), &good_payload(), &key());
        let cat = load_and_verify(&p, &key()).unwrap();
        assert_eq!(cat.entries.len(), 1);
        assert_eq!(cat.entries[0].port, 53);
    }

    #[test]
    fn rejects_tampered_payload() {
        let dir = tempfile::tempdir().unwrap();
        let p = write_catalog(dir.path(), &good_payload(), &key());
        let mut bytes = std::fs::read(&p).unwrap();
        // Flip a byte in the payload (after the HMAC line).
        let nl = bytes.iter().position(|b| *b == b'\n').unwrap();
        bytes[nl + 5] ^= 0x20;
        std::fs::write(&p, &bytes).unwrap();
        let err = load_and_verify(&p, &key()).unwrap_err();
        assert!(format!("{err:#}").contains("HMAC"), "{err:#}");
    }

    #[test]
    fn rejects_bad_hmac_line() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("catalog.json");
        std::fs::write(&p, b"not-hex\n{}").unwrap();
        let err = load_and_verify(&p, &key()).unwrap_err();
        assert!(
            format!("{err:#}").contains("HMAC line is"),
            "{err:#}"
        );
    }

    #[test]
    fn rejects_loopback_vip() {
        let json = serde_json::json!({
            "version": 1,
            "generation": 1,
            "entries": [
                {
                    "net_id": "00112233-4455-6677-8899-aabbccddeeff",
                    "service_id": "11223344-5566-7788-99aa-bbccddeeff00",
                    "nonce": "n",
                    "nonce_path": "/tmp/n",
                    "vip": "127.0.0.1",
                    "port": 53,
                    "proto": "udp",
                    "backends": [{"addr": "192.0.2.10", "port": 53}],
                    "health_check": {"type": "tcp_connect"}
                }
            ]
        });
        let payload = serde_json::to_vec(&json).unwrap();
        let dir = tempfile::tempdir().unwrap();
        let p = write_catalog(dir.path(), &payload, &key());
        let err = load_and_verify(&p, &key()).unwrap_err();
        assert!(format!("{err:#}").contains("loopback"), "{err:#}");
    }

    #[test]
    fn rejects_duplicate_listener() {
        let json = serde_json::json!({
            "version": 1,
            "generation": 1,
            "entries": [
                {
                    "net_id": "00112233-4455-6677-8899-aabbccddeeff",
                    "service_id": "11223344-5566-7788-99aa-bbccddeeff00",
                    "nonce": "n", "nonce_path": "/tmp/n",
                    "vip": "169.254.169.10", "port": 53, "proto": "udp",
                    "backends": [{"addr": "192.0.2.10", "port": 53}],
                    "health_check": {"type": "tcp_connect"}
                },
                {
                    "net_id": "00112233-4455-6677-8899-aabbccddeeff",
                    "service_id": "22334455-6677-8899-aabb-ccddeeff0011",
                    "nonce": "n", "nonce_path": "/tmp/n",
                    "vip": "169.254.169.10", "port": 53, "proto": "udp",
                    "backends": [{"addr": "192.0.2.11", "port": 53}],
                    "health_check": {"type": "tcp_connect"}
                }
            ]
        });
        let payload = serde_json::to_vec(&json).unwrap();
        let dir = tempfile::tempdir().unwrap();
        let p = write_catalog(dir.path(), &payload, &key());
        let err = load_and_verify(&p, &key()).unwrap_err();
        assert!(format!("{err:#}").contains("duplicate listener"), "{err:#}");
    }
}
