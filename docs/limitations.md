# Limitations of the Neutron Local Services PoC

This document captures everything the PoC deliberately does **not** do —
what was deferred, why, and what a productization effort would need to
add. It is the source of truth for "is this missing because we forgot,
or because we chose not to?"

The PoC's goal was to prove the data path (link-local VIPs from a
tenant guest land at an operator backend through netns plumbing on the
chassis) and the management path (a service plugin with a registry,
DHCP host_routes injection, an ovn-agent extension, two pluggable
exposure backends). Anything outside that core scope was bounded to
keep the PoC tractable.

For the original scope statement see
`README.md#out-of-scope----future-possibilities`. This file is the
consolidated, citeable version of what didn't make it into the PoC
and why.

---

## 1. IPv6 data path

**Model + API: done. Data path: IPv4-only.** The registry already
accepts and stores IPv6 VIPs — `local_ipv6` is a real column on
`local_services` (`neutron_local_services/db/models.py`), the API
validates and round-trips it (`db/local_services_db.py`), and a
service can be created with either family or both. What's not wired
up is the chassis-side realization:

- The DHCP host_routes injector (`neutron_local_services/host_routes.py`)
  writes IPv4 classless static routes (DHCP option 121) and skips IPv6
  subnets entirely.
- The agent extension's tap reconciler (`neutron_local_services/agent/netns.py`)
  layers `inet`-only `/32`s onto the localport veth.
- Both exposure plugins render IPv4-only listeners (LVS
  `virtual_server`; the proxy catalog only emits v4 entries).

**What productization needs:**

- Inject equivalent IPv6 routes via DHCPv6 option 22 or RA Route
  Information Option (RFC 4191). Neutron's RA stack is the integration
  point — same shape as the IPv4 host_routes path but a different
  branch in the mech driver.
- Render `/128` VIPs onto the tap; teach both plugins to emit
  IPv6-aware listener configs.
- Decide cross-family fallback policy (does a v4-only client get a
  hint that the v6 backend exists? Probably no.)

The data path is family-agnostic at the netns layer, so the lift is
plumbing, not architecture.

## 2. Availability Zones

**Not implemented.** The service registry has no AZ awareness:

- Backends carry `address`/`port` only — no `availability_zone` field.
- The agent picks up every backend for every binding regardless of
  which AZ the chassis belongs to.
- The mech driver injects the same VIP set into every chassis's
  DHCP_Options for the network.

**What productization needs:**

- Add `availability_zone` to the backend model with the standard
  Neutron AZ semantics (nullable = "global, fallback").
- Filter backends in the agent's `desired_state_for_network` per
  chassis AZ. A "no AZ-local backends, fall back to global"
  selector matches the README's stated intent.
- Decide whether VIPs themselves should be AZ-scoped, or whether
  AZ is purely a backend selection concern. (The PoC team's read:
  AZ is a backend concern; the VIP is the same everywhere, the
  director picks the closest backend.)

This is additive — none of the PoC code paths need to change shape,
just gain a filter.

## 3. RBAC depth

**Minimal.** Policy defaults
(`neutron_local_services/policies.py`) are admin-only for mutate,
read-anyone for list/show. There is no per-tenant ownership of services
or backends, no project-scoped sharing model.

What this means in practice:

- Anyone with a Neutron token can `GET /v2.0/local_services` and see
  every operator-defined service and its VIPs (the VIPs are link-
  local and not exploitable on their own, but the catalog is still
  enumerable).
- Only admins can create/update/delete services, backends, or
  bindings.
- A binding scopes a service to a network, but there is no check
  that the network's project is allowed to consume that service —
  any admin can bind any service to any network.
- The opt-in/opt-out **mechanism** is implemented (see §11), but the
  RBAC story for who is allowed to opt in or opt out is not — any
  admin can create or delete any binding row.

**What productization needs:**

- Project-owned services with the standard Neutron RBAC sharing
  table (`local_service_rbacs`) — same shape as
  `network_rbacs` / `qos_policy_rbacs`. Targets: project, all
  projects (public), specific access_as_external|access_as_shared.
- Per-binding authorization: the requester must have access to both
  the service (via RBAC) and the network (via existing Neutron
  network policy).
- A "consumer" project distinct from the "owner" project so a tenant
  can opt their network in to a shared service without granting
  mutate rights.
- Per-policy authorization for opt-in / opt-out actions so a tenant
  can opt their own network in to a shared opt-in service or out of
  an opt-out service without admin involvement.
- Audit logs for binding mutations so the operator can answer
  "who bound what where, and when" without grepping API logs.

This is the deepest missing piece. The PoC works because the operator
runs everything under a single admin tenant; a real multi-tenant cloud
needs the RBAC story before it can ship.

## 4. Underlay-network backends

