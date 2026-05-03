Neutron Local Services
======================

This repo contains a specification and proof-of-concept implementation for
a flexible local services capability in OpenStack Neutron. As used here,
"local services" refers to network services that are available to OpenStack
instances without routing outside of the local network segment. An existing example
of a local service is the OpenStack metadata service which is reachable on a
link-local IP address without requiring the workload to have any routable path
from its virtual network to a backend. With this new local services capability,
an operator will be able to expose arbitrary services such as DNS, NTP, and guest
OS services (i.e. Windows KMS or RPM/DEB repositories) to instances by link-local
IPs even when the tenant's network is isolated at L3.

Problem Statement
-----------------

Provide a way for an operator to expose arbitrary network services to tenant networks
within their cloud deployment without relying on the tenant network to have routed
access outside of the tenant's network. At the same time, provide a tenant an option
to opt-in or opt-out of certain local services based on the operator's policies.

In a general-purpose multitenant cloud, there are many services that the cloud provider
is expected to make easily available to tenant workloads. These almost universally
include DNS and NTP and usually also include guest OS licensing and content services
such as Windows KMS or content distribution tools for various Linux distributions.
They can also include more interactive elements like the existing OpenStack metadata API
or exposing the actual cloud APIs through private endpoints.

Exposing these services through local endpoints means that they are available to a
workload even if the network has no general outbound connectivity or if its connectivity
is through a private network solution such as EVPN that may not allow the operator a way
to expose services. Clouds which host higher compliance workloads (i.e. PCI-DSS) may
also choose to offer certain core services this way rather than rely on tenants to route
to public endpoints.

Prior Art
---------

- OpenStack Metadata
- Recursive DNS via DHCP Agent in ML2/OVS (OVN version only injects DHCP
  options and requires routability)
- The generic link-local service function in TungstenFabric / Contrail

Goals / Non-Goals
-----------------

Goals:

- Allow an operator to expose a catalog of local services to tenants via the Neutron API
- Allow an operator to configure each service as required, opt-out, or opt-in
- Allow tenants to connect to those services on a link-local IP address without
  a router
- Support TCP and UDP services
- Support multiple backend IPs per service with health checks
- Support either active-backup or simple load balancing across the backends

Non-Goals:

- Allow for tenants to create their own local services
- Provide security for the local services beyond an isolated proxy similar to metadata
- Provide any L5-7 handling of the local service traffic (i.e. TLS termination,
  instance header injection)

Use Cases
---------

### VPC / EVPN Tenant Networks

In clouds where tenant networks are implemented in isolated routing domains per
tenant or sub-tenant, it may not be practical or possilbe for an operator to
expose network services without traffic being routed into the tenant's network
core and then back to the provider over public interfaces. Even where that is
acceptable, the operator also has to contend with IP addressing conflicts when
the public interfaces are on an RFC1918-addressed WAN and tenants may be using
conflicting IPs within their routing domains.

With local services that proxy traffic to the compute / networker node underlay,
the operator can easily use link-local IPs to avoid conflicts and bypass any need
for traffic to be backhauled from tenant routing domains back to the provider.

### Proxy Termination and Compliance

When the userspace proxy exposure plugin is in use, all client
connections are terminated at the proxy and re-originated to
backends. This provides:

- A clear audit boundary between tenant and shared-services networks
- Per-flow connection accounting at the proxy
- A single point of observability and policy enforcement (active
  health checks, rate limits, structured logs)
- A migration path for future TLS re-termination if it becomes
  necessary

The kernel-NAT exposure plugin uses kernel-mode forwarding via
ip_vs and is appropriate for performance-sensitive deployments
where the proxy boundary doesn't need to be audited as an explicit
control. For compliance-regulated workloads (PCI-DSS, FedRAMP,
similar), the userspace proxy is recommended.

### DNS Resolution

