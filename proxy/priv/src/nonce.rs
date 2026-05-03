//! Tenant-netns nonce verification (tenant-netns nonce verification belt-and-suspenders).
//!
//! The agent writes a random per-network nonce to a file when it
//! creates the tenant netns, and stores the nonce string in catalog
//! entries for that network. The priv helper reads the nonce file
//! after `setns()` and rejects the bind if the contents differ.
//!
//! This catches the agent-bug case where the wrong netns fd is
//! paired with the wrong catalog entry. The primary mitigation
//! against netns recycling is fd-handoff via SCM_RIGHTS — the nonce
//! is defense-in-depth.

use std::io::Read;
use std::os::fd::FromRawFd;
use std::os::unix::ffi::OsStrExt;

use anyhow::{bail, Context, Result};

const MAX_NONCE_BYTES: usize = 256;

pub fn verify_nonce(path: &str, expected: &str) -> Result<()> {
    let cpath = std::ffi::CString::new(std::path::Path::new(path).as_os_str().as_bytes())
        .context("nonce path contains NUL")?;
    // SAFETY: NUL-terminated path, fixed flag set. Fd is wrapped in
    // a File so it's closed on drop.
    let raw = unsafe {
        libc::open(
            cpath.as_ptr(),
            libc::O_RDONLY | libc::O_CLOEXEC | libc::O_NOFOLLOW,
        )
    };
    if raw < 0 {
        return Err(std::io::Error::last_os_error())
            .with_context(|| format!("open({path}, O_NOFOLLOW)"));
    }
    let mut file = unsafe { std::fs::File::from_raw_fd(raw) };
    let mut buf = vec![0u8; MAX_NONCE_BYTES];
    let n = file.read(&mut buf).context("read nonce file")?;
    let actual = std::str::from_utf8(&buf[..n])
        .context("nonce file is not UTF-8")?
        .trim();
    if actual != expected {
        bail!("nonce mismatch (expected {} bytes, got {} bytes)", expected.len(), actual.len());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn verifies_matching_nonce() {
        let dir = tempdir();
        let path = dir.path().join("nonce");
        let mut f = std::fs::File::create(&path).unwrap();
        writeln!(f, "deadbeef").unwrap();
        verify_nonce(path.to_str().unwrap(), "deadbeef").unwrap();
    }

    #[test]
    fn rejects_mismatch() {
        let dir = tempdir();
        let path = dir.path().join("nonce");
        std::fs::write(&path, b"actual").unwrap();
        let err = verify_nonce(path.to_str().unwrap(), "expected").unwrap_err();
        let msg = format!("{err:#}");
        assert!(msg.contains("mismatch"), "unexpected: {msg}");
    }

    #[test]
    fn rejects_missing() {
        let err = verify_nonce("/nonexistent/path/x", "x").unwrap_err();
        let msg = format!("{err:#}");
        assert!(msg.contains("open"), "unexpected: {msg}");
    }

    fn tempdir() -> TempDirHandle {
        let path = std::env::temp_dir().join(format!(
            "nls-priv-test-{}-{}",
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
    impl std::ops::Deref for TempDirHandle {
        type Target = std::path::Path;
        fn deref(&self) -> &Self::Target {
            &self.0
        }
    }
    impl TempDirHandle {
        fn path(&self) -> &std::path::Path {
            &self.0
        }
    }
}
