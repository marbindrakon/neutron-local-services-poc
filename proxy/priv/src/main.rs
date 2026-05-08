//! `nls-proxy-priv` — privileged helper for `nls-proxy`.
//!
//! Trust boundary. Holds `CAP_SYS_ADMIN` (for `setns`) and
//! `CAP_NET_BIND_SERVICE` (for low-port bind). Exposes a single RPC —
//! `BindListener` — over a unix socket. The worker (`nls-proxy`)
//! holds zero capabilities and runs all proxy logic.
//!
//! Peer authorization. The unix socket is `0660 root:nls-admin` so
//! filesystem mode keeps unrelated users out, but **socket mode is
//! hardening, not authz**. Every accept does an `SO_PEERCRED` check
//! and refuses any peer that isn't a member of the configured peer
//! group (default `nls-admin`). Root peers are refused outright —
//! the priv helper has no legitimate root callers.
//!
//! Catalog-driven authorization. Every `BindListener` re-reads the
//! agent-signed catalog (`catalog.json` + `hmac.key`, same files the
//! worker watches) and refuses any `(net_id, vip, port, proto)`
//! tuple that isn't an entry. The post-`setns()` nonce check uses the
//! catalog entry's own `nonce` and `nonce_path` — the worker doesn't
//! influence either, so a compromised worker cannot widen what gets
//! bound or where the recycle-check file lives.
//!
//! Each `BindListener` request runs on a freshly spawned thread that
//! `setns()`s into the requested netns, performs `socket → bind →
//! listen`, sends the bound fd back, and exits. The thread never
//! returns to the caller's netns; it dies. The main thread (and every
//! other thread in the process) stays in the host netns for its
//! lifetime.

use std::os::fd::{AsFd, AsRawFd, OwnedFd};
use std::path::PathBuf;
use std::sync::Arc;
use std::thread;

use anyhow::{anyhow, bail, Context, Result};

mod catalog;
mod netns;
mod nonce;
mod rpc;

const DEFAULT_SOCKET_PATH: &str = "/var/run/neutron-local-services/_proxy/priv.sock";
const DEFAULT_PEER_GROUP: &str = "nls-admin";
const DEFAULT_CATALOG_PATH: &str = "/var/lib/neutron-local-services/_proxy/catalog.json";
const DEFAULT_HMAC_KEY_PATH: &str = "/var/lib/neutron-local-services/_proxy/hmac.key";
const DEFAULT_NONCE_DIR: &str = "/var/lib/neutron-local-services/_proxy/nonces";

/// Read-only refs held for the priv process's lifetime and consulted on
/// every `BindListener`. Cheap to clone (Arc / OwnedFd-as-Arc), so we
/// hand a clone to each connection thread.
#[derive(Clone)]
struct PrivContext {
    catalog_path: Arc<PathBuf>,
    hmac_key: Arc<Vec<u8>>,
    nonce_dir_fd: Arc<OwnedFd>,
}

