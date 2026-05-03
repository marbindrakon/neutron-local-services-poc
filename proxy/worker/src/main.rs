#![forbid(unsafe_code)]

//! `nls-proxy` — userspace L4 proxy worker.
//!
//! Process model. Runs with zero capabilities and full systemd
//! sandboxing. Every worker thread stays in the host root netns for
//! its entire lifetime — `setns()` is never called from this
//! process. Tenant-side listener fds are bound by `nls-proxy-priv`
//! and handed off via SCM_RIGHTS; the kernel routes I/O on those fds
//! based on each socket's bind-time netns, not the calling thread's
//! netns. The watchdog asserts this invariant at runtime.
//!
//! Thread topology:
//!
//! - main: starts everything; then runs the agent control socket
//! - hc: one tokio current-thread runtime; one async task per
//!   backend probe; publishes status via watch
//! - admin: axum on the unix admin socket
//! - watchdog: per-tid `/proc/self/task/<tid>/ns/net` inode check
//! - per-tenant: one tokio current-thread runtime per tenant netns,
//!   owns listener fds and session tables for that tenant

use std::path::PathBuf;
use std::sync::Arc;

use anyhow::{Context, Result};

mod admin;
mod catalog;
mod dispatcher;
mod hc;
mod lb;
mod metrics;
mod priv_client;
mod quota;
mod tcp;
mod tenant;
mod udp;
mod watchdog;

const DEFAULT_CATALOG_PATH: &str = "/var/lib/neutron-local-services/_proxy/catalog.json";
const DEFAULT_HMAC_KEY_PATH: &str = "/var/lib/neutron-local-services/_proxy/hmac.key";
const DEFAULT_ADMIN_TOKEN_PATH: &str = "/var/lib/neutron-local-services/_proxy/admin.token";
const DEFAULT_PRIV_SOCKET: &str = "/var/run/neutron-local-services/_proxy/priv.sock";
const DEFAULT_CONTROL_SOCKET: &str = "/var/run/neutron-local-services/_proxy/control.sock";
const DEFAULT_ADMIN_SOCKET: &str = "/var/run/neutron-local-services/_proxy/admin.sock";
const DEFAULT_BOOT_ID_PATH: &str = "/var/run/neutron-local-services/_proxy/worker.boot";

fn main() -> Result<()> {
    init_tracing();

    let cfg = Config::from_env();
    tracing::info!(?cfg, "nls-proxy starting");

    let key = std::fs::read(&cfg.hmac_key_path)
        .with_context(|| format!("read HMAC key {}", cfg.hmac_key_path.display()))?;
    let admin_token = std::fs::read_to_string(&cfg.admin_token_path)
        .with_context(|| format!("read admin token {}", cfg.admin_token_path.display()))?
        .trim()
        .to_owned();

    // Boot-id sentinel. Written atomically so the agent (which
    // reads it on every `_ensure_registered`) can detect a worker
    // restart and re-issue `AddNetns` for every tenant netns it
    // had previously registered. Without this, a worker restart
    // would leave the agent thinking everything is still
    // registered while the worker has zero per-tenant threads
    // and zero bound listeners.
    write_boot_id(&cfg.boot_id_path)
        .with_context(|| format!("write boot id {}", cfg.boot_id_path.display()))?;

    let host_inode = watchdog::capture_host_netns_inode().context("capture host netns inode")?;
    tracing::info!(host_inode, "captured host netns inode");

    let metrics = metrics::WorkerMetrics::new();
    let catalog_rx = catalog::spawn_watcher(
        cfg.catalog_path.clone(),
        key,
        Arc::clone(&metrics),
    );

    let initial_status = hc::empty_status_map();
    let status_rx = hc::spawn(initial_status, catalog_rx.clone());

    let _admin_join = admin::spawn(
        cfg.admin_socket.clone(),
        admin::AdminState {
            catalog_rx: catalog_rx.clone(),
            status_rx: status_rx.clone(),
            bearer_token: Arc::new(admin_token),
            metrics: Arc::clone(&metrics),
        },
    );

    let _watchdog_join = watchdog::spawn(host_inode);

    // Block on the control socket loop. This thread becomes the
    // dispatcher's accept loop; if it returns we exit.
    let dispatcher = dispatcher::Dispatcher {
        control_socket: cfg.control_socket,
        priv_socket: cfg.priv_socket,
        catalog_rx,
        status_rx,
    };
    dispatcher.run()
}

#[derive(Debug, Clone)]
struct Config {
    catalog_path: PathBuf,
    hmac_key_path: PathBuf,
    admin_token_path: PathBuf,
    priv_socket: PathBuf,
    control_socket: PathBuf,
    admin_socket: PathBuf,
    boot_id_path: PathBuf,
}

impl Config {
    fn from_env() -> Self {
        Self {
            catalog_path: env_path("NLS_PROXY_CATALOG", DEFAULT_CATALOG_PATH),
            hmac_key_path: env_path("NLS_PROXY_HMAC_KEY", DEFAULT_HMAC_KEY_PATH),
            admin_token_path: env_path("NLS_PROXY_ADMIN_TOKEN", DEFAULT_ADMIN_TOKEN_PATH),
            priv_socket: env_path("NLS_PROXY_PRIV_SOCK", DEFAULT_PRIV_SOCKET),
            control_socket: env_path("NLS_PROXY_CONTROL_SOCK", DEFAULT_CONTROL_SOCKET),
            admin_socket: env_path("NLS_PROXY_ADMIN_SOCK", DEFAULT_ADMIN_SOCKET),
            boot_id_path: env_path("NLS_PROXY_BOOT_ID", DEFAULT_BOOT_ID_PATH),
        }
    }
}

fn write_boot_id(path: &std::path::Path) -> Result<()> {
    use std::time::{SystemTime, UNIX_EPOCH};
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).ok();
    }
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let pid = std::process::id();
    let id = format!("{nanos}-{pid}\n");
    let tmp = path.with_extension("boot.tmp");
    std::fs::write(&tmp, &id)
        .with_context(|| format!("write {}", tmp.display()))?;
    std::fs::rename(&tmp, path)
        .with_context(|| format!("rename {} -> {}", tmp.display(), path.display()))?;
    tracing::info!(path = %path.display(), boot_id = %id.trim(), "wrote boot-id sentinel");
    Ok(())
}

fn env_path(var: &str, default: &str) -> PathBuf {
    std::env::var(var)
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from(default))
}

fn init_tracing() {
    use tracing_subscriber::{fmt, EnvFilter};
    let filter = EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| EnvFilter::new("info,nls_proxy=info"));
    fmt().with_env_filter(filter).with_target(true).init();
}