**Both plugins reach the underlay.** Backends that live on the chassis
underlay (operator infra on RFC1918 ranges, external services via the
chassis's default route) work for both `nat` and `proxy` plugins,
through different mechanisms:

- **`proxy` plugin:** the worker process lives in the host root netns
  by design (the privilege-split architecture binds listener fds
  inside the tenant netns but the worker dials backends from host
  netns). Underlay reachability is inherited from the chassis's
  routing table — no extra plumbing.
- **`nat` plugin:** Keepalived/ip_vs runs *inside* the
  `localsvc-<network>` netns, which has only its on-subnet route by
  default (e.g. `10.0.0.0/26 dev tls...`). The agent therefore
  provisions a per-network underlay-egress veth pair (host-root-side
  `nls<net>0`, ns-side `nls<net>1`) over a /30 from a configurable
  RFC6598 pool (default `100.64.0.0/22`), installs a default route in
  the netns via the host-side IP, and SNATs the netns CIDR to the
  chassis's egress IP via `iptables -t nat POSTROUTING`. The new veth
  is **not** on `br-int` — it lives entirely in Linux kernel land and
  never surfaces to OVN.

**Defense in depth against tenant escape via the underlay path** —
because the netns runs `ip_forward=1` so ip_vs can DNAT, a tenant
could in principle inject arbitrary-destination packets at the
tenant-side veth and have them forwarded out the underlay veth. The
agent installs four protections:

1. **The architectural barrier**: the netns has no route for any
   destination outside its on-subnet CIDR except the default route
   via the underlay veth. Any non-VIP destination tenant traffic
   ends up flowing out that veth — but the host-side ACL (next item)
   stops it there.
2. **Host-side per-network FORWARD whitelist** caps egress to exactly
   the configured backend `(proto, addr, port)` tuples — including
   `health_check_address`/`health_check_port` overrides. Default DROP
   at the chain tail. Refreshed on every catalog change. This is the
   primary tenant-escape gate.
3. **Inter-tenant DROP** (`-i nls+ -o nls+ -j DROP`) blocks one
   tenant from cross-talking another tenant's underlay path.
4. **rp_filter = 1** on both ends of the new veth prevents source-IP
   spoofing.

(An earlier design also tried requiring `conntrack --ctstatus DNAT`
on the in-netns FORWARD chain to demand that egress traffic had been
DNAT'd by ip_vs first. That match doesn't translate cleanly to the
nf_tables iptables backend on every distro; we drop it. The
host-side ACL is the load-bearing protection regardless.)

**Configuration knob:**

```ini
[local_services_agent]
underlay_egress_cidr = 100.64.0.0/22
```

A `/22` gives 1024 networks per chassis. The default RFC6598
"shared address space" rarely collides with operator private
networks; clouds that use it for carrier-grade NAT internally should
override.

**What productization could still tighten:**

- A higher-fidelity destination ACL keyed on conntrack `mark` from
  ip_vs rather than `(proto, addr, port)` — would let two services on
  the same network with the same backend address but different
  protocols be expressed cleanly.
- IPv6 underlay egress (the design is family-agnostic; only the pool
  CIDR and the inet/inet6 split need duplication).
- Explicit egress-interface selection (today the SNAT rule uses
  whatever interface the kernel picks; an operator running on a
  multi-homed chassis may want to pin the egress).

## 5. UDP — both plugins handle it

UDP works on both `nat` and `proxy`:

- **`nat`** uses kernel `ip_vs` UDP forwarding with `MISC_CHECK` probe
  scripts for DNS and NTP (shipped under
  `agent/plugins/check_scripts/`).
- **`proxy`** has a per-tenant UDP forwarder built on
  `tokio::net::UdpSocket`, with thread-local session tables (no
  shared map across tenants) and built-in `udp_dns_query` and
  `script` health checks.

There's no UDP-shaped gotcha left in the PoC. (An earlier two-tier
envoy-based design was abandoned in favor of the Rust proxy daemon
because envoy's HCM has no path to terminate CONNECT-UDP back into
raw datagrams in-process — that empirical finding led to the
single-daemon architecture.)

## 6. Multi-chassis validation

The PoC was validated end-to-end on a small DevStack lab (controller
plus four compute chassis). The agent is structurally multi-chassis-
correct: SB Port_Binding events fire on whichever chassis hosts the
localport, the netns is per-chassis, and each chassis runs its own
nat director / proxy worker pair, so there's no cross-chassis
coordination to get wrong.

**Validated on the PoC lab:**

- Five-node setup (one controller running Neutron + ovn-northd, four
  compute nodes running the agent).
- Tenant VMs scheduled across multiple chassis; each chassis
  provisions its own `localsvc-<network>` netns, its own keepalived,
  its own proxy worker. Per-chassis state is independent.
- Cross-chassis isolation assertions in the multi-chassis test
  fixture confirm one chassis's failure or backend-set divergence
  doesn't perturb another's data path.

What's left for productization is more "scale testing" than "correct-
ness testing": stress the agent with hundreds of networks per
chassis, multiple agent restarts in a row, etc.

## 7. Health-check daemon

**Not in v1.** Health checking is delegated to the exposure plugin —
Keepalived's TCP_CHECK / HTTP_GET / SSL_GET / MISC_CHECK in the
`nat` plugin, the proxy worker's built-in HC engine
(`tcp_connect` / `http_get` / `https_handshake` / `udp_dns_query` /
`script`) in the `proxy` plugin. The agent does not run a Python
health-check probe.