fn main() -> Result<()> {
    init_tracing();

    let socket_path = std::env::var("NLS_PROXY_PRIV_SOCK")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from(DEFAULT_SOCKET_PATH));

    let peer_group = std::env::var("NLS_PROXY_PRIV_PEER_GROUP")
        .unwrap_or_else(|_| DEFAULT_PEER_GROUP.to_owned());
    let peer_gid = resolve_group(&peer_group)
        .with_context(|| format!("resolve gid for peer group {peer_group:?}"))?;

    // Catalog / HMAC key / nonce dir come from the same agent-managed
    // state directory the worker reads (see proxy/worker/src/main.rs).
    // Defaults match; env vars allow overrides for tests.
    let catalog_path = std::env::var("NLS_PROXY_CATALOG")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from(DEFAULT_CATALOG_PATH));
    let hmac_key_path = std::env::var("NLS_PROXY_HMAC_KEY")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from(DEFAULT_HMAC_KEY_PATH));
    let nonce_dir = std::env::var("NLS_PROXY_NONCE_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from(DEFAULT_NONCE_DIR));

    let hmac_key = std::fs::read(&hmac_key_path)
        .with_context(|| format!("read HMAC key {}", hmac_key_path.display()))?;
    if hmac_key.is_empty() {
        bail!("HMAC key file {} is empty", hmac_key_path.display());
    }
    let nonce_dir_fd = nonce::open_nonce_dir(&nonce_dir)
        .with_context(|| format!("open nonce dir {}", nonce_dir.display()))?;
    let priv_ctx = PrivContext {
        catalog_path: Arc::new(catalog_path),
        hmac_key: Arc::new(hmac_key),
        nonce_dir_fd: Arc::new(nonce_dir_fd),
    };
    tracing::info!(
        catalog = %priv_ctx.catalog_path.display(),
        nonce_dir = %nonce_dir.display(),
        hmac_key_bytes = priv_ctx.hmac_key.len(),
        "loaded BindListener authorization context",
    );

    if let Some(parent) = socket_path.parent() {
        std::fs::create_dir_all(parent)
            .with_context(|| format!("create_dir_all({})", parent.display()))?;
        // Make the runtime dir group-owned by the peer group so the
        // worker (a member of that group) can both traverse the
        // directory and discover the socket. Doing this from the
        // binary instead of relying on a systemd ExecStartPost is
        // more reliable: services with ProtectSystem=strict can have
        // a private mount view of /run for which an external chgrp
        // run by systemd doesn't propagate to peers' views.
        chgrp(parent, peer_gid)?;
        chmod_2770(parent)?;
    }
    if socket_path.exists() {
        std::fs::remove_file(&socket_path).ok();
    }

    let listener = std::os::unix::net::UnixListener::bind(&socket_path)
        .with_context(|| format!("bind({})", socket_path.display()))?;
    chgrp(&socket_path, peer_gid)?;
    chmod_0660(&socket_path)?;

    tracing::info!(
        path = %socket_path.display(),
        peer_group = %peer_group,
        peer_gid = peer_gid,
        "nls-proxy-priv listening"
    );

    for client in listener.incoming() {
        match client {
            Ok(stream) => {
                if !peer_authorized(&stream, peer_gid) {
                    // peer_authorized logs the reason; just drop.
                    continue;
                }
                let conn_ctx = priv_ctx.clone();
                thread::Builder::new()
                    .name("priv-conn".into())
                    .spawn(move || {
                        if let Err(e) = handle_connection(stream, &conn_ctx) {
                            tracing::warn!(error = %e, "client connection ended with error");
                        }
                    })
                    .context("spawn connection thread")?;
            }
            Err(e) => {
                tracing::warn!(error = %e, "accept failed");
            }
        }
    }
    Ok(())
}

fn resolve_group(name: &str) -> Result<libc::gid_t> {
    use std::ffi::CString;
    let cname = CString::new(name).context("group name has interior NUL")?;
    // SAFETY: cname is NUL-terminated; getgrnam returns a static buffer
    // pointer that we use only to read gr_gid before any other libc call.
    let entry = unsafe { libc::getgrnam(cname.as_ptr()) };
    if entry.is_null() {
        bail!("group {name:?} not found (errno={})", std::io::Error::last_os_error());
    }
    Ok(unsafe { (*entry).gr_gid })
}

fn chgrp(path: &std::path::Path, gid: libc::gid_t) -> Result<()> {
    use std::os::unix::ffi::OsStrExt;
    let cpath = std::ffi::CString::new(path.as_os_str().as_bytes())
        .context("socket path has interior NUL")?;
    // SAFETY: cpath is NUL-terminated; uid -1 leaves owner unchanged.
    let r = unsafe { libc::chown(cpath.as_ptr(), libc::uid_t::MAX, gid) };
    if r != 0 {
        return Err(std::io::Error::last_os_error()).context(format!(
            "chgrp {} -> gid {}",
            path.display(),
            gid
        ));
    }
    Ok(())
}

