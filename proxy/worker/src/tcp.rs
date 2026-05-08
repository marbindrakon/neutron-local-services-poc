//! Per-tenant TCP forwarder.
//!
//! Wraps a listener fd bound by the priv helper inside the tenant
//! netns. Accepts connections, picks a healthy backend via the
//! per-listener selector, dials a fresh `TcpStream` to that backend
//! (in host root netns by virtue of the worker thread's permanent
//! netns), and runs `tokio::io::copy_bidirectional`.
//!
//! Bounded in time at three points:
//! - backend `connect()` is wrapped in [`CONNECT_TIMEOUT`] so a
//!   black-holing backend can't pin a tokio task forever waiting on
//!   the kernel's TCP timeout (minutes).
//! - each copy half is wrapped in a per-`read()` idle deadline
//!   ([`IDLE_TIMEOUT`]); a slow-loris client or a half-broken backend
//!   that stops sending traffic but keeps its socket open gets
//!   reaped instead of holding the per-listener quota slot
//!   indefinitely.

use std::net::SocketAddr;
use std::os::fd::OwnedFd;
use std::sync::Arc;
use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::sync::watch;

use crate::catalog::Entry;
use crate::hc::{BackendId, Status, StatusMap};
use crate::lb::Selector;
use crate::quota::ListenerQuota;

/// Hard cap on `connect()` to a backend.
const CONNECT_TIMEOUT: Duration = Duration::from_secs(5);

/// Per-`read()` idle deadline on each direction of the bidirectional
/// copy. If a half goes idle for longer than this we tear the whole
/// connection down. Generous enough to survive long-poll / keepalive
/// patterns; short enough that a stuck flow doesn't hold the quota
/// slot forever.
const IDLE_TIMEOUT: Duration = Duration::from_secs(300);

/// Buffer for the byte-shovel loop. Sized to fit a typical jumbo
/// frame's worth of payload without churning the allocator.
const COPY_BUF_BYTES: usize = 16 * 1024;

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
    let backend_stream = match tokio::time::timeout(
        CONNECT_TIMEOUT,
        tokio::net::TcpStream::connect(backend),
    )
    .await
    {
        Ok(Ok(s)) => s,
        Ok(Err(e)) => {
            tracing::warn!(?peer, %backend, error = %e, "backend connect failed");
            return;
        }
        Err(_) => {
            tracing::warn!(?peer, %backend, timeout_s = CONNECT_TIMEOUT.as_secs(), "backend connect timed out");
            return;
        }
    };
    let (mut cr, mut cw) = client.into_split();
    let (mut br, mut bw) = backend_stream.into_split();
    let c2b = copy_with_idle_timeout(&mut cr, &mut bw);
    let b2c = copy_with_idle_timeout(&mut br, &mut cw);
    // Either direction returning ends the conn (via Drop on the
    // halves). Both `Ok` and `Err` are normal terminations from the
    // proxy's POV — we just shovel bytes.
    let _ = tokio::join!(c2b, b2c);
}

/// Read/write loop that bails out if a single `read()` blocks for
/// longer than [`IDLE_TIMEOUT`]. We can't use `tokio::io::copy` for
/// this because it doesn't expose a per-syscall deadline.
async fn copy_with_idle_timeout<R, W>(reader: &mut R, writer: &mut W) -> std::io::Result<()>
where
    R: tokio::io::AsyncRead + Unpin,
    W: tokio::io::AsyncWrite + Unpin,
{
    let mut buf = vec![0u8; COPY_BUF_BYTES];
    loop {
        let n = match tokio::time::timeout(IDLE_TIMEOUT, reader.read(&mut buf)).await {
            Ok(Ok(0)) => {
                // Clean EOF — flush + propagate close to the peer half.
                let _ = writer.shutdown().await;
                return Ok(());
            }
            Ok(Ok(n)) => n,
            Ok(Err(e)) => return Err(e),
            Err(_) => {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::TimedOut,
                    format!("idle for >{}s", IDLE_TIMEOUT.as_secs()),
                ));
            }
        };
        writer.write_all(&buf[..n]).await?;
    }
}

