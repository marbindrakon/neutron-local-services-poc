//! Send/receive bytes plus file descriptors over a unix socket using
//! `SCM_RIGHTS`.
//!
//! Pulled into its own crate so callers (priv helper, worker RPC
//! client) get a single audited surface for the unsafe `OwnedFd`
//! handoff. The worker crate forbids unsafe project-wide; isolating
//! this here is what lets that lint stay tight.
//!
//! Frame format and types live in `nls-proxy-wire`. This crate is
//! purely about wire-level transport.

use std::io::{IoSlice, IoSliceMut};
use std::os::fd::{AsRawFd, BorrowedFd, FromRawFd, OwnedFd, RawFd};

use anyhow::{anyhow, bail, Context, Result};
use nix::sys::socket::{
    recvmsg, sendmsg, ControlMessage, ControlMessageOwned, MsgFlags,
};

const MAX_FDS: usize = 4;

/// Send a frame: 4-byte BE length, then `body_bytes`, with `fds`
/// attached via SCM_RIGHTS if non-empty.
///
/// **Wire layout matters.** The 4-byte length prefix goes out in
/// a *separate* `sendmsg` that carries no ancillary data. The body
/// goes out in a second `sendmsg` carrying the SCM_RIGHTS cmsg.
/// This split lets the receiver call `read_length_prefix` (which
/// refuses cmsgs) followed by `recv_with_fds` (which captures
/// them) without having to plumb cmsg state across the two reads.
/// Linux's unix(7) attaches a cmsg to the first byte of data in
/// its sendmsg call, so a single combined sendmsg would deliver
/// the cmsg with the length prefix and break the receiver split.
pub fn send_frame_with_fds<S: AsRawFd>(
    sock: &S,
    body_bytes: &[u8],
    fds: &[BorrowedFd<'_>],
) -> Result<()> {
    let len = u32::try_from(body_bytes.len()).context("body too large")?;
    let len_be = len.to_be_bytes();
    write_all_no_fds(sock, &len_be)?;
    write_all_with_fds_first(sock, body_bytes, fds)
}

fn write_all_no_fds<S: AsRawFd>(sock: &S, bytes: &[u8]) -> Result<()> {
    let mut sent = 0usize;
    while sent < bytes.len() {
        let iov = [IoSlice::new(&bytes[sent..])];
        let n = sendmsg::<()>(
            sock.as_raw_fd(),
            &iov,
            &[],
            MsgFlags::empty(),
            None,
        )
        .context("sendmsg")?;
        if n == 0 {
            bail!("sendmsg returned 0 (peer closed)");
        }
        sent += n;
    }
    Ok(())
}

fn write_all_with_fds_first<S: AsRawFd>(
    sock: &S,
    bytes: &[u8],
    fds: &[BorrowedFd<'_>],
) -> Result<()> {
    let raw_fds: Vec<RawFd> = fds.iter().map(|f| f.as_raw_fd()).collect();
    let cmsgs = if raw_fds.is_empty() {
        Vec::new()
    } else {
        vec![ControlMessage::ScmRights(&raw_fds)]
    };
    let mut sent = 0usize;
    while sent < bytes.len() {
        let iov = [IoSlice::new(&bytes[sent..])];
        let n = sendmsg::<()>(
            sock.as_raw_fd(),
            &iov,
            if sent == 0 { &cmsgs } else { &[] },
            MsgFlags::empty(),
            None,
        )
        .context("sendmsg")?;
        if n == 0 {
            bail!("sendmsg returned 0 (peer closed)");
        }
        sent += n;
    }
    Ok(())
}

/// Receive `body_len` body bytes plus any fds that arrive in
/// SCM_RIGHTS during the receive.
pub fn recv_with_fds<S: AsRawFd>(sock: &S, body_len: usize) -> Result<(Vec<u8>, Vec<OwnedFd>)> {
    let mut body = vec![0u8; body_len];
    let mut fds: Vec<OwnedFd> = Vec::new();
    let mut filled = 0usize;
    while filled < body_len {
        let mut iov = [IoSliceMut::new(&mut body[filled..])];
        let mut cmsg_buf = nix::cmsg_space!([RawFd; MAX_FDS]);
        let msg = recvmsg::<()>(
            sock.as_raw_fd(),
            &mut iov,
            Some(&mut cmsg_buf),
            MsgFlags::MSG_CMSG_CLOEXEC,
        )
        .context("recvmsg")?;
        if msg.bytes == 0 {
            bail!("recvmsg returned 0 (peer closed mid-frame)");
        }
        filled += msg.bytes;
        for cmsg in msg.cmsgs().context("decode cmsgs")? {
            if let ControlMessageOwned::ScmRights(rights) = cmsg {
                if fds.len() + rights.len() > MAX_FDS {
                    for fd in &rights {
                        // SAFETY: kernel just gave us this raw fd via
                        // SCM_RIGHTS; we own it and are dropping it.
                        unsafe { libc::close(*fd) };
                    }
                    bail!("too many fds in ancillary data");
                }
                for fd in rights {
                    // SAFETY: kernel just gave us a fresh fd via
                    // SCM_RIGHTS; no one else owns it.
                    fds.push(unsafe { OwnedFd::from_raw_fd(fd) });
                }
            }
        }
    }
    Ok((body, fds))
}

/// Read 4 big-endian length bytes from `sock`, returning `None` on
/// clean EOF before any byte arrives.
pub fn read_length_prefix<S: AsRawFd>(sock: &S) -> Result<Option<usize>> {
    let mut len_buf = [0u8; 4];
    let mut filled = 0usize;
    while filled < 4 {
        let mut iov = [IoSliceMut::new(&mut len_buf[filled..])];
        let mut cmsg_buf = nix::cmsg_space!([RawFd; MAX_FDS]);
        let msg = recvmsg::<()>(
            sock.as_raw_fd(),
            &mut iov,
            Some(&mut cmsg_buf),
            MsgFlags::MSG_CMSG_CLOEXEC,
        )
        .context("recvmsg(length prefix)")?;
        if msg.bytes == 0 {
            if filled == 0 {
                return Ok(None);
            }
            bail!("eof in length prefix after {filled} bytes");
        }
        filled += msg.bytes;
        for cmsg in msg.cmsgs().context("decode cmsgs")? {
            if let ControlMessageOwned::ScmRights(rights) = cmsg {
                for fd in rights {
                    // SAFETY: dropping a kernel-given fd we won't keep.
                    unsafe { libc::close(fd) };
                }
                return Err(anyhow!("unexpected fd in length-prefix message"));
            }
        }
    }
    Ok(Some(u32::from_be_bytes(len_buf) as usize))
}