/// SO_PEERCRED + supplementary-group check. The peer must be a member
/// of the configured peer group (default `nls-admin`) — either as
/// primary gid or via /proc/<pid>/status's `Groups:` line. Refuses
/// peers running as uid 0 because the priv helper itself is the only
/// expected root caller and it doesn't talk to its own socket.
fn peer_authorized(stream: &std::os::unix::net::UnixStream, allowed_gid: libc::gid_t) -> bool {
    use nix::sys::socket::{getsockopt, sockopt::PeerCredentials};
    let cred = match getsockopt(stream, PeerCredentials) {
        Ok(c) => c,
        Err(e) => {
            tracing::warn!(error = %e, "SO_PEERCRED failed; rejecting peer");
            return false;
        }
    };
    let peer_uid = cred.uid();
    let peer_gid = cred.gid();
    let peer_pid = cred.pid();
    if peer_uid == 0 {
        tracing::warn!(peer_pid, "rejecting privileged peer on priv socket");
        return false;
    }
    if peer_gid == allowed_gid {
        return true;
    }
    if peer_in_supplementary_group(peer_pid, allowed_gid) {
        return true;
    }
    tracing::warn!(
        peer_uid,
        peer_gid,
        peer_pid,
        allowed_gid,
        "peer not a member of allowed group; rejecting"
    );
    false
}

fn peer_in_supplementary_group(pid: i32, gid: libc::gid_t) -> bool {
    let path = format!("/proc/{pid}/status");
    let body = match std::fs::read_to_string(&path) {
        Ok(s) => s,
        Err(e) => {
            tracing::warn!(error = %e, %path, "read proc status");
            return false;
        }
    };
    for line in body.lines() {
        if let Some(rest) = line.strip_prefix("Groups:") {
            return rest
                .split_whitespace()
                .filter_map(|tok| tok.parse::<libc::gid_t>().ok())
                .any(|g| g == gid);
        }
    }
    false
}

fn init_tracing() {
    use tracing_subscriber::{fmt, EnvFilter};
    let filter = EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| EnvFilter::new("info,nls_proxy_priv=info"));
    fmt().with_env_filter(filter).with_target(true).init();
}

fn chmod_0660(path: &std::path::Path) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    let perms = std::fs::Permissions::from_mode(0o660);
    std::fs::set_permissions(path, perms)
        .with_context(|| format!("chmod 0660 {}", path.display()))
}

fn chmod_2770(path: &std::path::Path) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    // Setgid + 0770: setgid makes new files inside inherit the dir's
    // group (nls-admin), so sockets the worker creates land
    // group-readable by the agent without each binary having to chgrp.
    let perms = std::fs::Permissions::from_mode(0o2770);
    std::fs::set_permissions(path, perms)
        .with_context(|| format!("chmod 2770 {}", path.display()))
}

fn handle_connection(
    stream: std::os::unix::net::UnixStream,
    ctx: &PrivContext,
) -> Result<()> {
    loop {
        match rpc::recv_request(&stream) {
            Ok((req, fds)) => {
                let response = dispatch(req, fds, ctx);
                rpc::send_response(&stream, response)?;
            }
            Err(rpc::RpcError::Eof) => return Ok(()),
            Err(e) => {
                tracing::warn!(error = %e, "rpc recv failed; closing connection");
                return Ok(());
            }
        }
    }
}

fn dispatch(
    req: nls_proxy_wire::Request,
    in_fds: Vec<OwnedFd>,
    ctx: &PrivContext,
) -> rpc::Outgoing {
    use nls_proxy_wire::Request;
    match req {
        Request::BindListener {
            net_id,
            vip,
            port,
            proto,
        } => {
            let netns_fd = match exactly_one_netns_fd(in_fds) {
                Ok(fd) => fd,
                Err(e) => return rpc::Outgoing::err(format!("BindListener: {e:#}")),
            };
            match handle_bind_listener(netns_fd, &net_id, vip, port, proto, ctx) {
                Ok(fd) => rpc::Outgoing::ok_with_fd(nls_proxy_wire::Response::BoundListener, fd),
                Err(e) => rpc::Outgoing::err(format!(
                    "BindListener(net_id={net_id}, {vip}:{port}/{proto:?}): {e:#}"
                )),
            }
        }
    }
}

/// Caller must hand us **exactly one** fd that refers to a network
/// namespace. Wrong count → reject and let the surplus drop. Wrong
/// fd type → reject before anyone calls `setns` on it.
fn exactly_one_netns_fd(fds: Vec<OwnedFd>) -> Result<OwnedFd> {
    let n = fds.len();
    if n != 1 {
        bail!("BindListener: expected exactly 1 fd in SCM_RIGHTS, got {n}");
    }
    let fd = fds.into_iter().next().expect("len checked");
    netns::assert_is_netns_fd(fd.as_fd())?;
    Ok(fd)
}

