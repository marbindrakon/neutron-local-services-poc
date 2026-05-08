//! Per-tenant data-path thread.
//!
//! One OS thread + one tokio current-thread runtime per tenant
//! netns. Owns the netns fd (handed off by the agent via the worker
//! control socket) and the bound listener fds for that tenant.
//! Reconciles its listener set against the latest catalog snapshot
//! whenever the catalog changes.
//!
//! This thread runs in the host root netns for its entire life. It
//! never calls `setns()` — that's the priv helper's exclusive job.
//! All listener fds it holds were bound by the priv helper inside
//! the tenant netns; the kernel routes I/O on them based on the
//! socket's bind-time netns, not the calling thread's netns.

use std::collections::HashMap;
use std::net::IpAddr;
use std::os::fd::{AsFd, OwnedFd};
use std::path::PathBuf;
use std::sync::Arc;

use tokio::sync::watch;
use tokio::task::{AbortHandle, LocalSet};

use nls_proxy_wire::Proto;

use crate::catalog::{Catalog, Entry};
use crate::hc::StatusMap;
use crate::priv_client;
use crate::quota::ListenerQuota;
use crate::{tcp, udp};

pub type ListenerKey = (IpAddr, u16, Proto);

pub fn spawn(
    net_id: String,
    netns_fd: OwnedFd,
    catalog_rx: watch::Receiver<Arc<Catalog>>,
    status_rx: watch::Receiver<StatusMap>,
    priv_socket: PathBuf,
    shutdown_rx: tokio::sync::oneshot::Receiver<()>,
) -> std::thread::JoinHandle<()> {
    let thread_name = format!("nls-tenant-{}", &net_id[..net_id.len().min(8)]);
    std::thread::Builder::new()
        .name(thread_name)
        .spawn(move || {
            let rt = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .expect("build tenant runtime");
            let local = LocalSet::new();
            local.block_on(&rt, run(net_id, netns_fd, catalog_rx, status_rx, priv_socket, shutdown_rx));
        })
        .expect("spawn tenant thread")
}

struct ListenerHandle {
    entry_tx: watch::Sender<Arc<Entry>>,
    // Keeps the per-listener quota object alive while the listener is
    // running, so /metrics can read counters from it (follow-up).
    #[allow(dead_code)]
    quota: Arc<ListenerQuota>,
    abort: AbortHandle,
    // We keep listener fds alive as long as ListenerHandle lives by
    // letting the spawned task own them. Aborting the task drops the
    // fd; nothing else holds it.
}

async fn run(
    net_id: String,
    netns_fd: OwnedFd,
    mut catalog_rx: watch::Receiver<Arc<Catalog>>,
    status_rx: watch::Receiver<StatusMap>,
    priv_socket: PathBuf,
    mut shutdown_rx: tokio::sync::oneshot::Receiver<()>,
) {
    let mut listeners: HashMap<ListenerKey, ListenerHandle> = HashMap::new();
    tracing::info!(net_id = %net_id, "tenant supervisor running");

    loop {
        let cat = catalog_rx.borrow_and_update().clone();
        reconcile(
            &net_id,
            &netns_fd,
            &priv_socket,
            &cat,
            &status_rx,
            &mut listeners,
        )
        .await;

        tokio::select! {
            res = catalog_rx.changed() => {
                if res.is_err() {
                    break;
                }
            }
            _ = &mut shutdown_rx => {
                tracing::info!(net_id = %net_id, "tenant supervisor shutting down");
                break;
            }
        }
    }
    // Aborts all listener tasks + drops their fds.
    listeners.clear();
}

async fn reconcile(
    net_id: &str,
    netns_fd: &OwnedFd,
    priv_socket: &std::path::Path,
    cat: &Arc<Catalog>,
    status_rx: &watch::Receiver<StatusMap>,
    listeners: &mut HashMap<ListenerKey, ListenerHandle>,
) {
    // Compute desired listener set from the catalog.
    let mut desired: HashMap<ListenerKey, Arc<Entry>> = HashMap::new();
    for entry in &cat.entries {
        if entry.net_id != net_id {
            continue;
        }
        let key = (entry.vip, entry.port, entry.proto);
        desired.insert(key, Arc::new(entry.clone()));
    }

    // Drop listeners no longer wanted.
    let to_remove: Vec<ListenerKey> = listeners
        .keys()
        .copied()
        .filter(|k| !desired.contains_key(k))
        .collect();
    for k in to_remove {
        if let Some(h) = listeners.remove(&k) {
            tracing::info!(?k, "removing listener");
            h.abort.abort();
            // entry_tx drops; quota drops; tasks see channel
            // closure and exit.
            drop(h);
        }
    }

    // Add or update.
    for (key, entry) in desired {
        if let Some(handle) = listeners.get(&key) {
            // Just refresh the watch so the existing accept loop
            // sees new backends/HC.
            let _ = handle.entry_tx.send(entry);
            continue;
        }
        let listener_fd = match priv_client::bind_listener(
            priv_socket,
            netns_fd.as_fd(),
            net_id,
            entry.vip,
            entry.port,
            entry.proto,
        ) {
            Ok(fd) => fd,
            Err(e) => {
                tracing::error!(?key, error = %e, "priv-helper BindListener failed");
                continue;
            }
        };
        let (entry_tx, entry_rx) = watch::channel(entry.clone());
        let quota = Arc::new(ListenerQuota::new(entry.max_concurrent));
        let status_rx_clone = status_rx.clone();
        let quota_clone = Arc::clone(&quota);
        let proto = entry.proto;
        let join = tokio::task::spawn_local(async move {
            match proto {
                Proto::Tcp => {
                    tcp::run(listener_fd, entry_rx, status_rx_clone, quota_clone).await;
                }
                Proto::Udp => {
                    udp::run(listener_fd, entry_rx, status_rx_clone, quota_clone).await;
                }
            }
        });
        listeners.insert(
            key,
            ListenerHandle {
                entry_tx,
                quota,
                abort: join.abort_handle(),
            },
        );
        tracing::info!(?key, "added listener");
    }
}
