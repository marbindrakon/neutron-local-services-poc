//! Admin endpoint over a unix socket.
//!
//! Routes:
//! - `GET  /healthz`            → 200 "ok"
//! - `GET  /clusters?format=json` → envoy-shape cluster status JSON
//! - `GET  /listeners`          → JSON list of (net_id, vip, port, proto)
//! - `GET  /metrics`            → Prometheus text format
//!
//! All routes except `/healthz` require `Authorization: Bearer <token>`
//! where `<token>` is the contents of the per-boot token file
//! configured at startup. The bearer compare is constant-time.
//!
//! Peer authorization: every accepted connection must come from a peer
//! whose effective uid matches the worker's own uid (the agent runs as
//! the same user). Filesystem mode is hardening on top of that, not
//! authorization on its own.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use anyhow::{Context, Result};
use axum::extract::State;
use axum::http::{HeaderMap, StatusCode};
use axum::response::IntoResponse;
use axum::routing::get;
use axum::Json;
use axum::Router;
use serde_json::json;
use tokio::sync::watch;

/// Cap on simultaneously-served admin connections. The local agent
/// is the only legitimate caller (peer_uid is uid-checked) and
/// keeps a small pool for scrapes; anything past this cap is a
/// scraper bug or a stuck handler.
const MAX_CONCURRENT_CONNS: usize = 16;

use crate::catalog::Catalog;
use crate::hc::{Status, StatusMap};
use crate::metrics;

#[derive(Clone)]
pub struct AdminState {
    pub catalog_rx: watch::Receiver<Arc<Catalog>>,
    pub status_rx: watch::Receiver<StatusMap>,
    pub bearer_token: Arc<String>,
    pub metrics: Arc<metrics::WorkerMetrics>,
}

pub fn spawn(
    socket_path: PathBuf,
    state: AdminState,
) -> std::thread::JoinHandle<()> {
    std::thread::Builder::new()
        .name("nls-admin".into())
        .spawn(move || {
            let rt = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .expect("build admin runtime");
            rt.block_on(async move {
                if let Err(e) = serve(socket_path, state).await {
                    tracing::error!(error = %e, "admin server exited");
                }
            });
        })
        .expect("spawn admin")
}

async fn serve(socket_path: PathBuf, state: AdminState) -> Result<()> {
    if let Some(parent) = socket_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    if socket_path.exists() {
        std::fs::remove_file(&socket_path).ok();
    }
    let listener = tokio::net::UnixListener::bind(&socket_path)
        .with_context(|| format!("bind admin socket at {}", socket_path.display()))?;
    chmod_0600(&socket_path)?;

    let app = Router::new()
        .route("/healthz", get(healthz))
        .route("/clusters", get(clusters))
        .route("/listeners", get(listeners))
        .route("/metrics", get(prom_metrics))
        .with_state(state);

    tracing::info!(path = %socket_path.display(), "admin listening on unix socket");

    let our_uid = nix::unistd::geteuid().as_raw();
    let in_flight = Arc::new(AtomicUsize::new(0));

    loop {
        let (stream, _) = listener.accept().await?;
        match stream.peer_cred() {
            Ok(cred) if cred.uid() == our_uid => {}
            Ok(cred) => {
                tracing::warn!(peer_uid = cred.uid(), "rejecting admin conn from foreign uid");
                continue;
            }
            Err(e) => {
                tracing::warn!(error = %e, "admin peer_cred failed; dropping conn");
                continue;
            }
        }
        let prev = in_flight.fetch_add(1, Ordering::AcqRel);
        if prev >= MAX_CONCURRENT_CONNS {
            in_flight.fetch_sub(1, Ordering::AcqRel);
            tracing::warn!(
                in_flight = prev,
                cap = MAX_CONCURRENT_CONNS,
                "admin: at concurrent-connection cap; dropping accept"
            );
            // Closing the stream sends FIN; the scraper will retry.
            drop(stream);
            continue;
        }
        let app = app.clone();
        let in_flight_for_task = Arc::clone(&in_flight);
        tokio::spawn(async move {
            let svc = hyper_util::service::TowerToHyperService::new(app);
            let io = hyper_util::rt::TokioIo::new(stream);
            if let Err(e) = hyper_util::server::conn::auto::Builder::new(
                hyper_util::rt::TokioExecutor::new(),
            )
            .serve_connection(io, svc)
            .await
            {
                tracing::debug!(error = %e, "admin conn error");
            }
            in_flight_for_task.fetch_sub(1, Ordering::AcqRel);
        });
    }
}

