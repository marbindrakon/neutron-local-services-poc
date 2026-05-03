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
//! configured at startup.
//!
//! The unix socket itself is mode 0600 + group `nls-admin` (chosen by
//! the systemd unit). Bearer-token auth is defense-in-depth against
//! anyone who already has the socket fd.

use std::collections::HashMap;
use std::path::PathBuf;
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

    loop {
        let (stream, _) = listener.accept().await?;
        let app = app.clone();
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
    if !got.starts_with(prefix) || &got[prefix.len()..] != expected {
        return Err(StatusCode::UNAUTHORIZED);
    }
    Ok(())
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
