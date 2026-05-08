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
use std::sync::{Arc, Once};
use std::time::Duration;

use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::pki_types::{CertificateDer, ServerName, UnixTime};
use rustls::{ClientConfig, DigitallySignedStruct, SignatureScheme};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::sync::watch;
use tokio::time::{interval, timeout};
use tokio_rustls::TlsConnector;

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
        | HealthCheck::HttpsGet { common, .. }
        | HealthCheck::UdpDnsQuery { common, .. }
        | HealthCheck::UdpNtpQuery { common } => common.clone(),
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
        } => probe_http(addr, path, *expect_status, to).await,
        HealthCheck::HttpsGet {
            path,
            expect_status,
            sni,
            ..
        } => probe_https(addr, path, *expect_status, sni.as_deref(), to).await,
        HealthCheck::UdpDnsQuery { query, .. } => probe_udp_dns(addr, query, to).await,
        HealthCheck::UdpNtpQuery { .. } => probe_udp_ntp(addr, to).await,
    }
}

async fn probe_tcp(addr: SocketAddr, to: Duration) -> bool {
    timeout(to, tokio::net::TcpStream::connect(addr))
        .await
        .map(|r| r.is_ok())
        .unwrap_or(false)
}

fn validate_origin_form_path(path: &str) -> bool {
    // Defense in depth: catalog validation already rejects non-origin-form
    // and CR/LF in HC paths, but never interpolate an unvalidated path
    // into a request line.
    path.starts_with('/') && path.bytes().all(|b| (0x21..=0x7E).contains(&b))
}

fn build_get_request(path: &str, host: &str) -> String {
    format!("GET {path} HTTP/1.0\r\nHost: {host}\r\nConnection: close\r\n\r\n")
}

fn parse_status_line(buf: &[u8]) -> Option<u16> {
    let head = std::str::from_utf8(buf).ok()?;
    let mut parts = head.split_whitespace();
    let _http = parts.next()?;
    parts.next()?.parse::<u16>().ok()
}

async fn http_get_over<S>(stream: &mut S, req: &str, to: Duration) -> Option<u16>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    if timeout(to, stream.write_all(req.as_bytes())).await.is_err() {
        return None;
    }
    let mut buf = vec![0u8; 256];
    let n = match timeout(to, stream.read(&mut buf)).await {
        Ok(Ok(n)) => n,
        _ => return None,
    };
    parse_status_line(&buf[..n])
}

async fn probe_http(addr: SocketAddr, path: &str, expect_status: u16, to: Duration) -> bool {
    if !validate_origin_form_path(path) {
        tracing::warn!(?path, "refusing http hc with malformed path");
        return false;
    }
    let mut stream = match timeout(to, tokio::net::TcpStream::connect(addr)).await {
        Ok(Ok(s)) => s,
        _ => return false,
    };
    let req = build_get_request(path, &addr.ip().to_string());
    matches!(http_get_over(&mut stream, &req, to).await, Some(code) if code == expect_status)
}

/// HTTPS HC: real GET, like the `nat` plugin's keepalived `SSL_GET`.
///
/// TLS certificate validation is intentionally skipped: keepalived's
/// `SSL_GET` does not verify certs by default either, and operators
/// frequently point HC at backends with self-signed or internal-CA
/// certs. Adding a CA-store-driven verifier is left for a follow-up;
/// `verify_cert` is not on the wire today.
async fn probe_https(
    addr: SocketAddr,
    path: &str,
    expect_status: u16,
    sni: Option<&str>,
    to: Duration,
) -> bool {
    if !validate_origin_form_path(path) {
        tracing::warn!(?path, "refusing https hc with malformed path");
        return false;
    }
    let connector = match tls_connector() {
        Ok(c) => c,
        Err(err) => {
            tracing::warn!(?err, "rustls connector init failed");
            return false;
        }
    };
    let server_name = match server_name_for(addr.ip(), sni) {
        Ok(n) => n,
        Err(err) => {
            tracing::warn!(?err, ?sni, "invalid SNI for https hc");
            return false;
        }
    };
    let host_header = sni.map(str::to_owned).unwrap_or_else(|| addr.ip().to_string());
    let tcp = match timeout(to, tokio::net::TcpStream::connect(addr)).await {
        Ok(Ok(s)) => s,
        _ => return false,
    };
    let mut tls = match timeout(to, connector.connect(server_name, tcp)).await {
        Ok(Ok(s)) => s,
        _ => return false,
    };
    let req = build_get_request(path, &host_header);
    let status = http_get_over(&mut tls, &req, to).await;
    // Best-effort close; TLS shutdown failures don't affect HC verdict.
    let _ = tls.shutdown().await;
    matches!(status, Some(code) if code == expect_status)
}

