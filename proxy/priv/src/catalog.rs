//! Catalog load + HMAC verify, priv-side.
//!
//! priv reads the same agent-signed catalog the worker does, using
//! the same HMAC-SHA256 key, and uses it solely to authorize
//! `BindListener` requests: a request whose `(net_id, vip, port,
//! proto)` tuple isn't in the catalog is refused before any
//! `setns()`/`bind()` happens, and the canonical `nonce` /
//! `nonce_path` come from the catalog entry rather than the worker's
//! request body. A compromised worker therefore cannot:
//!
//! * bind on a (vip, port, proto) the operator never authorized
//!   inside a netns it has the fd for;
//! * point the post-`setns()` nonce read at an attacker-controlled
//!   path to satisfy the recycle check on a recycled-but-renamed
//!   netns.
//!
//! Reload model. The catalog is reloaded synchronously on every
//! `BindListener` call. Binds happen on catalog change, not on the
//! data path, so the per-call `read + HMAC verify` cost is negligible.
//! No inotify watcher, no shared state — the latest on-disk bytes are
//! authoritative each time.

use std::path::Path;

use anyhow::{anyhow, bail, Context, Result};
use hmac::{Hmac, Mac};
use sha2::Sha256;

pub use nls_proxy_wire::catalog::{Catalog, Entry};

type HmacSha256 = Hmac<Sha256>;

/// Read `path`, verify the HMAC line against `key`, decode the JSON
/// payload into a `Catalog`. No additional validation — the worker
/// validates schema constraints (uniqueness, port != 0, etc.) before
/// applying. priv only needs structural decode + per-call
/// (net_id, vip, port, proto) lookup to make its authorization call.
pub fn load_and_verify(path: &Path, key: &[u8]) -> Result<Catalog> {
    let bytes = std::fs::read(path).with_context(|| format!("read {}", path.display()))?;
    let (hmac_hex, payload) = split_hmac_payload(&bytes)?;
    verify_hmac(key, hmac_hex, payload).context("HMAC verification")?;
    let catalog: Catalog =
        serde_json::from_slice(payload).context("parse catalog payload")?;
    if catalog.version != 1 {
        bail!("unsupported catalog version: {}", catalog.version);
    }
    Ok(catalog)
}

/// Locate the catalog entry whose primary listener key matches
/// `(net_id, vip, port, proto)`. Returns `None` if no entry matches —
/// the caller refuses the BindListener.
pub fn lookup_entry<'a>(
    catalog: &'a Catalog,
    net_id: &str,
    vip: std::net::IpAddr,
    port: u16,
    proto: nls_proxy_wire::Proto,
) -> Option<&'a Entry> {
    catalog.entries.iter().find(|e| {
        e.net_id == net_id && e.vip == vip && e.port == port && e.proto == proto
    })
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
    mac.verify_slice(&expected)
        .map_err(|_| anyhow!("HMAC mismatch"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use nls_proxy_wire::Proto;

    fn key() -> Vec<u8> {
        b"test-hmac-key-do-not-use-in-prod".to_vec()
    }

    fn sign_payload(key: &[u8], payload: &[u8]) -> String {
        let mut mac = HmacSha256::new_from_slice(key).unwrap();
        mac.update(payload);
        hex::encode(mac.finalize().into_bytes())
    }

    fn write_catalog(dir: &Path, payload: &[u8], key: &[u8]) -> std::path::PathBuf {
        let p = dir.join("catalog.json");
        let hmac = sign_payload(key, payload);
        let mut bytes = Vec::new();
        bytes.extend_from_slice(hmac.as_bytes());
        bytes.push(b'\n');
        bytes.extend_from_slice(payload);
        std::fs::write(&p, bytes).unwrap();
        p
    }

    fn one_entry_payload(net_id: &str, vip: &str, port: u16) -> Vec<u8> {
        let json = serde_json::json!({
            "version": 1,
            "generation": 1,
            "entries": [{
                "net_id": net_id,
                "service_id": "11223344-5566-7788-99aa-bbccddeeff00",
                "nonce": "the-nonce",
                "nonce_path": "/run/netns-nonces/the-nonce",
                "vip": vip,
                "port": port,
                "proto": "tcp",
                "backends": [{"addr": "192.0.2.10", "port": port, "weight": 1}],
                "health_check": {"type": "tcp_connect"}
            }]
        });
        serde_json::to_vec(&json).unwrap()
    }

    fn tempdir() -> TempDirHandle {
        let path = std::env::temp_dir().join(format!(
            "nls-priv-cat-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0)
        ));
        std::fs::create_dir(&path).unwrap();
        TempDirHandle(path)
    }

    struct TempDirHandle(std::path::PathBuf);
    impl Drop for TempDirHandle {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }
    impl TempDirHandle {
        fn path(&self) -> &Path {
            &self.0
        }
    }

    #[test]
    fn load_and_verify_happy_path() {
        let dir = tempdir();
        let p = write_catalog(
            dir.path(),
            &one_entry_payload(
                "00112233-4455-6677-8899-aabbccddeeff",
                "169.254.42.1",
                8080,
            ),
            &key(),
        );
        let cat = load_and_verify(&p, &key()).unwrap();
        assert_eq!(cat.entries.len(), 1);
        assert_eq!(cat.entries[0].port, 8080);
    }

    #[test]
    fn rejects_bad_hmac() {
        let dir = tempdir();
        let p = write_catalog(
            dir.path(),
            &one_entry_payload(
                "00112233-4455-6677-8899-aabbccddeeff",
                "169.254.42.1",
                8080,
            ),
            &key(),
        );
        let err = load_and_verify(&p, b"different-key").unwrap_err();
        assert!(format!("{err:#}").contains("HMAC"), "{err:#}");
    }

    #[test]
    fn lookup_finds_matching_entry() {
        let dir = tempdir();
        let p = write_catalog(
            dir.path(),
            &one_entry_payload(
                "00112233-4455-6677-8899-aabbccddeeff",
                "169.254.42.1",
                8080,
            ),
            &key(),
        );
        let cat = load_and_verify(&p, &key()).unwrap();
        let hit = lookup_entry(
            &cat,
            "00112233-4455-6677-8899-aabbccddeeff",
            "169.254.42.1".parse().unwrap(),
            8080,
            Proto::Tcp,
        );
        assert!(hit.is_some());
        assert_eq!(hit.unwrap().nonce, "the-nonce");
    }

    #[test]
    fn lookup_rejects_wrong_port() {
        let dir = tempdir();
        let p = write_catalog(
            dir.path(),
            &one_entry_payload(
                "00112233-4455-6677-8899-aabbccddeeff",
                "169.254.42.1",
                8080,
            ),
            &key(),
        );
        let cat = load_and_verify(&p, &key()).unwrap();
        let miss = lookup_entry(
            &cat,
            "00112233-4455-6677-8899-aabbccddeeff",
            "169.254.42.1".parse().unwrap(),
            9090,
            Proto::Tcp,
        );
        assert!(miss.is_none());
    }

    #[test]
    fn lookup_rejects_wrong_net_id() {
        let dir = tempdir();
        let p = write_catalog(
            dir.path(),
            &one_entry_payload(
                "00112233-4455-6677-8899-aabbccddeeff",
                "169.254.42.1",
                8080,
            ),
            &key(),
        );
        let cat = load_and_verify(&p, &key()).unwrap();
        let miss = lookup_entry(
            &cat,
            "ffffffff-ffff-ffff-ffff-ffffffffffff",
            "169.254.42.1".parse().unwrap(),
            8080,
            Proto::Tcp,
        );
        assert!(miss.is_none());
    }
}
