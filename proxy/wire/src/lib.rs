//! Wire protocol shared between `nls-proxy` (worker, RPC client) and
//! `nls-proxy-priv` (privileged helper, RPC server).
//!
//! Frame: 4-byte big-endian length, then JSON body. Optional file
//! descriptors travel out-of-band via SCM_RIGHTS on the same unix-socket
//! message; the JSON body is authoritative about how many fds it expects.

use std::io::{self, Read, Write};
use std::net::IpAddr;

use serde::{Deserialize, Serialize};
use thiserror::Error;

pub mod catalog;

pub const PROTOCOL_VERSION: u32 = 1;
pub const MAX_FRAME_BYTES: usize = 64 * 1024;

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Hash)]
#[serde(rename_all = "lowercase")]
pub enum Proto {
    Tcp,
    Udp,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
pub enum Request {
    /// Bind a listener inside the netns whose fd is attached as the
    /// single SCM_RIGHTS payload of this request. Returns the bound
    /// listener fd via SCM_RIGHTS.
    ///
    /// Authorization. The priv helper looks up
    /// `(net_id, vip, port, proto)` in its own copy of the
    /// agent-signed catalog and refuses if no matching entry exists.
    /// The nonce string + nonce_path used for the post-`setns()`
    /// recycle check come from that catalog entry — the worker has no
    /// say in either, so a compromised worker cannot redirect the
    /// nonce read to an attacker-controlled path or skip the check.
    BindListener {
        net_id: String,
        vip: IpAddr,
        port: u16,
        proto: Proto,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "result", rename_all = "snake_case")]
pub enum Response {
    /// Returned for `BindListener`. Single fd in SCM_RIGHTS.
    BoundListener,
    /// Failure path. No fd attached.
    Error { msg: String },
}

/// Worker control socket: agent → worker. The agent sends netns
/// fds via SCM_RIGHTS so the worker can spawn or shut down the
/// per-tenant data-path thread for that net_id.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
pub enum ControlRequest {
    /// Register a tenant netns. The netns fd is attached as the
    /// single SCM_RIGHTS payload of this request.
    AddNetns { net_id: String },
    /// Tear down the per-tenant thread; the worker drops its
    /// listener fds and the netns fd.
    RemoveNetns { net_id: String },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "result", rename_all = "snake_case")]
pub enum ControlResponse {
    Ok,
    Error { msg: String },
}

#[derive(Debug, Error)]
pub enum WireError {
    #[error("io: {0}")]
    Io(#[from] io::Error),
    #[error("json: {0}")]
    Json(#[from] serde_json::Error),
    #[error("frame too large: {len} bytes (max {})", MAX_FRAME_BYTES)]
    FrameTooLarge { len: usize },
    #[error("short frame: wanted {wanted} got {got}")]
    ShortFrame { wanted: usize, got: usize },
}

/// Encode a value to a length-prefixed JSON frame on `w`.
pub fn write_frame<W: Write, T: Serialize>(w: &mut W, value: &T) -> Result<(), WireError> {
    let body = serde_json::to_vec(value)?;
    if body.len() > MAX_FRAME_BYTES {
        return Err(WireError::FrameTooLarge { len: body.len() });
    }
    let len = u32::try_from(body.len()).expect("checked above");
    w.write_all(&len.to_be_bytes())?;
    w.write_all(&body)?;
    Ok(())
}

/// Read a length-prefixed JSON frame from `r`.
pub fn read_frame<R: Read, T: for<'de> Deserialize<'de>>(r: &mut R) -> Result<T, WireError> {
    let mut len_buf = [0u8; 4];
    r.read_exact(&mut len_buf)?;
    let len = u32::from_be_bytes(len_buf) as usize;
    if len > MAX_FRAME_BYTES {
        return Err(WireError::FrameTooLarge { len });
    }
    let mut body = vec![0u8; len];
    r.read_exact(&mut body)?;
    let value = serde_json::from_slice(&body)?;
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn round_trip_bind_listener_v6() {
        let req = Request::BindListener {
            net_id: "11111111-1111-1111-1111-111111111111".into(),
            vip: "fe80::1".parse().unwrap(),
            port: 5353,
            proto: Proto::Udp,
        };
        let mut buf = Vec::new();
        write_frame(&mut buf, &req).unwrap();
        let mut cur = Cursor::new(buf);
        let got: Request = read_frame(&mut cur).unwrap();
        match got {
            Request::BindListener {
                net_id,
                vip,
                port,
                proto,
            } => {
                assert_eq!(net_id, "11111111-1111-1111-1111-111111111111");
                assert_eq!(vip, "fe80::1".parse::<IpAddr>().unwrap());
                assert_eq!(port, 5353);
                assert_eq!(proto, Proto::Udp);
            }
            _ => panic!("wrong variant"),
        }
    }

    #[test]
    fn frame_too_large_rejected() {
        let req = Request::BindListener {
            net_id: "x".repeat(MAX_FRAME_BYTES + 1),
            vip: "169.254.42.1".parse().unwrap(),
            port: 80,
            proto: Proto::Tcp,
        };
        let mut buf = Vec::new();
        let err = write_frame(&mut buf, &req).unwrap_err();
        assert!(matches!(err, WireError::FrameTooLarge { .. }));
    }

    #[test]
    fn response_serializes_with_tag() {
        let resp = Response::Error { msg: "bad".into() };
        let s = serde_json::to_string(&resp).unwrap();
        assert!(s.contains(r#""result":"error""#));
        assert!(s.contains(r#""msg":"bad""#));
    }
}
