//! Server-side RPC: receive `Request` frames + ancillary fds, send
//! `Response` frames + at-most-one ancillary fd.
//!
//! Frame format and types live in `nls-proxy-wire`. This module just
//! wires the JSON encoder/decoder up to the SCM_RIGHTS plumbing in
//! `nls-proxy-scm`.

use std::os::fd::{AsFd, OwnedFd};
use std::os::unix::net::UnixStream;

use anyhow::Context;
use thiserror::Error;

use nls_proxy_wire::{Request, Response, MAX_FRAME_BYTES};

#[derive(Debug, Error)]
pub enum RpcError {
    #[error("eof")]
    Eof,
    #[error("frame too large: {0}")]
    FrameTooLarge(usize),
    #[error("io: {0}")]
    Io(#[from] anyhow::Error),
    #[error("json: {0}")]
    Json(#[from] serde_json::Error),
}

/// Receive one request and any fds attached to it.
pub fn recv_request(stream: &UnixStream) -> Result<(Request, Vec<OwnedFd>), RpcError> {
    let len = match nls_proxy_scm::read_length_prefix(stream).map_err(RpcError::Io)? {
        Some(len) => len,
        None => return Err(RpcError::Eof),
    };
    if len > MAX_FRAME_BYTES {
        return Err(RpcError::FrameTooLarge(len));
    }
    let (body, fds) = nls_proxy_scm::recv_with_fds(stream, len).map_err(RpcError::Io)?;
    let req: Request = serde_json::from_slice(&body)?;
    Ok((req, fds))
}

/// What the dispatcher returns: a Response to encode plus at most
/// one fd to attach via SCM_RIGHTS.
pub struct Outgoing {
    pub response: Response,
    pub fd: Option<OwnedFd>,
}

impl Outgoing {
    pub fn ok_with_fd(response: Response, fd: OwnedFd) -> Self {
        Self {
            response,
            fd: Some(fd),
        }
    }
    pub fn err(msg: String) -> Self {
        Self {
            response: Response::Error { msg },
            fd: None,
        }
    }
}

pub fn send_response(stream: &UnixStream, out: Outgoing) -> anyhow::Result<()> {
    let body = serde_json::to_vec(&out.response).context("encode response")?;
    if body.len() > MAX_FRAME_BYTES {
        anyhow::bail!("response body too large: {} bytes", body.len());
    }
    let fds_storage: Vec<OwnedFd> = out.fd.into_iter().collect();
    let fd_refs: Vec<std::os::fd::BorrowedFd<'_>> =
        fds_storage.iter().map(|f| f.as_fd()).collect();
    nls_proxy_scm::send_frame_with_fds(stream, &body, &fd_refs)
}