fn server_name_for(addr: IpAddr, sni: Option<&str>) -> Result<ServerName<'static>, String> {
    if let Some(name) = sni {
        return ServerName::try_from(name.to_owned()).map_err(|e| e.to_string());
    }
    Ok(ServerName::IpAddress(addr.into()))
}

#[derive(Debug)]
struct NoVerifier;

impl ServerCertVerifier for NoVerifier {
    fn verify_server_cert(
        &self,
        _: &CertificateDer<'_>,
        _: &[CertificateDer<'_>],
        _: &ServerName<'_>,
        _: &[u8],
        _: UnixTime,
    ) -> Result<ServerCertVerified, rustls::Error> {
        Ok(ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        _: &[u8],
        _: &CertificateDer<'_>,
        _: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        Ok(HandshakeSignatureValid::assertion())
    }

    fn verify_tls13_signature(
        &self,
        _: &[u8],
        _: &CertificateDer<'_>,
        _: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        Ok(HandshakeSignatureValid::assertion())
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        vec![
            SignatureScheme::RSA_PKCS1_SHA1,
            SignatureScheme::ECDSA_SHA1_Legacy,
            SignatureScheme::RSA_PKCS1_SHA256,
            SignatureScheme::ECDSA_NISTP256_SHA256,
            SignatureScheme::RSA_PKCS1_SHA384,
            SignatureScheme::ECDSA_NISTP384_SHA384,
            SignatureScheme::RSA_PKCS1_SHA512,
            SignatureScheme::ECDSA_NISTP521_SHA512,
            SignatureScheme::RSA_PSS_SHA256,
            SignatureScheme::RSA_PSS_SHA384,
            SignatureScheme::RSA_PSS_SHA512,
            SignatureScheme::ED25519,
        ]
    }
}

fn tls_connector() -> Result<TlsConnector, String> {
    static INSTALL: Once = Once::new();
    INSTALL.call_once(|| {
        let _ = rustls::crypto::ring::default_provider().install_default();
    });
    let cfg = ClientConfig::builder()
        .dangerous()
        .with_custom_certificate_verifier(Arc::new(NoVerifier))
        .with_no_client_auth();
    Ok(TlsConnector::from(Arc::new(cfg)))
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

async fn probe_udp_ntp(addr: SocketAddr, to: Duration) -> bool {
    // SNTPv4 client request (RFC 4330): 48-byte packet, LI=0, VN=4,
    // Mode=3 (client). Everything else zeroed; servers don't require
    // anything else for a basic time response. We don't fill the
    // transmit timestamp because we never compute offset — we only
    // care that the server replies with mode=4 (server) and a
    // synchronized stratum.
    let mut req = [0u8; 48];
    req[0] = 0b00_100_011; // LI=0, VN=4, Mode=3

    let bind_addr = if addr.is_ipv6() { "[::]:0" } else { "0.0.0.0:0" };
    let socket = match tokio::net::UdpSocket::bind(bind_addr).await {
        Ok(s) => s,
        Err(_) => return false,
    };
    if timeout(to, socket.send_to(&req, addr)).await.is_err() {
        return false;
    }
    let mut buf = [0u8; 48];
    let n = match timeout(to, socket.recv(&mut buf)).await {
        Ok(Ok(n)) => n,
        _ => return false,
    };
    if n < 48 {
        return false;
    }
    // First byte: LI(2) | VN(3) | Mode(3). Mode must be 4 (server).
    let mode = buf[0] & 0b0000_0111;
    if mode != 4 {
        return false;
    }
    // LI=3 means "alarm condition (unsynchronized)" — reject.
    let li = (buf[0] & 0b1100_0000) >> 6;
    if li == 3 {
        return false;
    }
    // Stratum 0 is "kiss-of-death" (per RFC 4330 §5); 16 is
    // "unsynchronized." Either disqualifies the server.
    let stratum = buf[1];
    if stratum == 0 || stratum >= 16 {
        return false;
    }
    // Transmit timestamp must be non-zero on a real reply.
    if buf[40..48] == [0u8; 8] {
        return false;
    }
    true
}

#[cfg(test)]
mod http_tests {
    use super::*;
    use tokio::io::AsyncWriteExt;
    use tokio::net::TcpListener;

    /// Spawn a one-shot fake HTTP server that replies with the given
    /// status line (and an empty body); returns the bound address.
    async fn spawn_fake_http(status_line: &'static str) -> SocketAddr {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            let (mut sock, _) = listener.accept().await.unwrap();
            // Drain the request — we don't validate it here, the
            // request-line shape is covered by the build_get_request
            // unit test.
            let mut tmp = [0u8; 256];
            let _ = sock.read(&mut tmp).await;
            let resp = format!("{status_line}\r\nConnection: close\r\n\r\n");
            let _ = sock.write_all(resp.as_bytes()).await;
            let _ = sock.shutdown().await;
        });
        addr
    }

    #[tokio::test]
    async fn accepts_matching_status() {
        let addr = spawn_fake_http("HTTP/1.1 200 OK").await;
        assert!(probe_http(addr, "/", 200, Duration::from_secs(2)).await);
    }

    #[tokio::test]
    async fn rejects_mismatched_status() {
        let addr = spawn_fake_http("HTTP/1.1 503 Service Unavailable").await;
        assert!(!probe_http(addr, "/", 200, Duration::from_secs(2)).await);
    }

    #[tokio::test]
    async fn rejects_malformed_path() {
        // Path containing CR — defense in depth. We never reach the
        // socket so the address is unused.
        let addr: SocketAddr = "127.0.0.1:1".parse().unwrap();
        assert!(!probe_http(addr, "/foo\r\nX:y", 200, Duration::from_secs(1)).await);
    }

    #[test]
    fn parses_status_line() {
        assert_eq!(parse_status_line(b"HTTP/1.1 200 OK\r\n"), Some(200));
        assert_eq!(parse_status_line(b"HTTP/1.0 404 Not Found\r\n"), Some(404));
        assert_eq!(parse_status_line(b""), None);
        assert_eq!(parse_status_line(b"garbage"), None);
    }
}

#[cfg(test)]
mod ntp_tests {
    use super::*;