With ML2/OVN today, an operator can configure default DNS options for DHCP leases
but these are not useful without the instances having connectivity to the actual
recursive resolvers over their project networks.

With this feature, the operator can provide proxied recursive DNS even on isolated
networks or where security compliance makes opening firewall rules for routed DNS
connectivity difficult. Recursive DNS even on isolated networks may be useful for
application service discovery.

### NTP

NTP as a local service would allow operators to easily provide basic time services
for instances without requiring that the instances be configured for hypervisor
specific time sync methods (i.e. ptp_kvm) or having a routed connection.

### Licensing and Content Distribution

Operators that provide guest OS licensing to their tenants (i.e. Microsoft
SPLA providers, Red Hat CCSPs, etc) need to provide specific private network
services to provide OS activation or access to subscription-based software repositories.
Without local service capability, those require routed connections back to the
provider's infrastructure or require tenants to install duplicate infrastructure
inside their networks.

Local services make these endpoints easily available to tenants regardless of
their routing structure so that these tools "just work."

### Access to API Endpoints

Tenants that want to run software in their project networks that utilize the
OpenStack APIs (i.e. Kubernetes cloud manager) today must have routed access
to the public API endpoints of the OpenStack cloud. With API endpoints exposed
as local services, those tools can be used even in isolated network enclaves.


Implementation
--------------

This repo includes a proof-of-concept implementation of a Neutron Service Plugin
to provide the local service catalog as well as an OVN Agent extension to
provide data path management in ML2/OVN-based clouds.

For deeper detail beyond what this README covers:

- [`docs/architecture/overview.md`](docs/architecture/overview.md) —
  full architecture: service plugin, agent extension, netns model,
  underlay-egress plumbing, defense-in-depth iptables.
- [`docs/architecture/nat-plugin.md`](docs/architecture/nat-plugin.md) —
  Keepalived/ip_vs internals.
- [`docs/architecture/proxy-plugin.md`](docs/architecture/proxy-plugin.md) —
  Rust L4 proxy daemon internals.
- [`docs/exposure-plugins.md`](docs/exposure-plugins.md) — operator
  guide: when to pick `nat` vs `proxy`.
- [`docs/limitations.md`](docs/limitations.md) — what's deferred and
  why (IPv6, AZ, RBAC depth, etc).

### Architecture

The service registry extension provisions chassis-local proxies per
tenant logical switch, plumbed via OVN `localport` constructs. Each
service picks an exposure plugin: `nat` (kernel ip_vs forwarding via
Keepalived inside the per-tenant network namespace) or `proxy` (a
chassis-wide userspace L4 daemon that bind-mounts listener sockets
into the per-tenant netns while doing all forwarding work in the
host root netns). `nat` is the default; both can coexist on the same
tenant network for different services.