This is a deliberate choice (no Python HC daemon — HC fidelity belongs
to the plugin that owns the data path), not a deferral. The HC story is
complete for the PoC; what's missing is **surfacing** HC state through
the API. `NatPlugin.get_backend_health` inherits the base `'unknown'`
stub; `ProxyPlugin.get_backend_health` already scrapes the worker's
admin endpoint but the result isn't plumbed into the registry response.
The productization target is to parse `/proc/net/ip_vs` for the `nat`
plugin and the proxy worker's Envoy-shape `/clusters` JSON for the
`proxy` plugin, then expose the result on
`GET /v2.0/local_service_backends/<id>` as a `health_status` field.

## 8. Octavia coexistence

**Refused at startup.** `LocalServicesPlugin.__init__` checks
`cfg.CONF.service_providers.service_provider` for the
`ovn_octavia_provider` substring and raises `OctaviaConflictError`
if found (with a duplicate guard in `initialize()` to cover code
paths that bypass `__init__`). The two cannot run side-by-side on
the same Neutron because they both own the `ovn-lb-hm:distributed`
device_owner.

**What productization needs:** a productization-time refactor to use a
distinct device_owner (e.g. `ovn-localsvc:localport`) and stop
piggybacking on the LB-HM port shape. Until then, operators must
choose one or the other.

## 9. Backend rate limiting / L7 features

**Not implemented.** Both plugins are strictly L4. The `nat` plugin
forwards via kernel `ip_vs`; the `proxy` plugin runs `splice`-style
bidirectional copy on accepted TCP connections and a per-tenant UDP
session table. There is no path-based routing, no header manipulation,
no rate limiting, no JWT auth — just L4 forwarding with active health
checks. A future L7 plugin would be a separate exposure plugin
alongside `nat` and `proxy` rather than a refactor of either.

## 10. Service groups, router scoping, and other v2 ideas

Per `README.md#out-of-scope`:

- ML2/OVS implementation (PoC is OVN-only)
- Backend rate limiting
- OVN LB exposure (would need OVN core work to let an OVN LB reach
  underlay backends)
- Octavia Amphora exposure
- Local Service Groups (related services enabled/disabled together)
- Local Service / Group enablement on routers (inherited to
  connected networks)

These are tracked in the README, not here, because they are open
roadmap items rather than deliberate constraints.

## 11. Opt-in / opt-out attachment

**Implemented.** The `attachment_policy` field on `local_services` is
honored end-to-end:

- **opt-in**: a service applies to a network only if a
  `local_service_binding` row exists with `enabled=true`.
- **opt-out**: a service applies to every Neutron network unless a
  `local_service_binding` row exists for that (service, network) pair
  with `enabled=false` — the row is the opt-out marker.

Fan-out for opt-out services is driven by a periodic reconciler in the
service plugin (`[local_services] plugin_reconciler_interval`, default
60s) that walks every Neutron network and brings localport state
in line with the effective service set. The same per-network reconcile
routine runs synchronously on every binding create/update/delete so
explicit changes apply immediately.

**Latency window:** an admin creating a new opt-out service waits up to
`plugin_reconciler_interval` seconds before the service appears on
networks that didn't already have a localport. Creating a new network
has the same latency window before opt-out services attach. Operators
can lower the interval (minimum 10s) for lab work or raise it for
large clouds.

**Scope:** cloud-wide. Every Neutron network sees every enabled
opt-out service. Project-scoped opt-out (only this project's networks)
is not implemented and would require RBAC depth not yet present (see
§3 above).

**What productization could still tighten:**

- A fan-out trigger keyed on Neutron NETWORK / SUBNET `AFTER_CREATE`
  events, eliminating the reconciler latency window for new networks
  without removing the periodic-reconciler safety net.
- Project-scoped opt-out (paired with §3 RBAC work).
- Distinguishing "haven't decided yet" from "explicitly opted out" for
  opt-in services — today both are represented as the absence of an
  enabled binding row.

---

## Summary table

| Area                            | Status in PoC               | Productization lift         |
| ------------------------------- | --------------------------- | --------------------------- |
| IPv6 data path                  | model + API done; chassis-side v4-only | medium (DHCPv6/RA + plugin v6) |
| Availability zones              | not implemented             | small (additive filter)     |
| RBAC                            | admin-mutate / read-anyone  | large (RBAC table + checks) |
| Underlay backends (nat plugin)  | works via per-tenant `nls` veth + per-backend ACL | small (tighter ACL options) |
| Underlay backends (proxy plugin)| works (worker in host netns)| n/a                         |
| Multi-chassis (verified)        | 5-node lab; tests green     | n/a                         |
| Health-check API surface        | plugin-internal only        | small (parse + expose)      |
| Octavia coexistence             | mutually exclusive          | medium (device_owner split) |
| L7 / rate limiting              | none                        | medium (new L7 plugin)      |
| Service groups / router scoping | not implemented             | medium (model + API)        |
| Opt-in / opt-out attachment     | implemented (cloud-wide)    | small (event-driven fan-out, RBAC depth) |
