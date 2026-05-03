//! Agent → worker control socket.
//!
//! The agent connects, sends `AddNetns` / `RemoveNetns` requests with
//! the tenant netns fd attached via SCM_RIGHTS, and the dispatcher
//! either spawns a fresh per-tenant data-path thread or signals an
//! existing one to shut down. The control socket is mode 0600 owned
//! by the agent uid (group `nls-admin`).

use std::collections::HashMap;
use std::os::fd::OwnedFd;
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::Mutex;

use anyhow::{Context, Result};
use tokio::sync::watch;

use nls_proxy_wire::{ControlRequest, ControlResponse, MAX_FRAME_BYTES};

use crate::catalog::Catalog;
use crate::hc::StatusMap;
use crate::tenant;

pub struct Dispatcher {
    pub control_socket: PathBuf,
    pub priv_socket: PathBuf,
    pub catalog_rx: watch::Receiver<Arc<Catalog>>,
    pub status_rx: watch::Receiver<StatusMap>,
}

struct TenantHandle {
    join: std::thread::JoinHandle<()>,
    shutdown: tokio::sync::oneshot::Sender<()>,
}

impl Dispatcher {
    pub fn run(self) -> Result<()> {
        let socket_path = self.control_socket.clone();
        if let Some(parent) = socket_path.parent() {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("create_dir_all({})", parent.display()))?;
        }
        if socket_path.exists() {
            std::fs::remove_file(&socket_path).ok();
        }
        let listener = std::os::unix::net::UnixListener::bind(&socket_path)
            .with_context(|| format!("bind control socket at {}", socket_path.display()))?;
        chmod_0600(&socket_path)?;

        tracing::info!(path = %socket_path.display(), "control socket listening");

        let tenants: Arc<Mutex<HashMap<String, TenantHandle>>> =
            Arc::new(Mutex::new(HashMap::new()));

        for client in listener.incoming() {
            match client {
                Ok(stream) => {
                    let tenants = Arc::clone(&tenants);
                    let priv_socket = self.priv_socket.clone();
                    let catalog_rx = self.catalog_rx.clone();
                    let status_rx = self.status_rx.clone();
                    std::thread::Builder::new()
                        .name("nls-control-conn".into())
                        .spawn(move || {
                            handle_connection(
                                stream,
                                tenants,
                                priv_socket,
                                catalog_rx,
                                status_rx,
                            );
                        })
                        .context("spawn control conn thread")?;
                }
                Err(e) => {
                    tracing::warn!(error = %e, "control accept error");
                }
            }
        }
        Ok(())
    }
}

fn chmod_0600(path: &std::path::Path) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    let perms = std::fs::Permissions::from_mode(0o600);
    std::fs::set_permissions(path, perms)
        .with_context(|| format!("chmod 0600 {}", path.display()))
}

fn handle_connection(
    stream: UnixStream,
    tenants: Arc<Mutex<HashMap<String, TenantHandle>>>,
    priv_socket: PathBuf,
    catalog_rx: watch::Receiver<Arc<Catalog>>,
    status_rx: watch::Receiver<StatusMap>,
) {
    loop {
        let len = match nls_proxy_scm::read_length_prefix(&stream) {
            Ok(Some(n)) => n,
            Ok(None) => return,
            Err(e) => {
                tracing::warn!(error = %e, "control: length-prefix read failed");
                return;
            }
        };
        if len > MAX_FRAME_BYTES {
            tracing::warn!(len, "control: oversize frame");
            return;
        }
        let (body, fds) = match nls_proxy_scm::recv_with_fds(&stream, len) {
            Ok(t) => t,
            Err(e) => {
                tracing::warn!(error = %e, "control: recv body failed");
                return;
            }
        };
        let req: ControlRequest = match serde_json::from_slice(&body) {
            Ok(r) => r,
            Err(e) => {
                let _ = send_response(&stream, ControlResponse::Error {
                    msg: format!("decode: {e}"),
                });
                return;
            }
        };
        let resp = dispatch(
            req,
            fds,
            &tenants,
            &priv_socket,
            &catalog_rx,
            &status_rx,
        );
        if let Err(e) = send_response(&stream, resp) {
            tracing::warn!(error = %e, "control: send response failed");
            return;
        }
    }
}

fn dispatch(
    req: ControlRequest,
    fds: Vec<OwnedFd>,
    tenants: &Mutex<HashMap<String, TenantHandle>>,
    priv_socket: &std::path::Path,
    catalog_rx: &watch::Receiver<Arc<Catalog>>,
    status_rx: &watch::Receiver<StatusMap>,
) -> ControlResponse {
    match req {
        ControlRequest::AddNetns { net_id } => {
            let netns_fd = match fds.into_iter().next() {
                Some(fd) => fd,
                None => return ControlResponse::Error {
                    msg: "AddNetns: missing netns fd in SCM_RIGHTS".into(),
                },
            };
            let mut guard = tenants.lock().expect("tenants mutex poisoned");
            if guard.contains_key(&net_id) {
                return ControlResponse::Error {
                    msg: format!("AddNetns: net_id {net_id} already registered"),
                };
            }
            let (sd_tx, sd_rx) = tokio::sync::oneshot::channel();
            let join = tenant::spawn(
                net_id.clone(),
                netns_fd,
                catalog_rx.clone(),
                status_rx.clone(),
                priv_socket.to_path_buf(),
                sd_rx,
            );
            guard.insert(
                net_id.clone(),
                TenantHandle {
                    join,
                    shutdown: sd_tx,
                },
            );
            tracing::info!(%net_id, "added tenant");
            ControlResponse::Ok
        }
        ControlRequest::RemoveNetns { net_id } => {
            let mut guard = tenants.lock().expect("tenants mutex poisoned");
            match guard.remove(&net_id) {
                Some(h) => {
                    let _ = h.shutdown.send(());
                    // Don't block the control socket on the join.
                    std::thread::Builder::new()
                        .name("nls-tenant-reaper".into())
                        .spawn(move || {
                            let _ = h.join.join();
                        })
                        .ok();
                    tracing::info!(%net_id, "removed tenant");
                    ControlResponse::Ok
                }
                None => ControlResponse::Error {
                    msg: format!("RemoveNetns: net_id {net_id} not registered"),
                },
            }
        }
    }
}

fn send_response(stream: &UnixStream, resp: ControlResponse) -> Result<()> {
    let body = serde_json::to_vec(&resp)?;
    nls_proxy_scm::send_frame_with_fds(stream, &body, &[])
}