```text
┌────────────────────────────────────────────────────────────────────────┐
│                  Service Registry API (Neutron extension)              │
│            services • backends • scope • health-check policy           │
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
                               │
       ┌───────────────────────┴───────────────────────┐
       │                                               │
       │ ovn-controller watches SB                     │ ovn-agent watches SB
       │  (flow programming)                           │  + Neutron registry API
       │                                               │  (host-side management)
       ▼                                               ▼
═══════════════════════════════════════════════════════════════════════════
                            COMPUTE CHASSIS
═══════════════════════════════════════════════════════════════════════════

  ┌────────────────────────────────────────────────────────────────────┐
  │                        ovn-agent (per chassis)                     │
  │                                                                    │
  │   Extensions (pluggable, like metadata extension today):           │
  │     ┌──────────────────────────────────────────────────────────┐   │
  │     │  metadata-extension       (existing)                     │   │
  │     │  local-service-extension  (this project)                 │   │
  │     └──────────────────────────────────────────────────────────┘   │
  │                                                                    │
  │   local-service-extension responsibilities:                        │
  │     • watch Port_Binding for local LSes that have services         │
  │     • create/destroy per-tenant svc netns on demand                │
  │     • plug localport into svc netns; configure addrs (incl. VIPs)  │
  │     • render plugin config (Keepalived for `nat`, JSON catalog     │
  │       for `proxy`) and drive plugin lifecycle                      │
  │     • emit metrics + status to host collector                      │
  └─────────────┬─────────────────────────┬────────────────────────────┘
                │ catalog write           │ Keepalived spawn/reload
                ▼                         ▼
  ┌──────────────────────────────┐   ┌──────────────────────────────┐
  │ proxy daemon pair            │   │ Keepalived per tenant netns  │
  │   nls-proxy-priv (root,      │   │   (when `nat` plugin is in   │
  │     small surface): only     │   │    use for any service on    │
  │     does setns + bind in     │   │    that network)             │
  │     tenant netns, hands      │   │                              │
  │     fds to worker via        │   │ Programs kernel ip_vs in     │
  │     SCM_RIGHTS               │   │ the netns; HC via MISC_CHECK │
  │   nls-proxy (unprivileged):  │   │ scripts shipped by this pkg  │
  │     per-tenant tokio thread, │   │                              │
  │     accept loop on tenant-   │   │ NAT mode + POSTROUTING       │
  │     bound listener fd, dial  │   │ MASQUERADE on the netns      │
  │     backend from host netns, │   │ veth so backend replies      │
  │     copy_bidirectional;      │   │ traverse the director        │
  │     active HC, /metrics,     │   │                              │
  │     /clusters admin endpoint │   │                              │
  └─────────────┬────────────────┘   └─────────────┬────────────────┘
                │ listener fds                     │ ip_vs rules
                │ (live in tenant netns)           │ (live in tenant netns)
                ▼                                  ▼
  ┌────────────┐                 ┌────────────┐
  │ VM tenant-A│                 │ VM tenant-B│
  └─────┬──────┘                 └─────┬──────┘
        │ tap                          │ tap
        ▼                              ▼
  ┌────────────────────────────────────────────────┐    ┌──────────┐
  │                  br-int (OVS)                  │◄───┤ flows    │
  │                                                │    │ programmed
  │  tenant-A LS    pipeline → localport-A         │    │ from SB  │
  │  tenant-B LS    pipeline → localport-B         │    │ by       │
  │  ARP responders & L2 lookups for VIP per LS    │    │ ovn-     │
  └──────┬──────────────────────────────────┬──────┘    │ controller
         │ localport-A                      │ localport-B└──────────┘
         ▼                                  ▼
  ┌─────────────────────────┐     ┌─────────────────────────┐
  │  netns: localsvc-tenA   │     │  netns: localsvc-tenB   │
  │  (managed by ovn-agent) │     │  (managed by ovn-agent) │
  │                         │     │                         │
  │   tap with fixed IP +   │     │   tap with fixed IP +   │
  │   /32 service VIPs      │     │   /32 service VIPs      │
  │                         │     │                         │
  │   listeners (nls-proxy- │     │   ip_vs rules (nat      │
  │   bound, owned by host  │     │   plugin) and/or        │
  │   worker thread):       │     │   listeners (proxy      │
  │     :53/udp  DNS        │     │   plugin):              │
  │     :123/udp NTP        │     │     :53/udp  DNS        │
  │     :1688/tcp KMS       │     │     :443/tcp  RHUI      │
  │     :443/tcp  RHUI      │     │                         │
  │                         │     │                         │
  │   veth-underlay ────────┼──┐  │   veth-underlay ────────┼──┐
  └─────────────────────────┘  │  └─────────────────────────┘  │
                               │                               │
                               ▼                               ▼
            ┌──────────────────────────────────────────────────┐
            │  host root netns                                 │
            │  underlay routing / service VRF                  │
            │  nls-proxy worker dials backends from here       │
            │  log/metrics collection (ovn-agent → telemetry)  │
            └──────────────────────┬───────────────────────────┘
                                   │
                                   ▼
                       ┌──────────────────────┐
                       │   Backend services   │
                       │ DNS · NTP · KMS·RHUI │
                       └──────────────────────┘
```

