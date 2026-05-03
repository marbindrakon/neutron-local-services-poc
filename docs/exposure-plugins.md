# Exposure plugins — `nat` vs `proxy`

Each local service picks an **exposure plugin** that owns its data
path on the chassis. The plugin name is set per service via the
`exposure_plugin` field at create time and cannot be changed
afterwards (delete and recreate to switch). A single tenant network
can mix services using different plugins; the agent runs each
plugin's lifecycle independently.

The plugin choice is a deployment decision. This page exists to make
that decision easy.

## TL;DR

| You want…                                          | Pick     |
| -------------------------------------------------- | -------- |
| The simplest possible operator footprint           | `nat`    |
| To ship one less binary (no Rust toolchain in CI)  | `nat`    |
| Raw kernel-path throughput                         | `nat`    |
| JSON access logs / per-flow observability          | `proxy`  |
| Compliance-grade audit boundary                    | `proxy`  |
| HC fidelity beyond `TCP_CHECK` / `MISC_CHECK`      | `proxy`  |

`nat` is the default. Most clouds should leave it alone unless one of
the `proxy` reasons specifically applies.

## Why `nat` is the default

The `nat` plugin's headline benefit is that **the operator doesn't
need to ship or audit a new binary**. Its only userspace component is
`keepalived` — a battle-tested piece of software that's been packaged
in EPEL, Ubuntu main, Debian, and every other major distro for over
two decades. Most ops teams already deploy it (for VRRP, for HAProxy
HC, for VIP failover) and most security teams already have it in their
approved binary lists. Adding a local-services capability with the
`nat` plugin means flipping a config knob and provisioning a Neutron
agent extension — not introducing a new daemon to the audit
inventory.

The data path is kernel ip_vs in NAT mode, which means line-rate
forwarding for the kinds of services this is designed to expose
(DNS, NTP, KMS, package repositories). On the same lab measurement
that recorded the proxy plugin's ~4 Gbps `copy_bidirectional`
throughput, `nat` was bound only by the underlying NIC.

The diagnostic story is also operationally familiar. `ipvsadm -ln`
inside the per-tenant netns shows the live virtual-server table and
realserver connection counts. Standard kernel-netfilter diagnostics
apply.

## Why pick `proxy`

The `proxy` plugin trades operational simplicity for richer behavior:

- **HC fidelity.** Built-in active health checks include
  `tcp_connect`, `http_get` (with status-code matching),
  `https_handshake`, `udp_dns_query`, and a generic `script` type
  that runs the same Keepalived `MISC_CHECK`-compatible probe scripts
  shipped with the package (so DNS and NTP HC scripts are reusable
  across both plugins). Health state surfaces as JSON on the admin
  unix socket at `/clusters?format=json` — same shape as Envoy's
  `/clusters` admin endpoint, so operators can layer existing
  scrapers on it.
- **Per-flow observability.** Connection counts, per-listener idle
  evictions, and quota counters are exposed on the admin endpoint's
  `/listeners` and `/metrics` (Prometheus) routes.
- **Structural per-tenant isolation.** Each tenant network's data
  path runs on its own OS thread with its own `tokio` current-thread
  runtime. The TCP forwarder's connection state and the UDP
  forwarder's session table are thread-local — no shared `DashMap`,
  no shared LRU. A bug or DoS targeting one tenant's flow cannot
  reach another tenant's state because it isn't on the same heap.
- **Compliance audit boundary.** All client connections are
  terminated at the proxy worker and re-originated to the backend
  from the host root netns. This is a clear bytes-in / bytes-out
  control point for compliance regimes that want one. The Rust
  worker is `#![forbid(unsafe_code)]` and runs zero-capability under
  systemd hardening (`ProtectKernelTunables`, `MemoryDenyWriteExecute`,
  `RestrictNamespaces=net`, etc.); the only privileged binary in the
  pair is a ~340 LOC helper (`nls-proxy-priv`) whose sole job is
  `setns()` + `bind()` + SCM_RIGHTS handoff.
- **Migration path to L7.** The two-tier socket model leaves room to
  later attach an HCM listener (HTTP routing, header manipulation,
  rate limits) without redoing the trust split. The PoC stops at L4.

The cost is shipping the `nls-proxy` and `nls-proxy-priv` Rust
binaries, plus their two systemd units. The agent's DevStack plugin
builds them automatically; for production deployments operators take
on packaging them.

## Co-existence

Both plugins coexist on the same chassis and even on the same tenant
network. A single tenant network can run, for example, DNS via `nat`
(line-rate, kernel forwarding, simple HC) and an internal HTTPS API
via `proxy` (with HC fidelity and access logs). The agent's
`reconcile_network` dispatches each service's catalog slice to the
right plugin's `apply_config`; the plugins don't share VIP or port
state.

## Performance reference

Both plugins are well above the throughput needed for canonical
local services (DNS, NTP, KMS, internal API endpoints). Lab
measurements on the PoC chassis:

- `proxy`: ~4 Gbps sustained on `tokio::io::copy_bidirectional`
  (kernel-bound `splice`-style copy).
- `nat`: effectively line-rate (no userspace data path).

Neither has been stress-tested at the millions-of-PPS UDP scale that
some carriers run for DNS — that's a productization concern, not a
PoC limitation.

## Health-check matrix

| Type      | `nat` (Keepalived)                       | `proxy` (built-in)                     |
| --------- | ---------------------------------------- | -------------------------------------- |
| `tcp`     | `TCP_CHECK`                              | `tcp_connect`                          |
| `http`    | `HTTP_GET` (status code match)           | `http_get`                             |
| `https`   | `SSL_GET`                                | `https_handshake`                      |
| `dns`     | `MISC_CHECK` running `check_dns.sh`      | `udp_dns_query` (built-in) or `script` |
| `ntp`     | `MISC_CHECK` running `check_ntp.sh`      | `script` running `check_ntp.sh`        |
| `none`    | no HC                                    | no HC                                  |

The shipped probe scripts live under
`neutron_local_services/agent/plugins/check_scripts/` and follow
Keepalived's `MISC_CHECK` contract (env vars `BACKEND_ADDR` /
`BACKEND_PORT` plus the same values as positional args). One script
works under both plugins.

## Distribution policy

Both plugins accept `round-robin` and `least-connection` for the
service's `distribution_policy` field. `active-backup` is currently
mapped to weighted round-robin in both plugins (a known PoC
simplification — see `docs/limitations.md`).

## Configuration

Per-service:

```bash
openstack network local-service create internal-dns \
    --local-ipv4 169.254.10.53 \
    --port 53 --protocol udp \
    --exposure-plugin nat \
    --health-check-type dns
```

Per-chassis (agent-side, in `[local_services_agent]`):

```ini
underlay_egress_cidr = 100.64.0.0/22   # for nat plugin underlay backends
```

The proxy plugin reads its catalog file path and HMAC key from a
state directory the DevStack plugin sets up automatically; production
deployments wire those via the agent's systemd unit.
