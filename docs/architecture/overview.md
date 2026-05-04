# Architecture overview

This document describes the system architecture of the local-services
capability — the service plugin, the ovn-agent extension, the
per-tenant network namespace lifecycle, and the exposure-plugin
abstraction. Plugin-specific internals live in
[`nat-plugin.md`](nat-plugin.md) and [`proxy-plugin.md`](proxy-plugin.md).

## Goals revisited

The core promise: an operator can register a network service in
Neutron and have it reachable from any tenant network at a
link-local IP, without any tenant-side routing setup, without
modifying OVN's data model, and without putting the tenant in the
operator's underlay.

Concretely the architecture must:

- Provide a CRUD API for services, backends, and bindings.
- Reconcile state on the chassis so each tenant network gets the
  link-local VIP set it should see.
- Hand traffic to a real backend with health-check pruning of dead
  ones.
- Stay isolated cross-tenant on shared kernel resources.
- Survive agent restarts, OVS restarts, OVN failovers.

## High-level diagram

```text
┌────────────────────────────────────────────────────────────────────────┐
│              Service Registry API (Neutron extension)                  │
│         services • backends • bindings • health-check policy           │
└──────────────────────────────┬─────────────────────────────────────────┘
                               │ writes
                               ▼
                          ┌──────────┐
                          │  OVN NB  │   LSP (localport, single fixed IP)
                          │          │   LS attachments
                          └────┬─────┘
                               │ northd
                               ▼
                          ┌──────────┐
                          │  OVN SB  │
                          └────┬─────┘
                               │ ovn-agent (per chassis) watches SB
                               ▼
═══════════════════════════════════════════════════════════════════════════
                            COMPUTE CHASSIS
═══════════════════════════════════════════════════════════════════════════

  ┌────────────────────────────────────────────────────────────────────┐
  │                         ovn-agent                                  │
  │   Extensions:                                                      │
  │     ┌──────────────────────────────────────────────────────────┐   │
  │     │ metadata-extension      (existing)                       │   │
  │     │ local-services-extension (this project)                  │   │
  │     └──────────────────────────────────────────────────────────┘   │
  │                                                                    │
  │   local-services-extension:                                        │
  │     • watches SB Port_Binding for our localport rows               │
  │     • per-network localsvc-<network> netns lifecycle               │
  │     • plumbs tap (br-int) + underlay-egress (nlsu) veths           │
  │     • reconciles link-local /32 VIPs on the tap                    │
  │     • dispatches per-service catalog slices to exposure plugins    │
  │     • polls registry every 10s; events trigger immediate reconcile │
  └────────────┬─────────────────────────┬─────────────────────────────┘
               │ catalog write           │ keepalived spawn/reload
               ▼                         ▼
  ┌──────────────────────────────┐   ┌──────────────────────────────┐
  │ proxy plugin                 │   │ nat plugin                   │
  │   nls-proxy-priv (tiny       │   │ keepalived per tenant netns  │
  │     priv helper)             │   │                              │
  │   nls-proxy (worker, host    │   │ programs kernel ip_vs in     │
  │     root netns, zero caps)   │   │ the netns, with NAT-mode     │
  │   trust split: helper does   │   │ MASQUERADE so backend        │
  │     setns+bind, worker dials │   │ replies traverse the         │
  │     backends from host       │   │ director                     │
  │                              │   │ HC: TCP_CHECK / HTTP_GET /   │
  │   structural per-tenant      │   │     SSL_GET / MISC_CHECK     │
  │   isolation; HMAC catalog;   │   │     (DNS / NTP shipped)      │
  │   admin endpoint over UDS    │   │                              │
  └─────────────┬────────────────┘   └─────────────┬────────────────┘
                │ listener fds                     │ ip_vs rules
                │ (live in tenant netns)           │ (live in tenant netns)
                ▼                                  ▼
       ┌────────────────┐               ┌────────────────┐
       │ VM on tenant-A │               │ VM on tenant-B │
       └───────┬────────┘               └───────┬────────┘
               │ tap                            │ tap
               ▼                                ▼
  ┌────────────────────────────────────────────────┐
  │                  br-int (OVS)                  │◄── flows from SB
  │                                                │    by ovn-controller
  │  tenant-A LS    pipeline → localport-A         │
  │  tenant-B LS    pipeline → localport-B         │
  └──────┬──────────────────────────────────┬──────┘
         │ localport-A                      │ localport-B
         ▼                                  ▼
  ┌──────────────────────────┐  ┌──────────────────────────┐
  │  netns: localsvc-tenA    │  │  netns: localsvc-tenB    │
  │                          │  │                          │
  │  tap with on-subnet IP   │  │  tap with on-subnet IP   │
  │  + /32 service VIPs      │  │  + /32 service VIPs      │
  │                          │  │                          │
  │  underlay-egress veth    │  │  underlay-egress veth    │
  │  (nlsu) — default route  │  │  (nlsu) — default route  │
  │  to host root netns      │  │  to host root netns      │
  │                          │  │                          │
  │  proxy listeners (priv-  │  │  proxy listeners (priv-  │
  │  helper-bound) and/or    │  │  helper-bound) and/or    │
  │  ip_vs DNAT (nat plugin) │  │  ip_vs DNAT (nat plugin) │
  └─────────────┬────────────┘  └─────────────┬────────────┘
                │                              │
                ▼ (proxy worker dials from     ▼
                   host root netns; nat plumbs
                   via nlsu+SNAT)
  ┌────────────────────────────────────────────────┐
  │  host root netns                               │
  │  underlay routing / service VRF                │
  │  nls-proxy worker dials backends from here     │
  │  per-network FORWARD ACL whitelists configured │
  │  backend (proto, addr, port) tuples for nat    │
  │  plugin egress; default DROP at the chain tail │
  └──────────────────────┬─────────────────────────┘
                         │
                         ▼
              ┌──────────────────────┐
              │   Backend services   │
              │ DNS · NTP · KMS·RHUI │
              └──────────────────────┘
```