The `proxy` plugin uses a **trust-split** between two long-lived
processes per chassis:

- **Privileged helper** (`nls-proxy-priv`): tiny, audited binary
  with only the capabilities needed to enter a tenant network
  namespace and bind a low-numbered port. For each bind request it
  spawns a one-shot thread that `setns()`es into the requested
  netns, opens the socket, and hands the file descriptor back to
  the worker over SCM_RIGHTS. The thread exits as soon as the
  handoff is done.
- **Worker** (`nls-proxy`): unprivileged, sandboxed (seccomp,
  systemd hardening, no namespace transitions), runs forever in
  the host root netns. Receives bound listener fds from the
  helper. Linux preserves the netns binding on the socket, so
  reads on a tenant-bound listener fd see tenant traffic even
  though the worker thread itself is in the host netns. Backend
  `connect()` from the worker uses the host netns route table.

Cross-tenant isolation in the worker is **structural**: each
tenant's data-path runs on its own OS thread with its own
single-threaded async runtime, owning its listener fds and its UDP
session table on its own heap. There is no cross-tenant shared
mutable state — no global session map, no shared LRU, no
cross-tenant connection pool — so a bug in one tenant's path can't
corrupt another's.

The agent writes a single chassis-wide JSON catalog file
(HMAC-signed with a per-boot key) that the worker watches via
inotify. Catalog entries describe (network, VIP, port, protocol,
backends, health-check, load-balancing policy) tuples. On
parse-or-HMAC failure the worker keeps its previous good state and
surfaces a counter on `/metrics` rather than tearing down live
listeners. A boot-id sentinel file lets the agent detect worker
restarts and re-register tenant network namespaces automatically.

### Components

- **Service Registry API** — Neutron service plugin (`local_services`)
  providing CRUD for service definitions, backend pools, scoping
  rules, and health-check policy. Drives Neutron port creation for
  the per-network localport.
- **OVN NB / SB / northd / ovn-controller** — unmodified. The
  extension uses existing OVN primitives (`localport` LSPs); no
  schema changes required.
- **ovn-agent** — chassis-local management daemon. Hosts the
  `local-service-extension` alongside the existing
  `metadata-extension`, following the same extension pattern.
- **local-service-extension** — owns per-tenant network namespace
  lifecycle, veth plumbing into the host root namespace,
  link-local VIP reconciliation on the netns tap, and dispatch to
  the right exposure plugin per service.
- **Exposure plugins** (per service, per chassis):
  - **`nat` plugin** (default): Keepalived configures kernel ip_vs
    rules in the per-tenant netns. TCP + UDP support in a unified
    config syntax; health checks via TCP_CHECK, HTTP_GET, and (for
    the dns / ntp check types) MISC_CHECK with probe scripts
    shipped by this package. Kernel-path forwarding, low resource
    footprint, line-rate throughput. POSTROUTING MASQUERADE on
    the netns veth so backend replies traverse the director.
  - **`proxy` plugin**: single chassis-wide Rust L4 daemon
    (privileged helper + unprivileged worker). Listener fds are
    bound inside the tenant netns by the helper and handed to the
    worker via SCM_RIGHTS; the worker accepts connections, picks
    a healthy backend via weighted round-robin, dials it from the
    host root netns, and runs `splice`-style bidirectional copy.
    Active health checks (TCP connect, HTTP GET, HTTPS handshake,
    UDP DNS query, MISC_CHECK-compatible script) run in a
    dedicated async thread and publish status to a shared
    snapshot. Admin surface over a unix socket: `/healthz`,
    `/clusters` (envoy-shape JSON), `/listeners`, `/metrics`
    (Prometheus). Per-listener concurrency caps and idle-session
    eviction enforce resource bounds.