fn chmod_0600(path: &std::path::Path) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    let perms = std::fs::Permissions::from_mode(0o600);
    std::fs::set_permissions(path, perms)
        .with_context(|| format!("chmod 0600 {}", path.display()))
}

async fn healthz() -> &'static str {
    "ok\n"
}

fn require_auth(headers: &HeaderMap, expected: &str) -> Result<(), StatusCode> {
    let got = headers
        .get(axum::http::header::AUTHORIZATION)
        .and_then(|h| h.to_str().ok())
        .unwrap_or("");
    let prefix = "Bearer ";
    let presented = got.strip_prefix(prefix).unwrap_or("");
    if !constant_time_eq(presented.as_bytes(), expected.as_bytes()) {
        return Err(StatusCode::UNAUTHORIZED);
    }
    Ok(())
}

/// Length-prefixed constant-time byte compare. Returns `false` for
/// length mismatch without touching either buffer further; for equal
/// lengths the loop's branch count is independent of where the bytes
/// differ.
fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff: u8 = 0;
    for (x, y) in a.iter().zip(b.iter()) {
        diff |= x ^ y;
    }
    diff == 0
}

async fn clusters(
    State(state): State<AdminState>,
    headers: HeaderMap,
) -> Result<Json<serde_json::Value>, StatusCode> {
    require_auth(&headers, &state.bearer_token)?;
    let cat = state.catalog_rx.borrow().clone();
    let status = state.status_rx.borrow().clone();

    // Group entries by service_id so the resulting "clusters" list
    // mirrors envoy's shape: one cluster per backend pool.
    let mut by_service: HashMap<&str, Vec<&crate::catalog::Entry>> = HashMap::new();
    for e in &cat.entries {
        by_service.entry(&e.service_id).or_default().push(e);
    }

    let mut cluster_statuses = Vec::new();
    for (service_id, entries) in by_service {
        let mut host_statuses = Vec::new();
        for entry in &entries {
            for b in &entry.backends {
                let id = crate::hc::BackendId::from(entry, b);
                let eds = match status.get(&id) {
                    Some(Status::Up) => "HEALTHY",
                    Some(Status::Down) => "UNHEALTHY",
                    Some(Status::Unknown) | None => "UNKNOWN",
                };
                host_statuses.push(json!({
                    "address": {
                        "socket_address": {
                            "address": b.addr.to_string(),
                            "port_value": b.port,
                        }
                    },
                    "health_status": { "eds_health_status": eds },
                    "weight": b.weight,
                }));
            }
        }
        cluster_statuses.push(json!({
            "name": service_id,
            "host_statuses": host_statuses,
        }));
    }

    Ok(Json(json!({ "cluster_statuses": cluster_statuses })))
}

async fn listeners(
    State(state): State<AdminState>,
    headers: HeaderMap,
) -> Result<Json<serde_json::Value>, StatusCode> {
    require_auth(&headers, &state.bearer_token)?;
    let cat = state.catalog_rx.borrow().clone();
    let listeners: Vec<_> = cat
        .entries
        .iter()
        .map(|e| {
            json!({
                "net_id": e.net_id,
                "service_id": e.service_id,
                "vip": e.vip.to_string(),
                "port": e.port,
                "proto": match e.proto {
                    nls_proxy_wire::Proto::Tcp => "tcp",
                    nls_proxy_wire::Proto::Udp => "udp",
                },
                "backends": e.backends.len(),
            })
        })
        .collect();
    Ok(Json(json!({ "listeners": listeners })))
}

async fn prom_metrics(
    State(state): State<AdminState>,
    headers: HeaderMap,
) -> Result<impl IntoResponse, StatusCode> {
    require_auth(&headers, &state.bearer_token)?;
    let cat = state.catalog_rx.borrow().clone();
    let status = state.status_rx.borrow().clone();
    let body = state.metrics.render(&cat, &status);
    Ok((
        [(axum::http::header::CONTENT_TYPE, "text/plain; version=0.0.4")],
        body,
    ))
}
