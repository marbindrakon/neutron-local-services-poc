//! Synchronous RPC client to `nls-proxy-priv`.
//!
//! Each `BindListener` call opens a fresh unix-socket connection,
//! sends one request with the netns fd attached via SCM_RIGHTS,
//! reads one response with the bound listener fd attached, then
//! drops the connection. Binds happen on catalog change, not on
//! the data path, so per-call connection setup is fine.
//!
//! All `unsafe` lives inside `nls-proxy-scm`; this module — and the
//! whole worker crate — stays `#![forbid(unsafe_code)]`.

use std::net::IpAddr;
use std::os::fd::{BorrowedFd, OwnedFd};
use std::os::unix::net::UnixStream;
use std::path::Path;

use anyhow::{anyhow, bail, Context, Result};
use nls_proxy_wire::{Proto, Request, Response, MAX_FRAME_BYTES};

pub fn bind_listener(
    socket_path: &Path,
    netns_fd: BorrowedFd<'_>,
    net_id: &str,
    vip: IpAddr,
    port: u16,
    proto: Proto,
) -> Result<OwnedFd> {
    let stream = UnixStream::connect(socket_path)
        .with_context(|| format!("connect priv helper at {}", socket_path.display()))?;
    let req = Request::BindListener {
        net_id: net_id.to_owned(),
        vip,
        port,
        proto,
    };
    send(&stream, &req, &[netns_fd])?;
    let (resp, mut fds) = recv(&stream)?;
    match resp {
        Response::BoundListener => fds
            .pop()
            .ok_or_else(|| anyhow!("priv helper returned BoundListener with no fd")),
        Response::Error { msg } => bail!("priv helper: {}", msg),
    }
}

fn send(stream: &UnixStream, req: &Request, fds: &[BorrowedFd<'_>]) -> Result<()> {
    let body = serde_json::to_vec(req).context("encode request")?;
    if body.len() > MAX_FRAME_BYTES {
        bail!("request body too large: {} bytes", body.len());
    }
    nls_proxy_scm::send_frame_with_fds(stream, &body, fds)
}

fn recv(stream: &UnixStream) -> Result<(Response, Vec<OwnedFd>)> {
    let len = nls_proxy_scm::read_length_prefix(stream)?
        .ok_or_else(|| anyhow!("priv helper closed connection"))?;
    if len > MAX_FRAME_BYTES {
        bail!("response too large: {} bytes", len);
    }
    let (body, fds) = nls_proxy_scm::recv_with_fds(stream, len)?;
    let resp: Response = serde_json::from_slice(&body).context("decode response")?;
    Ok((resp, fds))
}