- **Host root netns** — provides underlay connectivity from the
  `proxy` worker (and from `nat`-plugin keepalived's NAT'ed reply
  path) to backend services.

API Reference
-------------

### Service Catalog API Objects

#### Service Definition (Tenant-visible Fields)

  - Service Name
  - Description
  - Local IPv4 (Optional if IPv6 is provided. Must not come from a configurable
    deny-list of IPs [i.e. 169.254.169.254])
  - Local IPv6 (Optional if IPv4 is provided. Must not come from a configurable
    deny-list of IPs [i.e. fe80::a9fe:a9fe])
  - Protocol (TCP, UDP, or TCP/UDP)
  - Port
  - Attachment policy
    - Opt-Out - Attached to tenant networks by default, tenant can opt-out.
                Opt-out is controlled by RBAC.
    - Opt-In - Not Attached to tenant networks by default, tenant can opt-in.
               Opt-in is controlled by RBAC.

#### Service Definition (Admin Fields)

  - Distribution policy (active-backup, round-robin, least-connection)
  - Exposure Plugin (`nat` or `proxy`; default `nat`)
    - Selects per-service. Different services on the same network
      can use different plugins; the agent runs the right one for
      each. Pick `nat` for raw throughput; pick `proxy` for
      health-check fidelity, per-flow observability, and a
      compliance-grade audit boundary.
  - Health check type — supported across both plugins: `tcp`,
    `http`, `https`, `dns`, `ntp`, or `none`.
    - `nat` plugin: `tcp` / `http` / `https` map to Keepalived's
      TCP_CHECK / HTTP_GET / SSL_GET; `dns` and `ntp` are
      implemented via MISC_CHECK with shipped probe scripts.
    - `proxy` plugin: `tcp` is a TCP connect probe; `http` is a
      GET with an expected status code; `https` is a TLS
      handshake; `dns` and `ntp` invoke the same MISC_CHECK probe
      scripts shipped with this package (the worker passes
      `BACKEND_ADDR`/`BACKEND_PORT` both as positional args and
      env vars so a single script works under either plugin).
  - Health check additional config (Optional, plugin-specific
    JSON blob — for example HTTP path or expected status code)

Per-service plugin selection means a single tenant network can mix
both plugins. The `nat` plugin runs Keepalived inside the tenant
netns and programs ip_vs there; the `proxy` plugin's listeners are
bound inside the tenant netns by the chassis-wide privileged
helper while the worker process itself stays in the host root
netns. Each binds different VIP/port tuples and they don't conflict.

#### Service Backend Definition (Admin Only)

  - Service
  - Backend Name
  - Availability Zone (Optional, the backend is considered AZ-specific if provided
    and will be preferred by agents inside that zone. If omitted, the backend is considered
    global and may be used as a fall-back if AZ-specific endpoints are not available
    [configurable, fallback disabled by default])
  - Weight (Optional)
  - Backend Address
  - Backend Port
  - Health check port (optional)
  - Health check address (optional)
  - Enabled / Disabled

### API Endpoints

All resources live under the standard Neutron `/v2.0/` prefix and use
the standard Neutron auth (X-Auth-Token from Keystone). Resource and
collection names match the alembic schema.

#### `local_services` — service definitions

```
GET    /v2.0/local_services
GET    /v2.0/local_services/<id>
POST   /v2.0/local_services
PUT    /v2.0/local_services/<id>
DELETE /v2.0/local_services/<id>
```

POST body:

```json
{
  "local_service": {
    "name": "internal-dns",
    "description": "Operator recursive DNS",
    "local_ipv4": "169.254.10.53",
    "local_ipv6": null,
    "protocol": "udp",
    "port": 53,
    "attachment_policy": "opt-in",
    "distribution_policy": "round-robin",
    "exposure_plugin": "nat",
    "health_check_type": "dns",
    "health_check_config": null,
    "enabled": true
  }
}
```

