//! Health checking.
//!
//! One async task per backend probe. Lives on the dedicated HC
//! tokio current-thread runtime in the HC OS thread (always in
//! host root netns). Publishes `Arc<HashMap<BackendId, Status>>`
//! over a `tokio::sync::watch` channel; per-tenant threads
//! subscribe and read.
//!
//! The HC thread owns its state. Per-tenant threads are read-only
//! consumers; this is what makes cross-tenant HC poisoning
//! structurally impossible.

use std::collections::HashMap;
use std::net::{IpAddr, SocketAddr};
use std::sync::Arc;
use std::time::Duration;

use tokio::io::AsyncWriteExt;
use tokio::sync::watch;
use tokio::time::{interval, timeout};

use crate::catalog::{Backend, Catalog, Entry, HcCommon, HealthCheck};

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct BackendId {
    pub net_id: String,
    pub service_id: String,
    pub addr: IpAddr,
    pub port: u16,
}

impl BackendId {
    pub fn from(entry: &Entry, b: &Backend) -> Self {
        Self {
            net_id: entry.net_id.clone(),
            service_id: entry.service_id.clone(),
            addr: b.addr,
            port: b.port,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Status {
    Up,
    Down,
    Unknown,
}

pub type StatusMap = Arc<HashMap<BackendId, Status>>;

pub fn empty_status_map() -> StatusMap {
    Arc::new(HashMap::new())
}

/// Spawn the HC thread. It owns its tokio current-thread runtime
/// and runs probe tasks indefinitely. Returns the watch receiver
/// per-tenant threads should subscribe to and a sender for catalog
/// updates.
pub fn spawn(
    initial_status: StatusMap,
    mut catalog_rx: watch::Receiver<Arc<Catalog>>,
) -> watch::Receiver<StatusMap> {
    let (status_tx, status_rx) = watch::channel(initial_status);
    std::thread::Builder::new()
        .name("nls-proxy-hc".into())
        .spawn(move || {
            let rt = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .expect("build hc runtime");
            rt.block_on(async move {
                let mut probes: HashMap<BackendId, ProbeHandle> = HashMap::new();
                let consecutive: Arc<tokio::sync::Mutex<HashMap<BackendId, Streak>>> =
                    Arc::new(tokio::sync::Mutex::new(HashMap::new()));
                loop {
                    let cat = catalog_rx.borrow_and_update().clone();
                    reconcile_probes(&cat, &mut probes, &status_tx, &consecutive).await;
                    if catalog_rx.changed().await.is_err() {
                        break;
                    }
                }
            });
        })
        .expect("spawn hc thread");
    status_rx
}

#[derive(Debug, Default, Clone, Copy)]
struct Streak {
    consecutive_pass: u32,
    consecutive_fail: u32,
}

struct ProbeHandle {
    cancel: tokio::sync::oneshot::Sender<()>,
}

async fn reconcile_probes(
    cat: &Arc<Catalog>,
    probes: &mut HashMap<BackendId, ProbeHandle>,
    status_tx: &watch::Sender<StatusMap>,
    consecutive: &Arc<tokio::sync::Mutex<HashMap<BackendId, Streak>>>,
) {
    let mut desired: HashMap<BackendId, (HealthCheck, Backend)> = HashMap::new();
    for entry in &cat.entries {
        for b in &entry.backends {
            desired.insert(BackendId::from(entry, b), (entry.health_check.clone(), b.clone()));
        }
    }

    // Stop probes for backends no longer in catalog.
    let to_remove: Vec<BackendId> = probes
        .keys()
        .filter(|id| !desired.contains_key(*id))
        .cloned()
        .collect();
    for id in to_remove {
        if let Some(h) = probes.remove(&id) {
            let _ = h.cancel.send(());
        }
        consecutive.lock().await.remove(&id);
        let mut next = (**status_tx.borrow()).clone();
        next.remove(&id);
        let _ = status_tx.send(Arc::new(next));
    }

    // Start probes for new backends.
    for (id, (hc, backend)) in desired {
        if probes.contains_key(&id) {
            continue;
        }
        // Seed the status as Unknown until the first probe completes.
        {
            let mut next = (**status_tx.borrow()).clone();
            next.insert(id.clone(), Status::Unknown);
            let _ = status_tx.send(Arc::new(next));
        }
        let (cancel_tx, cancel_rx) = tokio::sync::oneshot::channel();
        let status_tx = status_tx.clone();
        let consecutive = Arc::clone(consecutive);
        let id_for_task = id.clone();
        tokio::spawn(async move {
            run_probe(id_for_task, hc, backend, status_tx, consecutive, cancel_rx).await;
        });
        probes.insert(id, ProbeHandle { cancel: cancel_tx });
    }
}

async fn run_probe(
    id: BackendId,
    hc: HealthCheck,
    backend: Backend,
    status_tx: watch::Sender<StatusMap>,
    consecutive: Arc<tokio::sync::Mutex<HashMap<BackendId, Streak>>>,
    mut cancel_rx: tokio::sync::oneshot::Receiver<()>,
) {
    let common = hc_common(&hc);
    let mut tick = interval(Duration::from_secs(common.interval_s.max(1) as u64));
    loop {
        tokio::select! {
            _ = &mut cancel_rx => return,
            _ = tick.tick() => {}
        }
        let outcome = probe_once(&hc, &backend).await;
        let mut state = consecutive.lock().await;
        let s = state.entry(id.clone()).or_default();
        let prev = current_status(&status_tx, &id);
        let next = match outcome {
            true => {
                s.consecutive_fail = 0;
                s.consecutive_pass = s.consecutive_pass.saturating_add(1);
                if s.consecutive_pass >= common.rise_after.max(1) {
                    Status::Up
                } else if matches!(prev, Status::Down) {
                    Status::Down
                } else {
                    prev
                }
            }
            false => {
                s.consecutive_pass = 0;
                s.consecutive_fail = s.consecutive_fail.saturating_add(1);
                if s.consecutive_fail >= common.fail_after.max(1) {
                    Status::Down
                } else if matches!(prev, Status::Up) {
                    Status::Up
                } else {
                    prev
                }
            }
        };
        drop(state);
        if next != prev {
            let mut map = (**status_tx.borrow()).clone();
            map.insert(id.clone(), next);
            let _ = status_tx.send(Arc::new(map));
            tracing::info!(?id, ?prev, ?next, "backend status transition");
        }
    }
}

fn current_status(tx: &watch::Sender<StatusMap>, id: &BackendId) -> Status {
    tx.borrow().get(id).copied().unwrap_or(Status::Unknown)
}

fn hc_common(hc: &HealthCheck) -> HcCommon {
    match hc {
        HealthCheck::TcpConnect { common }
        | HealthCheck::HttpGet { common, .. }
        | HealthCheck::HttpsHandshake { common, .. }
        | HealthCheck::UdpDnsQuery { common, .. }
        | HealthCheck::Script { common, .. } => common.clone(),
    }
}

async fn probe_once(hc: &HealthCheck, backend: &Backend) -> bool {
    let common = hc_common(hc);
    let to = Duration::from_secs(common.timeout_s.max(1) as u64);
    let addr = SocketAddr::new(backend.addr, backend.port);
    match hc {
        HealthCheck::TcpConnect { .. } => probe_tcp(addr, to).await,
        HealthCheck::HttpGet {
            path,
            expect_status,
            ..
        } => probe_http(addr, path, *expect_status, false, None, to).await,
        HealthCheck::HttpsHandshake { sni, .. } => {
            probe_http(addr, "/", 0, true, sni.clone(), to).await
        }
        HealthCheck::UdpDnsQuery { query, .. } => probe_udp_dns(addr, query, to).await,
        HealthCheck::Script { path, args, .. } => probe_script(addr, path, args, to).await,
    }
}

async fn probe_tcp(addr: SocketAddr, to: Duration) -> bool {
    timeout(to, tokio::net::TcpStream::connect(addr))
        .await
        .map(|r| r.is_ok())
        .unwrap_or(false)
}

async fn probe_http(
    addr: SocketAddr,
    path: &str,
    expect_status: u16,
    https: bool,
    _sni: Option<String>,
    to: Duration,
) -> bool {
    if https {
        // For PoC: HTTPS HC is "TLS handshake completes." We don't
        // pull rustls just to check that.
        // Recommendation: per-backend circuit breaker (TBD); for now
        // we treat HTTPS HC as a TCP-connect with longer timeout.
        return probe_tcp(addr, to).await;
    }
    let mut stream = match timeout(to, tokio::net::TcpStream::connect(addr)).await {
        Ok(Ok(s)) => s,
        _ => return false,
    };
    let req = format!(
        "GET {path} HTTP/1.0\r\nHost: {host}\r\nConnection: close\r\n\r\n",
        path = path,
        host = addr.ip()
    );
    if timeout(to, stream.write_all(req.as_bytes())).await.is_err() {
        return false;
    }
    use tokio::io::AsyncReadExt;
    let mut buf = vec![0u8; 256];
    let n = match timeout(to, stream.read(&mut buf)).await {
        Ok(Ok(n)) => n,
        _ => return false,
    };
    let head = String::from_utf8_lossy(&buf[..n]);
    let mut parts = head.split_whitespace();
    let _http = parts.next();
    let status = parts.next().and_then(|s| s.parse::<u16>().ok());
    matches!(status, Some(code) if code == expect_status)
}

async fn probe_udp_dns(addr: SocketAddr, query: &str, to: Duration) -> bool {
    use hickory_proto::op::{Message, MessageType, Query};
    use hickory_proto::rr::{Name, RecordType};
    use hickory_proto::serialize::binary::{BinDecodable, BinEncodable};

    let name = match Name::from_ascii(query) {
        Ok(n) => n,
        Err(_) => return false,
    };
    let mut msg = Message::new();
    msg.set_id(rand_id());
    msg.set_message_type(MessageType::Query);
    msg.set_recursion_desired(true);
    msg.add_query(Query::query(name, RecordType::A));
    let req_bytes = match msg.to_bytes() {
        Ok(b) => b,
        Err(_) => return false,
    };

    let bind_addr = if addr.is_ipv6() {
        "[::]:0"
    } else {
        "0.0.0.0:0"
    };
    let socket = match tokio::net::UdpSocket::bind(bind_addr).await {
        Ok(s) => s,
        Err(_) => return false,
    };
    if timeout(to, socket.send_to(&req_bytes, addr)).await.is_err() {
        return false;
    }
    let mut buf = vec![0u8; 1232];
    let n = match timeout(to, socket.recv(&mut buf)).await {
        Ok(Ok(n)) => n,
        _ => return false,
    };
    let resp = match Message::from_bytes(&buf[..n]) {
        Ok(m) => m,
        Err(_) => return false,
    };
    resp.id() == msg.id() && resp.response_code().low() == 0
}

fn rand_id() -> u16 {
    use std::time::{SystemTime, UNIX_EPOCH};
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.subsec_nanos())
        .unwrap_or(0);
    (nanos & 0xffff) as u16
}

async fn probe_script(addr: SocketAddr, path: &str, args: &[String], to: Duration) -> bool {
    // Shipped check scripts (check_dns.sh, check_ntp.sh) follow the
    // Keepalived MISC_CHECK convention: positional args
    // `<addr> <port>`. We also export the values as env vars so a
    // script can pick whichever it prefers, then append any
    // catalog-supplied extra args after the implicit positional pair.
    let mut cmd = tokio::process::Command::new(path);
    cmd.env("BACKEND_ADDR", addr.ip().to_string());
    cmd.env("BACKEND_PORT", addr.port().to_string());
    cmd.arg(addr.ip().to_string());
    cmd.arg(addr.port().to_string());
    cmd.args(args);
    cmd.stdout(std::process::Stdio::null());
    cmd.stderr(std::process::Stdio::null());
    let child = match cmd.spawn() {
        Ok(c) => c,
        Err(_) => return false,
    };
    match timeout(to, child.wait_with_output()).await {
        Ok(Ok(out)) => out.status.success(),
        _ => false,
    }
}