fn handle_bind_listener(
    netns_fd: OwnedFd,
    net_id: &str,
    vip: std::net::IpAddr,
    port: u16,
    proto: nls_proxy_wire::Proto,
    ctx: &PrivContext,
) -> Result<OwnedFd> {
    // Step 1: load + verify the agent-signed catalog. The HMAC envelope
    // is the trust root for everything below — if it doesn't verify,
    // we refuse before any netns work.
    let cat = catalog::load_and_verify(&ctx.catalog_path, &ctx.hmac_key)
        .context("load + verify catalog")?;

    // Step 2: authorize the (net_id, vip, port, proto) tuple against
    // the catalog. Without this check, a compromised worker holding a
    // tenant netns fd could ask us to bind on any (vip, port, proto)
    // it likes inside that netns. Refusing here pins the worker's
    // capability surface to exactly what the operator authorized.
    let entry = match catalog::lookup_entry(&cat, net_id, vip, port, proto) {
        Some(e) => e,
        None => bail!(
            "no catalog entry for (net_id={net_id}, vip={vip}, port={port}, proto={proto:?}); refusing"
        ),
    };

    // Step 3: pull the canonical nonce + nonce filename from the
    // catalog. The worker doesn't get to influence either.
    let nonce = entry.nonce.clone();
    let nonce_basename = std::path::Path::new(&entry.nonce_path)
        .file_name()
        .ok_or_else(|| anyhow!(
            "catalog entry's nonce_path has no filename component: {:?}",
            entry.nonce_path
        ))?
        .to_owned();

    let nonce_dir_fd = Arc::clone(&ctx.nonce_dir_fd);

    let join = thread::Builder::new()
        .name("bind-helper".into())
        .spawn(move || -> Result<OwnedFd> {
            netns::setns_to(netns_fd.as_fd())
                .context("setns into tenant netns")?;
            // setns(CLONE_NEWNET) leaves the mount namespace alone, so
            // nonce_dir_fd (opened in priv's mount ns) is still valid
            // here. The openat2 + RESOLVE_BENEATH on nonce_dir_fd is
            // belt-and-suspenders against a tainted nonce_path string.
            nonce::verify_nonce_at(nonce_dir_fd.as_fd(), &nonce_basename, &nonce)
                .context("verify tenant nonce file")?;
            let fd = bind_listener_in_current_netns(vip, port, proto)
                .context("bind listener in tenant netns")?;
            Ok(fd)
        })
        .context("spawn bind-helper thread")?;

    join.join().map_err(|_| anyhow!("bind-helper thread panicked"))?
}

fn bind_listener_in_current_netns(
    vip: std::net::IpAddr,
    port: u16,
    proto: nls_proxy_wire::Proto,
) -> Result<OwnedFd> {
    let addr = std::net::SocketAddr::new(vip, port);
    let fd: OwnedFd = match proto {
        nls_proxy_wire::Proto::Tcp => std::net::TcpListener::bind(addr)
            .with_context(|| format!("TCP bind {addr}"))?
            .into(),
        nls_proxy_wire::Proto::Udp => std::net::UdpSocket::bind(addr)
            .with_context(|| format!("UDP bind {addr}"))?
            .into(),
    };
    if vip.is_ipv6() {
        // Force v6only so v4-mapped addresses don't sneak in.
        set_ipv6_v6only(fd.as_raw_fd())?;
    }
    Ok(fd)
}

fn set_ipv6_v6only(fd: std::os::fd::RawFd) -> Result<()> {
    let on: libc::c_int = 1;
    // SAFETY: setsockopt with a valid fd, level/option constants from
    // libc, and a pointer+length pair backed by a stack-local int.
    let r = unsafe {
        libc::setsockopt(
            fd,
            libc::IPPROTO_IPV6,
            libc::IPV6_V6ONLY,
            &on as *const _ as *const _,
            std::mem::size_of_val(&on) as libc::socklen_t,
        )
    };
    if r != 0 {
        return Err(std::io::Error::last_os_error()).context("setsockopt IPV6_V6ONLY");
    }
    Ok(())
}
