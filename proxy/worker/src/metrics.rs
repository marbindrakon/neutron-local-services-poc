//! Prometheus text-format metrics for the worker.
//!
//! We hand-format the small set of metrics we expose rather than
//! pulling a full Prometheus client crate. The set is intentionally
//! minimal for the PoC; it'll grow as operator dashboards demand.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use crate::catalog::Catalog;
use crate::hc::{BackendId, Status, StatusMap};

#[derive(Debug, Default)]
pub struct WorkerMetrics {
    pub catalog_loads_total: AtomicU64,
    pub catalog_load_errors_total: AtomicU64,
    pub catalog_generation: AtomicU64,
}

impl WorkerMetrics {
    pub fn new() -> Arc<Self> {
        Arc::new(Self::default())
    }

    pub fn note_load_ok(&self, generation: u64) {
        self.catalog_loads_total.fetch_add(1, Ordering::Relaxed);
        self.catalog_generation.store(generation, Ordering::Relaxed);
    }

    pub fn note_load_err(&self) {
        self.catalog_load_errors_total
            .fetch_add(1, Ordering::Relaxed);
    }

    pub fn render(&self, cat: &Catalog, status: &StatusMap) -> String {
        let mut s = String::new();
        s.push_str("# HELP nls_proxy_catalog_generation Last successfully loaded catalog generation\n");
        s.push_str("# TYPE nls_proxy_catalog_generation gauge\n");
        s.push_str(&format!(
            "nls_proxy_catalog_generation {}\n",
            self.catalog_generation.load(Ordering::Relaxed)
        ));

        s.push_str("# HELP nls_proxy_catalog_loads_total Successful catalog reload count\n");
        s.push_str("# TYPE nls_proxy_catalog_loads_total counter\n");
        s.push_str(&format!(
            "nls_proxy_catalog_loads_total {}\n",
            self.catalog_loads_total.load(Ordering::Relaxed)
        ));

        s.push_str("# HELP nls_proxy_catalog_load_errors_total Catalog reload errors (parse / HMAC)\n");
        s.push_str("# TYPE nls_proxy_catalog_load_errors_total counter\n");
        s.push_str(&format!(
            "nls_proxy_catalog_load_errors_total {}\n",
            self.catalog_load_errors_total.load(Ordering::Relaxed)
        ));

        s.push_str("# HELP nls_proxy_listeners Number of configured listeners\n");
        s.push_str("# TYPE nls_proxy_listeners gauge\n");
        s.push_str(&format!("nls_proxy_listeners {}\n", cat.entries.len()));

        let mut up = 0u64;
        let mut down = 0u64;
        let mut unknown = 0u64;
        for entry in &cat.entries {
            for b in &entry.backends {
                let id = BackendId::from(entry, b);
                match status.get(&id) {
                    Some(Status::Up) => up += 1,
                    Some(Status::Down) => down += 1,
                    Some(Status::Unknown) | None => unknown += 1,
                }
            }
        }
        s.push_str("# HELP nls_proxy_backends Backends by health state\n");
        s.push_str("# TYPE nls_proxy_backends gauge\n");
        s.push_str(&format!("nls_proxy_backends{{state=\"up\"}} {up}\n"));
        s.push_str(&format!("nls_proxy_backends{{state=\"down\"}} {down}\n"));
        s.push_str(&format!("nls_proxy_backends{{state=\"unknown\"}} {unknown}\n"));

        s
    }
}
