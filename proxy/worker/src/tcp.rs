//! Per-tenant TCP forwarder.
//!
//! Wraps a listener fd bound by the priv helper inside the tenant
//! netns. Accepts connections, picks a healthy backend via the
//! per-listener selector, dials a fresh `TcpStream` to that backend
//! (in host root netns by virtue of the worker thread's permanent
//! netns), and runs `tokio::io::copy_bidirectional`.

use std::net::SocketAddr;
use std::os::fd::OwnedFd;
use std::sync::Arc;

use tokio::sync::watch;

use crate::catalog::Entry;
use crate::hc::{BackendId, Status, StatusMap};
use crate::lb::Selector;
use crate::quota::ListenerQuota;

pub async fn run(
    listener_fd: OwnedFd,
    mut entry_rx: watch::Receiver<Arc<Entry>>,
    mut status_rx: watch::Receiver<StatusMap>,
    quota: Arc<ListenerQuota>,
) {
    let std_listener = std::net::TcpListener::from(listener_fd);
    if let Err(e) = std_listener.set_nonblocking(true) {
        tracing::error!(error = %e, "set_nonblocking on TCP listener");
        return;
    }
    let listener = match tokio::net::TcpListener::from_std(std_listener) {
        Ok(l) => l,
        Err(e) => {
            tracing::error!(error = %e, "TcpListener::from_std");
            return;
        }
    };
    let selector = Arc::new(Selector::new());
    let local_addr = listener.local_addr().ok();
    tracing::info!(?local_addr, "tcp accept loop running");

    loop {
        tokio::select! {
            res = listener.accept() => {
                match res {
                    Ok((client, peer)) => {
                        let entry = entry_rx.borrow_and_update().clone();
                        let status = status_rx.borrow_and_update().clone();
                        let backend = selector.pick(&entry, |b| {
                            let id = BackendId::from(&entry, b);
                            matches!(status.get(&id), Some(Status::Up) | Some(Status::Unknown) | None)
                        });
                        let Some(backend) = backend else {
                            tracing::warn!(?peer, "no healthy backend; dropping conn");
                            drop(client);
                            continue;
                        };
                        let Some(guard) = quota.try_acquire() else {
                            tracing::warn!(?peer, max = quota.max_concurrent(), "quota breach; dropping conn");
                            drop(client);
                            continue;
                        };
                        let backend_addr = SocketAddr::new(backend.addr, backend.port);
                        tokio::task::spawn_local(async move {
                            // Hold guard until both halves close.
                            let _g = guard;
                            forward_one(client, backend_addr).await;
                        });
                    }
                    Err(e) => {
                        tracing::warn!(error = %e, "accept error; sleeping briefly");
                        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
                    }
                }
            }
            // Catalog change. We don't need to do anything special;
            // the next accept reads the freshest Entry. But wake up
            // so the loop can react if someone wants to abort us.
            _ = entry_rx.changed() => {}
            _ = status_rx.changed() => {}
        }
    }
}

async fn forward_one(client: tokio::net::TcpStream, backend: SocketAddr) {
    let peer = client.peer_addr().ok();
    let backend_stream = match tokio::net::TcpStream::connect(backend).await {
        Ok(s) => s,
        Err(e) => {
            tracing::warn!(?peer, %backend, error = %e, "backend connect failed");
            return;
        }
    };
    let (mut cr, mut cw) = client.into_split();
    let (mut br, mut bw) = backend_stream.into_split();
    let c2b = async { tokio::io::copy(&mut cr, &mut bw).await };
    let b2c = async { tokio::io::copy(&mut br, &mut cw).await };
    let _ = tokio::join!(c2b, b2c);
}

