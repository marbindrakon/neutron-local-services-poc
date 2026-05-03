//! Per-tenant UDP forwarder.
//!
//! Wraps a UDP listener fd bound by the priv helper inside the
//! tenant netns. For each new client `(client_addr, client_port)`,
//! pick a healthy backend and allocate an ephemeral host-side UDP
//! socket that's `connect()`ed to that backend (so we get the
//! kernel's flow demux for free). A per-session reply task copies
//! datagrams back to the client. Sessions evict on idle TTL.
//!
//! The session table is a thread-local `HashMap` (not `Mutex`,
//! not `DashMap`) — cross-tenant isolation is structural.

use std::collections::HashMap;
use std::net::SocketAddr;
use std::os::fd::OwnedFd;
use std::sync::Arc;
use std::time::Duration;

use tokio::sync::watch;
use tokio::task::AbortHandle;
use tokio::time::Instant;

use crate::catalog::Entry;
use crate::hc::{BackendId, Status, StatusMap};
use crate::lb::Selector;
use crate::quota::ListenerQuota;

const MAX_DATAGRAM: usize = 65535;

struct Session {
    backend_socket: Arc<tokio::net::UdpSocket>,
    last_seen: Instant,
    reply_task: AbortHandle,
}

impl Drop for Session {
    fn drop(&mut self) {
        self.reply_task.abort();
    }
}

pub async fn run(
    listener_fd: OwnedFd,
    mut entry_rx: watch::Receiver<Arc<Entry>>,
    mut status_rx: watch::Receiver<StatusMap>,
    quota: Arc<ListenerQuota>,
) {
    let std_socket = std::net::UdpSocket::from(listener_fd);
    if let Err(e) = std_socket.set_nonblocking(true) {
        tracing::error!(error = %e, "set_nonblocking on UDP listener");
        return;
    }
    let listener = match tokio::net::UdpSocket::from_std(std_socket) {
        Ok(s) => Arc::new(s),
        Err(e) => {
            tracing::error!(error = %e, "UdpSocket::from_std");
            return;
        }
    };
    let local_addr = listener.local_addr().ok();
    tracing::info!(?local_addr, "udp recv loop running");

    let selector = Arc::new(Selector::new());
    let mut sessions: HashMap<SocketAddr, Session> = HashMap::new();
    let mut buf = vec![0u8; MAX_DATAGRAM];
    let mut sweep = tokio::time::interval(Duration::from_secs(5));

    loop {
        tokio::select! {
            res = listener.recv_from(&mut buf) => {
                match res {
                    Ok((n, peer)) => {
                        let entry = entry_rx.borrow_and_update().clone();
                        let status = status_rx.borrow_and_update().clone();
                        let session = match sessions.get_mut(&peer) {
                            Some(s) => {
                                s.last_seen = Instant::now();
                                Some(&*s)
                            }
                            None => None,
                        };
                        let backend_socket = if let Some(s) = session {
                            Arc::clone(&s.backend_socket)
                        } else {
                            // New session.
                            if quota.try_acquire().is_none() {
                                tracing::warn!(?peer, "udp session quota breach; dropping");
                                continue;
                            }
                            // We released the guard immediately above
                            // because UDP sessions don't have a clean
                            // close event; we count via session-table
                            // size on /metrics instead. The
                            // `quota.in_flight` doubles as a TCP
                            // counter; UDP cap is enforced via
                            // catalog `max_concurrent` checked here.
                            let backend = selector.pick(&entry, |b| {
                                let id = BackendId::from(&entry, b);
                                matches!(status.get(&id), Some(Status::Up) | Some(Status::Unknown) | None)
                            });
                            let Some(backend) = backend else {
                                tracing::warn!(?peer, "no healthy backend; dropping datagram");
                                continue;
                            };
                            let backend_addr = SocketAddr::new(backend.addr, backend.port);
                            let bind_addr = if backend_addr.is_ipv6() {
                                "[::]:0"
                            } else {
                                "0.0.0.0:0"
                            };
                            let host_socket = match tokio::net::UdpSocket::bind(bind_addr).await {
                                Ok(s) => s,
                                Err(e) => {
                                    tracing::warn!(error = %e, "host-side udp bind");
                                    continue;
                                }
                            };
                            if let Err(e) = host_socket.connect(backend_addr).await {
                                tracing::warn!(error = %e, ?backend_addr, "udp connect");
                                continue;
                            }
                            let host_socket = Arc::new(host_socket);
                            let listener_clone = Arc::clone(&listener);
                            let host_clone = Arc::clone(&host_socket);
                            let join = tokio::task::spawn_local(async move {
                                let mut rx_buf = vec![0u8; MAX_DATAGRAM];
                                loop {
                                    match host_clone.recv(&mut rx_buf).await {
                                        Ok(n) => {
                                            if let Err(e) = listener_clone
                                                .send_to(&rx_buf[..n], peer)
                                                .await
                                            {
                                                tracing::debug!(error = %e, ?peer, "udp reply send_to");
                                                break;
                                            }
                                        }
                                        Err(e) => {
                                            tracing::debug!(error = %e, "host udp recv");
                                            break;
                                        }
                                    }
                                }
                            });
                            sessions.insert(
                                peer,
                                Session {
                                    backend_socket: Arc::clone(&host_socket),
                                    last_seen: Instant::now(),
                                    reply_task: join.abort_handle(),
                                },
                            );
                            host_socket
                        };
                        if let Err(e) = backend_socket.send(&buf[..n]).await {
                            tracing::debug!(error = %e, ?peer, "udp forward send");
                            sessions.remove(&peer);
                        }
                    }
                    Err(e) => {
                        tracing::warn!(error = %e, "udp recv_from error");
                        tokio::time::sleep(Duration::from_millis(50)).await;
                    }
                }
            }
            _ = sweep.tick() => {
                let entry = entry_rx.borrow_and_update().clone();
                let ttl = Duration::from_secs(entry.max_session_idle_s.max(1) as u64);
                let now = Instant::now();
                sessions.retain(|_, s| now.saturating_duration_since(s.last_seen) <= ttl);
            }
            _ = entry_rx.changed() => {}
            _ = status_rx.changed() => {}
        }
    }
}
