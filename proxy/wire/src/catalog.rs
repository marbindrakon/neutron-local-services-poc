//! Catalog schema shared between agent (writer), worker (consumer
//! for data-path config), and priv helper (consumer for BindListener
//! authorization).
//!
//! The on-disk format is documented in `proxy/worker/src/catalog.rs`:
//! one HMAC-SHA256 hex line, newline, then this struct serialized as
//! JSON. Keeping the schema in `nls-proxy-wire` ensures both binaries
//! agree on the field set without reaching across crate boundaries.

use std::net::IpAddr;

use serde::{Deserialize, Serialize};

use crate::Proto;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Catalog {
    pub version: u32,
    pub generation: u64,
    pub entries: Vec<Entry>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Entry {
    pub net_id: String,
    pub service_id: String,
    pub nonce: String,
    pub nonce_path: String,
    pub vip: IpAddr,
    pub port: u16,
    pub proto: Proto,
    pub backends: Vec<Backend>,
    pub health_check: HealthCheck,
    #[serde(default = "default_lb_algo")]
    pub lb_algo: LbAlgo,
    #[serde(default = "default_max_concurrent")]
    pub max_concurrent: u32,
    #[serde(default = "default_max_session_idle_s")]
    pub max_session_idle_s: u32,
}

fn default_lb_algo() -> LbAlgo {
    LbAlgo::Wrr
}
fn default_max_concurrent() -> u32 {
    1000
}
fn default_max_session_idle_s() -> u32 {
    60
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Backend {
    pub addr: IpAddr,
    pub port: u16,
    #[serde(default = "default_weight")]
    pub weight: u32,
}

fn default_weight() -> u32 {
    1
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum LbAlgo {
    Wrr,
    LeastConn,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum HealthCheck {
    TcpConnect {
        #[serde(flatten)]
        common: HcCommon,
    },
    HttpGet {
        #[serde(flatten)]
        common: HcCommon,
        path: String,
        #[serde(default = "default_http_status")]
        expect_status: u16,
    },
    HttpsHandshake {
        #[serde(flatten)]
        common: HcCommon,
        #[serde(default)]
        sni: Option<String>,
    },
    UdpDnsQuery {
        #[serde(flatten)]
        common: HcCommon,
        #[serde(default = "default_dns_query")]
        query: String,
    },
    UdpNtpQuery {
        #[serde(flatten)]
        common: HcCommon,
    },
}

fn default_http_status() -> u16 {
    200
}
fn default_dns_query() -> String {
    "health.invalid".into()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HcCommon {
    #[serde(default = "default_interval")]
    pub interval_s: u32,
    #[serde(default = "default_timeout")]
    pub timeout_s: u32,
    #[serde(default = "default_fail_after")]
    pub fail_after: u32,
    #[serde(default = "default_rise_after")]
    pub rise_after: u32,
}

fn default_interval() -> u32 {
    5
}
fn default_timeout() -> u32 {
    2
}
fn default_fail_after() -> u32 {
    3
}
fn default_rise_after() -> u32 {
    2
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn udp_ntp_query_round_trips() {
        let json = r#"{"type":"udp_ntp_query"}"#;
        let hc: HealthCheck = serde_json::from_str(json).unwrap();
        match hc {
            HealthCheck::UdpNtpQuery { common } => {
                // Defaults from HcCommon should apply when omitted.
                assert_eq!(common.interval_s, 5);
                assert_eq!(common.timeout_s, 2);
            }
            other => panic!("unexpected variant: {other:?}"),
        }
        let back = serde_json::to_string(&HealthCheck::UdpNtpQuery {
            common: HcCommon {
                interval_s: 5,
                timeout_s: 2,
                fail_after: 3,
                rise_after: 2,
            },
        })
        .unwrap();
        assert!(back.contains(r#""type":"udp_ntp_query""#));
    }

    #[test]
    fn legacy_script_variant_no_longer_parses() {
        // The `script` variant was removed from the wire so a tampered
        // (or stale-agent-emitted) catalog can't ask the worker to
        // fork-exec an attacker-named binary.
        let json = r#"{"type":"script","path":"/usr/bin/whatever","args":["rm","-rf","/"]}"#;
        let err = serde_json::from_str::<HealthCheck>(json).unwrap_err();
        let msg = format!("{err}");
        assert!(
            msg.contains("unknown variant") || msg.contains("script"),
            "unexpected error: {msg}"
        );
    }
}
