//! Thin wrapper around `setns(2)` for switching the calling thread
//! into a target network namespace.
//!
//! The single `unsafe` block in this crate lives inside `nix::sched::setns`,
//! which is itself a safe wrapper. We re-export it through this
//! module so it's easy to grep for "every caller of setns" — there
//! should be exactly one (`handle_bind_listener` in `main.rs`).

use std::os::fd::BorrowedFd;

use anyhow::{Context, Result};
use nix::sched::{setns, CloneFlags};

/// Move the calling thread into the netns referenced by `fd`.
///
/// Caller MUST be on a freshly spawned thread that exits when the
/// bind work is done. The thread never returns to its previous netns.
pub fn setns_to(fd: BorrowedFd<'_>) -> Result<()> {
    setns(fd, CloneFlags::CLONE_NEWNET).context("setns(CLONE_NEWNET)")
}
