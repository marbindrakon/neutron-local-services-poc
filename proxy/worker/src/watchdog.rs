//! Per-thread netns inode watchdog.
//!
//! At startup we capture the inode of `/proc/self/ns/net` (host
//! root netns). Every second, walk every TID under
//! `/proc/self/task/` and stat its `ns/net` link; any mismatch
//! means a worker thread is no longer in the host netns. By design
//! that's impossible — the worker process never calls `setns()` —
//! so a mismatch is treated as a hard invariant violation and the
//! process exits.

use std::os::unix::fs::MetadataExt;
use std::time::Duration;

pub fn spawn(host_netns_inode: u64) -> std::thread::JoinHandle<()> {
    std::thread::Builder::new()
        .name("nls-watchdog".into())
        .spawn(move || run(host_netns_inode))
        .expect("spawn watchdog")
}

pub fn capture_host_netns_inode() -> std::io::Result<u64> {
    let meta = std::fs::metadata("/proc/self/ns/net")?;
    Ok(meta.ino())
}

fn run(host_netns_inode: u64) {
    loop {
        std::thread::sleep(Duration::from_secs(1));
        match check_once(host_netns_inode) {
            Ok(()) => {}
            Err(WatchdogError::Mismatch { tid, found }) => {
                tracing::error!(
                    tid,
                    found,
                    expected = host_netns_inode,
                    "watchdog: thread is in wrong netns; exiting"
                );
                std::process::exit(73);
            }
            Err(WatchdogError::Io(e)) => {
                tracing::warn!(error = %e, "watchdog: io error reading /proc/self/task; continuing");
            }
        }
    }
}

#[derive(Debug)]
enum WatchdogError {
    Io(std::io::Error),
    Mismatch { tid: String, found: u64 },
}

fn check_once(expected: u64) -> Result<(), WatchdogError> {
    let task_dir = std::fs::read_dir("/proc/self/task").map_err(WatchdogError::Io)?;
    for ent in task_dir {
        let ent = ent.map_err(WatchdogError::Io)?;
        let tid = ent.file_name().to_string_lossy().into_owned();
        let path = ent.path().join("ns").join("net");
        let meta = match std::fs::metadata(&path) {
            Ok(m) => m,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
            Err(e) => return Err(WatchdogError::Io(e)),
        };
        if meta.ino() != expected {
            return Err(WatchdogError::Mismatch {
                tid,
                found: meta.ino(),
            });
        }
    }
    Ok(())
}
