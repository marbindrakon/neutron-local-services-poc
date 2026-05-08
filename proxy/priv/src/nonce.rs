//! Tenant-netns nonce verification (recycle-detection belt-and-suspenders).
//!
//! The agent writes a random per-network nonce to a file inside a
//! configured nonce directory when it creates the tenant netns, and
//! stores both the nonce and its path in the catalog entries for that
//! network. After `setns()`, the priv helper:
//!
//! 1. Looks up the catalog entry by `(net_id, vip, port, proto)`.
//! 2. Extracts the basename of `entry.nonce_path`.
//! 3. Opens that basename via `openat2(nonce_dir_fd, basename,
//!    RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS)`. Even if the catalog
//!    payload is somehow tainted (HMAC bypass, agent bug), the open
//!    cannot escape the configured nonce directory — `..` and
//!    symlinks are rejected by the kernel.
//! 4. Compares the file contents to `entry.nonce`.
//!
//! `setns(CLONE_NEWNET)` does not change the mount namespace, so the
//! nonce-dir fd opened at startup in priv's mount view is still valid
//! after the bind-helper thread switches into the tenant netns. The
//! nonce check sees the agent's file, not anything inside the tenant
//! netns's mount view (tenants don't share priv's mount namespace).

use std::ffi::OsStr;
use std::io::Read;
use std::os::fd::{AsRawFd, BorrowedFd, FromRawFd, OwnedFd};

use anyhow::{bail, Context, Result};
use nix::fcntl::{openat2, OFlag, OpenHow, ResolveFlag};
use nix::sys::stat::Mode;

const MAX_NONCE_BYTES: usize = 256;

/// Open `nonce_dir` as an `O_PATH | O_DIRECTORY` fd for use as the
/// `dirfd` of subsequent `openat2` calls. Held for the lifetime of the
/// priv process. `O_PATH` is enough — we never read/write the directory
/// itself, we just need a stable handle to anchor `RESOLVE_BENEATH`.
pub fn open_nonce_dir(path: &std::path::Path) -> Result<OwnedFd> {
    use std::os::unix::ffi::OsStrExt;
    let cpath = std::ffi::CString::new(path.as_os_str().as_bytes())
        .context("nonce dir path contains NUL")?;
    // SAFETY: cpath is NUL-terminated; flags are well-formed.
    let raw = unsafe {
        libc::open(
            cpath.as_ptr(),
            libc::O_PATH | libc::O_DIRECTORY | libc::O_CLOEXEC,
        )
    };
    if raw < 0 {
        return Err(std::io::Error::last_os_error())
            .with_context(|| format!("open({}, O_PATH|O_DIRECTORY)", path.display()));
    }
    Ok(unsafe { OwnedFd::from_raw_fd(raw) })
}

