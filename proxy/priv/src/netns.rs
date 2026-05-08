//! Thin wrapper around `setns(2)` for switching the calling thread
//! into a target network namespace.
//!
//! The single `unsafe` block in this crate lives inside `nix::sched::setns`,
//! which is itself a safe wrapper. We re-export it through this
//! module so it's easy to grep for "every caller of setns" — there
//! should be exactly one (`handle_bind_listener` in `main.rs`).

use std::os::fd::{AsRawFd, BorrowedFd};

use anyhow::{bail, Context, Result};
use nix::sched::{setns, CloneFlags};

/// Move the calling thread into the netns referenced by `fd`.
///
/// Caller MUST be on a freshly spawned thread that exits when the
/// bind work is done. The thread never returns to its previous netns.
pub fn setns_to(fd: BorrowedFd<'_>) -> Result<()> {
    setns(fd, CloneFlags::CLONE_NEWNET).context("setns(CLONE_NEWNET)")
}

/// Confirm `fd` refers to a network namespace. Uses `NS_GET_NSTYPE`
/// from `linux/nsfs.h` (kernel ≥ 4.11). Returning Ok proves the kernel
/// itself classifies this fd as a netns; we don't have to trust the
/// caller's word that the SCM_RIGHTS payload was a netns fd.
pub fn assert_is_netns_fd(fd: BorrowedFd<'_>) -> Result<()> {
    // _IO(NSIO=0xb7, 3) — direction NONE, size 0.
    const NS_GET_NSTYPE: libc::c_ulong = (0xb7u32 << 8 | 3) as libc::c_ulong;
    // SAFETY: ioctl on a borrowed fd. NS_GET_NSTYPE takes no payload
    // and returns the namespace type as the syscall result.
    let r = unsafe { libc::ioctl(fd.as_raw_fd(), NS_GET_NSTYPE) };
    if r < 0 {
        return Err(std::io::Error::last_os_error())
            .context("ioctl(NS_GET_NSTYPE) — fd is not on nsfs");
    }
    if r as u32 != libc::CLONE_NEWNET as u32 {
        bail!("fd is a namespace but not a network namespace (type=0x{r:x})");
    }
    Ok(())
}
