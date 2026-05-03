//! `nls-proxy-priv` — privileged helper for `nls-proxy`.
//!
//! Trust boundary. Holds `CAP_SYS_ADMIN` (for `setns`) and
//! `CAP_NET_BIND_SERVICE` (for low-port bind). Does nothing except
//! receive RPC requests on a unix socket and hand back file
//! descriptors over SCM_RIGHTS. The worker (`nls-proxy`) holds zero
//! capabilities and runs all proxy logic.
//!
//! Each `BindListener` request runs on a freshly spawned thread that
//! `setns()`s into the requested netns, performs `socket → bind →
//! listen`, sends the bound fd back, and exits. The thread never
//! returns to the caller's netns; it dies. The main thread (and every
//! other thread in the process) stays in the host netns for its
//! lifetime.

use std::os::fd::{AsFd, AsRawFd, OwnedFd};
use std::path::PathBuf;
use std::thread;

use anyhow::{anyhow, bail, Context, Result};

mod netns;
mod nonce;
mod rpc;

const DEFAULT_SOCKET_PATH: &str = "/var/run/neutron-local-services/_proxy/priv.sock";

fn main() -> Result<()> {
    init_tracing();

    let socket_path = std::env::var("NLS_PROXY_PRIV_SOCK")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from(DEFAULT_SOCKET_PATH));

    if let Some(parent) = socket_path.parent() {
        std::fs::create_dir_all(parent)
            .with_context(|| format!("create_dir_all({})", parent.display()))?;
    }
    if socket_path.exists() {
        std::fs::remove_file(&socket_path).ok();
    }

    let listener = std::os::unix::net::UnixListener::bind(&socket_path)
        .with_context(|| format!("bind({})", socket_path.display()))?;
    chmod_0600(&socket_path)?;

    tracing::info!(path = %socket_path.display(), "nls-proxy-priv listening");

    for client in listener.incoming() {
        match client {
            Ok(stream) => {
                thread::Builder::new()
                    .name("priv-conn".into())
                    .spawn(move || {
                        if let Err(e) = handle_connection(stream) {
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

fn init_tracing() {
    use tracing_subscriber::{fmt, EnvFilter};
    let filter = EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| EnvFilter::new("info,nls_proxy_priv=info"));
    fmt().with_env_filter(filter).with_target(true).init();
}

fn chmod_0600(path: &std::path::Path) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    let perms = std::fs::Permissions::from_mode(0o600);
    std::fs::set_permissions(path, perms)
        .with_context(|| format!("chmod 0600 {}", path.display()))
}

fn handle_connection(stream: std::os::unix::net::UnixStream) -> Result<()> {
    loop {
        match rpc::recv_request(&stream) {
            Ok((req, fds)) => {
                let response = dispatch(req, fds);
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

fn dispatch(req: nls_proxy_wire::Request, in_fds: Vec<OwnedFd>) -> rpc::Outgoing {
    use nls_proxy_wire::Request;
    match req {
        Request::OpenNetns { name } => match handle_open_netns(&name) {
            Ok(fd) => rpc::Outgoing::ok_with_fd(nls_proxy_wire::Response::OpenedNetns, fd),
            Err(e) => rpc::Outgoing::err(format!("OpenNetns({name}): {e:#}")),
        },
        Request::BindListener {
            nonce,
            nonce_path,
            vip,
            port,
            proto,
        } => {
            let netns_fd = match in_fds.into_iter().next() {
                Some(fd) => fd,
                None => return rpc::Outgoing::err("BindListener: missing netns fd".into()),
            };
            match handle_bind_listener(netns_fd, &nonce, &nonce_path, vip, port, proto) {
                Ok(fd) => rpc::Outgoing::ok_with_fd(nls_proxy_wire::Response::BoundListener, fd),
                Err(e) => rpc::Outgoing::err(format!("BindListener: {e:#}")),
            }
        }
    }
}

fn handle_open_netns(name: &str) -> Result<OwnedFd> {
    if !is_safe_netns_name(name) {
        bail!("unsafe netns name: {name:?}");
    }
    let path = format!("/run/netns/{name}");
    use std::os::fd::FromRawFd;
    use std::os::unix::ffi::OsStrExt;
    let cpath =
        std::ffi::CString::new(std::path::Path::new(&path).as_os_str().as_bytes()).unwrap();
    // SAFETY: we pass a NUL-terminated path and known-good flags. The
    // returned fd is owned and tracked by `OwnedFd`.
    let raw = unsafe {
        libc_open(
            cpath.as_ptr(),
            libc::O_RDONLY | libc::O_CLOEXEC | libc::O_NOFOLLOW,
        )
    };
    if raw < 0 {
        return Err(std::io::Error::last_os_error()).context(format!("open({path})"));
    }
    Ok(unsafe { OwnedFd::from_raw_fd(raw) })
}

fn is_safe_netns_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 64
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
}

fn handle_bind_listener(
    netns_fd: OwnedFd,
    nonce: &str,
    nonce_path: &str,
    vip: std::net::IpAddr,
    port: u16,
    proto: nls_proxy_wire::Proto,
) -> Result<OwnedFd> {
    let nonce = nonce.to_owned();
    let nonce_path = nonce_path.to_owned();

    let join = thread::Builder::new()
        .name("bind-helper".into())
        .spawn(move || -> Result<OwnedFd> {
            netns::setns_to(netns_fd.as_fd())
                .context("setns into tenant netns")?;
            nonce::verify_nonce(&nonce_path, &nonce)
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

unsafe fn libc_open(path: *const libc::c_char, flags: libc::c_int) -> libc::c_int {
    libc::open(path, flags)
}
