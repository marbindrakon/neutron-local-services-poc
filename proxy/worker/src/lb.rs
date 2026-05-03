//! Load balancing for backend selection.
//!
//! v1 ships weighted round-robin (WRR). Least-conn is a stretch and
//! lives alongside a thread-local concurrent-conn counter passed in
//! via the selector closure.

use std::sync::atomic::{AtomicUsize, Ordering};

use crate::catalog::{Backend, LbAlgo, Entry};

/// Returns indices into `entry.backends` in WRR order, skipping
/// backends marked unhealthy. Caller passes `is_healthy` so the
/// selector stays decoupled from the HC subsystem.
pub struct Selector {
    cursor: AtomicUsize,
}

impl Selector {
    pub fn new() -> Self {
        Self {
            cursor: AtomicUsize::new(0),
        }
    }

    pub fn pick<'a>(
        &self,
        entry: &'a Entry,
        is_healthy: impl Fn(&Backend) -> bool,
    ) -> Option<&'a Backend> {
        match entry.lb_algo {
            LbAlgo::Wrr | LbAlgo::LeastConn => self.pick_wrr(entry, is_healthy),
        }
    }

    fn pick_wrr<'a>(
        &self,
        entry: &'a Entry,
        is_healthy: impl Fn(&Backend) -> bool,
    ) -> Option<&'a Backend> {
        // Build the weighted ring lazily per-pick. Cheap for the
        // small backend lists we expect (handful per service).
        let healthy: Vec<&Backend> = entry
            .backends
            .iter()
            .filter(|b| is_healthy(b))
            .collect();
        if healthy.is_empty() {
            return None;
        }
        let total_weight: u32 = healthy.iter().map(|b| b.weight.max(1)).sum();
        if total_weight == 0 {
            return None;
        }
        let cursor = self.cursor.fetch_add(1, Ordering::Relaxed);
        let target = (cursor as u32) % total_weight;
        let mut acc = 0u32;
        for b in &healthy {
            acc += b.weight.max(1);
            if target < acc {
                return Some(*b);
            }
        }
        healthy.last().copied()
    }
}

impl Default for Selector {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::catalog::{HcCommon, HealthCheck};
    use std::net::IpAddr;

    fn entry_with(backends: Vec<Backend>) -> Entry {
        Entry {
            net_id: "00000000-0000-0000-0000-000000000000".into(),
            service_id: "00000000-0000-0000-0000-000000000001".into(),
            nonce: "n".into(),
            nonce_path: "/tmp/n".into(),
            vip: "169.254.169.10".parse().unwrap(),
            port: 53,
            proto: nls_proxy_wire::Proto::Udp,
            backends,
            health_check: HealthCheck::TcpConnect {
                common: HcCommon {
                    interval_s: 1,
                    timeout_s: 1,
                    fail_after: 1,
                    rise_after: 1,
                },
            },
            lb_algo: LbAlgo::Wrr,
            max_concurrent: 100,
            max_session_idle_s: 60,
        }
    }

    fn b(addr: &str, weight: u32) -> Backend {
        Backend {
            addr: addr.parse::<IpAddr>().unwrap(),
            port: 53,
            weight,
        }
    }

    #[test]
    fn skips_unhealthy() {
        let entry = entry_with(vec![b("10.0.0.1", 1), b("10.0.0.2", 1)]);
        let sel = Selector::new();
        let dead: IpAddr = "10.0.0.1".parse().unwrap();
        for _ in 0..10 {
            let pick = sel.pick(&entry, |b| b.addr != dead).unwrap();
            assert_ne!(pick.addr, dead);
        }
    }

    #[test]
    fn weighted_roughly_proportional() {
        let entry = entry_with(vec![b("10.0.0.1", 1), b("10.0.0.2", 9)]);
        let sel = Selector::new();
        let mut count1 = 0;
        let mut count2 = 0;
        for _ in 0..1000 {
            let pick = sel.pick(&entry, |_| true).unwrap();
            if pick.addr == "10.0.0.1".parse::<IpAddr>().unwrap() {
                count1 += 1;
            } else {
                count2 += 1;
            }
        }
        // Expect ~10% / ~90%; allow generous slack.
        assert!(count1 < 200, "{count1} too high");
        assert!(count2 > 800, "{count2} too low");
    }

    #[test]
    fn returns_none_when_all_unhealthy() {
        let entry = entry_with(vec![b("10.0.0.1", 1)]);
        let sel = Selector::new();
        assert!(sel.pick(&entry, |_| false).is_none());
    }
}