    /// Spawn a one-shot fake NTP server that returns `reply_first_byte`
    /// + `reply_stratum` + the supplied transmit-timestamp bytes.
    /// Returns the bound address.
    async fn spawn_fake_ntp(
        reply_first_byte: u8,
        reply_stratum: u8,
        reply_xmit: [u8; 8],
    ) -> SocketAddr {
        let server = tokio::net::UdpSocket::bind("127.0.0.1:0").await.unwrap();
        let addr = server.local_addr().unwrap();
        tokio::spawn(async move {
            let mut buf = [0u8; 48];
            let (_, peer) = server.recv_from(&mut buf).await.unwrap();
            // First byte must look like a v4 client request (mode=3).
            assert_eq!(buf[0] & 0b0000_0111, 3);
            let mut reply = [0u8; 48];
            reply[0] = reply_first_byte;
            reply[1] = reply_stratum;
            reply[40..48].copy_from_slice(&reply_xmit);
            let _ = server.send_to(&reply, peer).await;
        });
        addr
    }

    #[tokio::test]
    async fn accepts_well_formed_synchronized_reply() {
        let addr = spawn_fake_ntp(
            0b00_100_100, // LI=0, VN=4, Mode=4 (server)
            2,
            [0, 0, 0, 0, 0, 0, 0, 1],
        )
        .await;
        assert!(probe_udp_ntp(addr, Duration::from_secs(2)).await);
    }

    #[tokio::test]
    async fn rejects_wrong_mode() {
        let addr = spawn_fake_ntp(
            0b00_100_011, // Mode=3, not server
            2,
            [0, 0, 0, 0, 0, 0, 0, 1],
        )
        .await;
        assert!(!probe_udp_ntp(addr, Duration::from_secs(2)).await);
    }

    #[tokio::test]
    async fn rejects_kiss_of_death_stratum_0() {
        let addr = spawn_fake_ntp(0b00_100_100, 0, [0, 0, 0, 0, 0, 0, 0, 1]).await;
        assert!(!probe_udp_ntp(addr, Duration::from_secs(2)).await);
    }

    #[tokio::test]
    async fn rejects_unsynchronized_stratum_16() {
        let addr = spawn_fake_ntp(0b00_100_100, 16, [0, 0, 0, 0, 0, 0, 0, 1]).await;
        assert!(!probe_udp_ntp(addr, Duration::from_secs(2)).await);
    }

    #[tokio::test]
    async fn rejects_zero_transmit_timestamp() {
        let addr = spawn_fake_ntp(0b00_100_100, 2, [0u8; 8]).await;
        assert!(!probe_udp_ntp(addr, Duration::from_secs(2)).await);
    }

    #[tokio::test]
    async fn rejects_alarm_li_3() {
        let addr = spawn_fake_ntp(
            0b11_100_100, // LI=3 (alarm), Mode=4
            2,
            [0, 0, 0, 0, 0, 0, 0, 1],
        )
        .await;
        assert!(!probe_udp_ntp(addr, Duration::from_secs(2)).await);
    }

    #[tokio::test]
    async fn times_out_on_silent_server() {
        let server = tokio::net::UdpSocket::bind("127.0.0.1:0").await.unwrap();
        let addr = server.local_addr().unwrap();
        // No spawned task — nobody answers.
        assert!(!probe_udp_ntp(addr, Duration::from_millis(200)).await);
    }
}