## Components

### Service Registry API

A Neutron service plugin (registered as `local_services` in
`neutron.conf`'s `service_plugins`) provides three resources:

- `local_service` — the operator-defined service definition (VIP,
  port, protocol, exposure plugin, HC policy).
- `local_service_backend` — a real backend address+port behind a
  service.
- `local_service_binding` — a (service, network) pair scoping the
  service onto a tenant network.

CRUD on these resources uses the standard Neutron RBAC / policy
machinery. Default policy: admin-only mutate, anyone-read. See
[the API reference in README.md](../../README.md#api-reference).

#### Effective attachment & opt-in / opt-out

A service is "effectively attached" to a network when it should
materialize a localport + DHCP host_routes there. The rule is:

- `attachment_policy = opt-in`: effective iff a `local_service_binding`
  row exists for the (service, network) pair with `enabled=true`.
- `attachment_policy = opt-out`: effective for every Neutron network
  unless a `local_service_binding` row exists for the pair with
  `enabled=false` (the row functions as the opt-out marker).
- `service.enabled=false` overrides everything — the service is never
  effective, regardless of binding state.

Both the server-side host_routes injector and the agent-side reconciler
compute the effective set the same way; the agent reads it via two REST
calls (the per-network bindings list and a filtered query for enabled
opt-out services) so no privileged data crosses an extra channel.

#### Plugin reconciler

The service plugin runs a periodic loop
(`[local_services] plugin_reconciler_interval`, default 60s) that
walks every Neutron network and brings localport state in line with the
effective service set. The same per-network reconcile routine
(`LocalServicesPlugin._reconcile_network`) fires synchronously on every
binding create/update/delete. Together this gives:

- O(reconcile_interval) latency for opt-out services to attach to a
  newly-created network or for a newly-created opt-out service to fan
  out across existing networks.
- Immediate response to explicit binding writes (no waiting for the
  next tick).
- Self-healing: any drift between the binding/service catalog and the
  on-chassis localport state gets reconciled on the next tick.

### Localport piggyback

The plugin doesn't introduce a new OVN type. Instead, it creates a
single Neutron port per (network, locality-of-services) with
`device_owner = ovn-lb-hm:distributed` and a marker `device_id` of
the form `ovn-lb-hm-localsvc-<network_uuid>`. The leading
`ovn-lb-hm-` triggers OVN's existing localport materialization (the
same pattern Octavia LB-HM ports use); the embedded `localsvc-`
infix disambiguates our ports from real LB-HM ports so we never
mistake one for the other.

The Neutron port's fixed IP is on a regular tenant-network subnet,
so guests have an on-subnet next-hop to reach the link-local VIPs
via DHCP option 121 (classless static routes).

### DHCP host_routes injection

When a service is bound to a network, the plugin injects classless
static routes into every IPv4 subnet of that network:
`<vip>/32 via <localport-fixed-ip>`. Cleaned up on unbind.

### ovn-agent extension

A chassis-local extension to ovn-agent (`local_services` in
`[agent] extensions` of `ovn_agent.ini`) does the host-side work:

- Watches SB `Port_Binding` events filtered to our localports.
- Brings up a `localsvc-<network_uuid>` netns and plumbs a tenant-
  side veth (`tls<short>`) into it from `br-int` with `iface-id ==
  <localport_id>` so ovn-controller binds the LSP.
- Plumbs an underlay-egress veth (`nlsu<short>`) from the netns to
  the host root netns over a /30 from a configurable RFC6598 pool
  (default `100.64.0.0/22`). This lets the `nat` plugin's
  Keepalived/ip_vs reach operator backends on the chassis underlay.
- Reconciles the link-local /32 VIP set on the tenant veth from the
  registry every 10s (and on every PB event).
- Dispatches each service's catalog slice to its exposure plugin's
  `apply_config()`.

### Exposure plugins

Two plugins ship in v1, picked per service by the operator:

- **`nat`** (default): keepalived in the netns programs kernel ip_vs
  in NAT mode. Single audited userspace binary (keepalived). See
  [nat-plugin.md](nat-plugin.md).
- **`proxy`**: a Rust userspace L4 proxy daemon (`nls-proxy`) with a
  privileged helper (`nls-proxy-priv`) for tenant-netns binding.
  Per-tenant tokio current-thread runtime; HMAC-signed catalog;
  Envoy-shape admin endpoint over a UDS. See
  [proxy-plugin.md](proxy-plugin.md).

A single network can mix plugins; one service's choice doesn't
constrain another's.

## Per-network state

For each network with at least one bound service, the agent
maintains:

- A `localsvc-<network_uuid>` netns.
- A tenant-side veth pair (`tls<short>0` host / `tls<short>1` ns)
  attached to `br-int` with `iface-id == <localport_id>` so OVN
  forwards tenant frames to the netns.
- An underlay-egress veth pair (`nlsu<short>0` host /
  `nlsu<short>1` ns) entirely in the host root netns (NOT on
  `br-int`). The host-side end SNATs the netns CIDR to the chassis's
  egress IP via `iptables -t nat POSTROUTING`. The ns-side end
  carries a default route in the netns.
- Per-network iptables rules in **both** netns and host root netns
  for tenant-escape defense in depth (see "Underlay egress" below).
- Plugin state (e.g. `keepalived.conf` for nat, catalog entries for
  proxy) under `/var/lib/neutron-local-services/<network_uuid>/`.

## Underlay egress (defense in depth against tenant escape)

The `nat` plugin's Keepalived runs *inside* the per-tenant netns,
which by default has only its on-subnet route. To reach operator
backends on the chassis underlay, the agent provisions the
`nlsu<short>` veth pair described above, plus protections on both
ends:

- **In the host root netns**, a per-network `NLS_UND_<short>` chain
  whitelists exactly the configured backend `(proto, addr, port)`
  tuples (including any `health_check_address` / `health_check_port`
  overrides). Default DROP at the chain tail. Reconciled on every
  catalog change. **This is the primary tenant-escape gate.**
- **Chassis-wide**, `-i nlsu+ -o nlsu+ -j DROP` blocks one tenant
  from cross-talking another tenant's underlay path.
- **rp_filter = 1** on both ends of every `nlsu` veth.
- **In the netns**, FORWARD permits the tenant→underlay flow
  (allowing the standard ip_vs DNAT path to work) and the
  conntrack-tracked return path; the catch-all `DROP` at the tail
  prevents anything else from leaking through.
- The `proxy` plugin doesn't need this plumbing because its worker
  reaches backends from the host root netns directly. But the
  host-side ACL plumbing lives anyway — it costs nothing for proxy-
  only networks.

The host-side per-network whitelist is the load-bearing protection.
It's keyed on the configured backend set, so any tenant attempt to
forward to a non-backend underlay destination gets DROPped at the
host's FORWARD chain.

## What the agent doesn't do

- **No direct OVN NB writes for LSPs.** All Neutron port creation
  goes through `core_plugin.create_port()`. NB writes happen only
  for `DHCP_Options` (via the OVN mech driver, not us).
- **No service VIPs in `LSP.addresses`.** Tenant guests reach VIPs
  via DHCP option 121 (host_routes) plus ARP-respond from the
  netns kernel for the /32. The LSP itself only has the localport's
  on-subnet fixed IP.
- **No metadata `ovnmeta-` reuse.** Our netns name is
  `localsvc-<network>`; the netns shape and naming are independent
  from metadata's.
- **No Python health-check daemon.** HC is the exposure plugin's
  responsibility — keepalived for `nat`, the worker's HC engine for
  `proxy`.

## What's intentionally not implemented

See [`docs/limitations.md`](../limitations.md) for the full list:
IPv6, AZ-aware backend selection, deep RBAC, cross-chassis HA, L7
features. Each entry there is a "deferred-by-design" item — they're
deferrals, not bugs.