`protocol` is one of `tcp`, `udp`, `tcp-udp`.
`attachment_policy` is one of `opt-in`, `opt-out`.
`distribution_policy` is one of `active-backup`, `round-robin`, `least-connection`.
`exposure_plugin` is one of `nat` (default), `proxy`.
`health_check_type` is one of `none`, `tcp`, `http`, `https`, `dns`, `ntp`.
At least one of `local_ipv4` / `local_ipv6` must be provided. `protocol`,
`port`, and `exposure_plugin` are immutable after create. `health_check_config`
is a free-form JSON blob whose interpretation depends on `health_check_type`
(e.g. HTTP path or expected status code).

#### `local_service_backends` — real backends behind a service

```
GET    /v2.0/local_service_backends?service_id=<svc>
GET    /v2.0/local_service_backends/<id>
POST   /v2.0/local_service_backends
PUT    /v2.0/local_service_backends/<id>
DELETE /v2.0/local_service_backends/<id>
```

POST body:

```json
{
  "local_service_backend": {
    "service_id": "<service-uuid>",
    "name": "dns-east-1",
    "availability_zone": "az-east-1",
    "weight": 1,
    "address": "172.18.42.10",
    "port": 53,
    "health_check_address": null,
    "health_check_port": null,
    "enabled": true
  }
}
```

`service_id` is immutable after create. `availability_zone` is a free-form
string used for AZ-aware backend selection (not yet honored by the agent
in the PoC — see `docs/limitations.md` §2). `weight` defaults to 1; pass
`null` for "unweighted." `health_check_address` and `health_check_port`
are optional overrides — when omitted, HC probes the same `address` /
`port` as the data path.

#### `local_service_bindings` — scope a service to a tenant network

```
GET    /v2.0/local_service_bindings?service_id=<svc>&network_id=<net>
GET    /v2.0/local_service_bindings/<id>
POST   /v2.0/local_service_bindings
PUT    /v2.0/local_service_bindings/<id>
DELETE /v2.0/local_service_bindings/<id>
```

POST body:

```json
{
  "local_service_binding": {
    "service_id": "<service-uuid>",
    "network_id": "<network-uuid>",
    "enabled": true
  }
}
```

Creating a binding triggers (a) the `localport` Neutron port on that
network (created lazily; one per network), (b) DHCP `host_routes`
injection of `<vip>/32 via <localport-fixed-ip>` on every IPv4 subnet,
and (c) Port_Binding events at the chassis hosting the localport that
drive netns + plugin reconcile. Deleting the last binding for a network
reverses all three.

#### Default RBAC

Admin: full mutate. Anyone authenticated: list + show. There is no
per-tenant ownership of services or backends in v1; see
`docs/limitations.md` §3 for the productization story on RBAC.

Compatibility
-------------

This feature should be broadly compatible with existing ML2/OVN installations that
include ovn-agent and run currently-supported versions of OVN (26.03 is the test target).

### Hardware Offload

It is expected that local services work when OVS hardware offload is enabled but
will use the slow path and not be accelerated.

### Metadata Agent

This feature requires ovn-agent to be installed, but does not conflict with the
metadata agent if it is in use. The IP deny-list will include metadata IPs by default.

### OVN BGP / ovn-bgp-agent

BGP agent should not distribute routes for localport interfaces, so this should be
compatible without additional work.

Out of Scope -- Future Possibilities
------------------------------------
* ML2/OVS Implementation
* Backend rate limiting
* OVN LB exposure (requires OVN work to allow a LB to reach underlay backends)
* Octavia Amphora exposure
* Local Service Groups -- Related local services that can be enabled or disabled together
* Local Service / Group enablement on routers (inherited to connected networks)
* L7 handling of backend traffic (i.e. injecting instance or project IDs)
