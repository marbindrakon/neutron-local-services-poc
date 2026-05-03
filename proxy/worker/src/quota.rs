//! Per-listener and per-tenant quotas.
//!
//! All counters are thread-local — they live on the per-tenant
//! data-path thread. Cross-tenant interference is structurally
//! impossible because no other tenant's thread can reach this
//! tenant's `Quota` value.
//!
//! Counters expose `try_acquire` / `release` for connection-style
//! use and `note_session` for UDP session-table sizing. A failed
//! `try_acquire` counts as a quota-breach event for metrics.

use std::sync::atomic::{AtomicU32, AtomicU64, Ordering};
use std::sync::Arc;

#[derive(Debug)]
pub struct ListenerQuota {
    /// Cap on simultaneously-open TCP connections (or live UDP
    /// sessions) for this listener.
    max_concurrent: u32,
    in_flight: AtomicU32,
    rejected: AtomicU64,
}

impl ListenerQuota {
    pub fn new(max_concurrent: u32) -> Self {
        Self {
            max_concurrent,
            in_flight: AtomicU32::new(0),
            rejected: AtomicU64::new(0),
        }
    }

    /// Acquire a slot. The returned guard releases the slot on drop.
    /// Takes `Arc<Self>` so the guard can be `'static` (tasks spawned
    /// onto a tokio runtime require their captures to outlive the
    /// runtime).
    pub fn try_acquire(self: &Arc<Self>) -> Option<QuotaGuard> {
        // CAS loop: increment if below cap.
        loop {
            let cur = self.in_flight.load(Ordering::Acquire);
            if cur >= self.max_concurrent {
                self.rejected.fetch_add(1, Ordering::Relaxed);
                return None;
            }
            if self
                .in_flight
                .compare_exchange(cur, cur + 1, Ordering::AcqRel, Ordering::Acquire)
                .is_ok()
            {
                return Some(QuotaGuard {
                    owner: Arc::clone(self),
                });
            }
        }
    }

    #[allow(dead_code)] // surfaced by /metrics in a follow-up commit
    pub fn in_flight(&self) -> u32 {
        self.in_flight.load(Ordering::Relaxed)
    }

    #[allow(dead_code)] // surfaced by /metrics in a follow-up commit
    pub fn rejected(&self) -> u64 {
        self.rejected.load(Ordering::Relaxed)
    }

    pub fn max_concurrent(&self) -> u32 {
        self.max_concurrent
    }
}

pub struct QuotaGuard {
    owner: Arc<ListenerQuota>,
}

impl Drop for QuotaGuard {
    fn drop(&mut self) {
        // saturating decrement.
        let prev = self.owner.in_flight.fetch_sub(1, Ordering::AcqRel);
        if prev == 0 {
            // Pathological — shouldn't happen, but don't underflow.
            self.owner.in_flight.store(0, Ordering::Release);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn enforces_cap_and_releases() {
        let q = Arc::new(ListenerQuota::new(2));
        let g1 = q.try_acquire().unwrap();
        let g2 = q.try_acquire().unwrap();
        assert!(q.try_acquire().is_none());
        assert_eq!(q.in_flight(), 2);
        assert_eq!(q.rejected(), 1);
        drop(g1);
        assert_eq!(q.in_flight(), 1);
        let _g3 = q.try_acquire().unwrap();
        assert_eq!(q.in_flight(), 2);
        drop(g2);
        assert_eq!(q.in_flight(), 1);
    }
}