/// Open `<nonce_dir>/<basename>` via `openat2` with `RESOLVE_BENEATH |
/// RESOLVE_NO_SYMLINKS`, read up to MAX_NONCE_BYTES, and verify the
/// trimmed contents equal `expected`.
///
/// `RESOLVE_BENEATH` makes the kernel reject any path that escapes the
/// directory referenced by `dirfd` (`..` segments, absolute paths, mount
/// crossings). `RESOLVE_NO_SYMLINKS` rejects symlinks in any path
/// component, not just the trailing one (`O_NOFOLLOW` only protects the
/// last component). Together these make the open robust against both a
/// malformed `nonce_path` in the catalog payload and any prior bug that
/// might have planted a symlink inside the nonce directory.
pub fn verify_nonce_at(
    nonce_dir_fd: BorrowedFd<'_>,
    basename: &OsStr,
    expected: &str,
) -> Result<()> {
    if basename.is_empty() {
        bail!("empty nonce filename");
    }
    let bytes = basename.as_encoded_bytes();
    if bytes.contains(&b'/') || bytes == b".." || bytes == b"." {
        bail!("invalid nonce filename: {:?}", basename);
    }

    let how = OpenHow::new()
        .flags(OFlag::O_RDONLY | OFlag::O_CLOEXEC | OFlag::O_NOFOLLOW)
        .mode(Mode::empty())
        .resolve(ResolveFlag::RESOLVE_BENEATH | ResolveFlag::RESOLVE_NO_SYMLINKS);

    let raw = openat2(nonce_dir_fd.as_raw_fd(), basename, how)
        .context("openat2 nonce file")?;
    // Wrap as OwnedFd → File so it closes on drop.
    let owned = unsafe { OwnedFd::from_raw_fd(raw) };
    let mut file = std::fs::File::from(owned);
    let mut buf = vec![0u8; MAX_NONCE_BYTES];
    let n = file.read(&mut buf).context("read nonce file")?;
    let actual = std::str::from_utf8(&buf[..n])
        .context("nonce file is not UTF-8")?
        .trim();
    if actual != expected {
        bail!(
            "nonce mismatch (expected {} bytes, got {} bytes)",
            expected.len(),
            actual.len()
        );
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::OsStr;
    use std::io::Write;
    use std::os::fd::AsFd;

    fn tempdir() -> TempDirHandle {
        let path = std::env::temp_dir().join(format!(
            "nls-priv-nonce-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0)
        ));
        std::fs::create_dir(&path).unwrap();
        TempDirHandle(path)
    }

    struct TempDirHandle(std::path::PathBuf);
    impl Drop for TempDirHandle {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }
    impl TempDirHandle {
        fn path(&self) -> &std::path::Path {
            &self.0
        }
    }

    #[test]
    fn verifies_matching_nonce() {
        let dir = tempdir();
        let path = dir.path().join("n");
        let mut f = std::fs::File::create(&path).unwrap();
        writeln!(f, "deadbeef").unwrap();
        let dirfd = open_nonce_dir(dir.path()).unwrap();
        verify_nonce_at(dirfd.as_fd(), OsStr::new("n"), "deadbeef").unwrap();
    }

    #[test]
    fn rejects_mismatch() {
        let dir = tempdir();
        let path = dir.path().join("n");
        std::fs::write(&path, b"actual").unwrap();
        let dirfd = open_nonce_dir(dir.path()).unwrap();
        let err = verify_nonce_at(dirfd.as_fd(), OsStr::new("n"), "expected").unwrap_err();
        assert!(format!("{err:#}").contains("mismatch"), "{err:#}");
    }

    #[test]
    fn rejects_path_traversal() {
        let dir = tempdir();
        // Set up: <tmp>/inner is the nonce dir; <tmp>/outside is the
        // attacker target. A nonce_path of "../outside" must NOT
        // succeed even though the file's contents would match.
        let inner = dir.path().join("inner");
        std::fs::create_dir(&inner).unwrap();
        std::fs::write(dir.path().join("outside"), b"the-nonce\n").unwrap();
        let dirfd = open_nonce_dir(&inner).unwrap();
        let err =
            verify_nonce_at(dirfd.as_fd(), OsStr::new("../outside"), "the-nonce").unwrap_err();
        let msg = format!("{err:#}");
        // Our pre-check fires (slash in basename); even if it didn't,
        // the kernel would reject the openat2 with RESOLVE_BENEATH.
        assert!(
            msg.contains("invalid nonce filename") || msg.contains("openat2"),
            "{msg}"
        );
    }

    #[test]
    fn rejects_symlink_to_outside() {
        let dir = tempdir();
        let inner = dir.path().join("inner");
        std::fs::create_dir(&inner).unwrap();
        std::fs::write(dir.path().join("outside"), b"the-nonce\n").unwrap();
        // Plant a symlink inside the nonce dir pointing outside.
        std::os::unix::fs::symlink(dir.path().join("outside"), inner.join("link")).unwrap();
        let dirfd = open_nonce_dir(&inner).unwrap();
        let err =
            verify_nonce_at(dirfd.as_fd(), OsStr::new("link"), "the-nonce").unwrap_err();
        assert!(format!("{err:#}").contains("openat2"), "{err:#}");
    }
}
