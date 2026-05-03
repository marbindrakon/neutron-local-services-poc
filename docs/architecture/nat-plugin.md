# `nat` exposure plugin (Keepalived/ip_vs)

The `nat` plugin is the default exposure plugin. It runs Keepalived
inside the per-tenant `localsvc-<network>` netns, which programs the
kernel's `ip_vs` virtual-server table to forward tenant traffic to
real backends in NAT mode.

This page covers the plugin internals — how the config gets rendered,
how the process lifecycle works, how health checks are wired, and how
underlay-egress integration fits.

For the operator-facing decision guide see
[`../exposure-plugins.md`](../exposure-plugins.md).

For the system-level picture see
[`overview.md`](overview.md).

## Why Keepalived

Keepalived is the only userspace component the `nat` plugin
introduces, and it's been packaged in EPEL, Ubuntu main, Debian, and
every other major distro for two decades. Most ops teams already
have it on their approved binary list. That's the headline benefit
of picking `nat` — no new daemon to audit, no Rust toolchain in CI,
no novel attack surface.

Keepalived's data plane is kernel `ip_vs`. Forwarding doesn't go
through userspace — Keepalived only programs the kernel and runs
health checks. Throughput is bound by NIC and CPU, not by a proxy
process.

## Process model

One keepalived process per managed network, running inside that
network's `localsvc-<network>` netns. Spawned via privsep
(`ip netns exec localsvc-<net> keepalived -f conf -p pid -D`).

State on disk:

```
/var/lib/neutron-local-services/<network_uuid>/nat/
    keepalived.conf       — rendered config
    keepalived.pid        — keepalived's pidfile (written by it)
    config.hash           — sha256 of last applied config
```

`config.hash` lets the agent skip work when the rendered config
matches what's already running. On a real change the agent SIGHUPs
keepalived, which rebuilds `ip_vs` without dropping connections that
the new config still admits.

## Concurrency

A per-network `threading.Lock` protects `apply_config` against itself.
The agent fires reconcile from three places that can interleave on
startup:

- The Port_Binding CREATE event (when the localport materializes).
- The agent's startup `_periodic_reconcile()` after `sync()`.
- The 10s timer.

Without serialization, two callers see "no pidfile yet" (keepalived
takes ~1s to daemonize and another few seconds to write the pidfile
under selinux + privsep) and both spawn keepalived. `flock(2)` won't
help — Linux flock is per-OFD, so two `open()` calls in the same
process get distinct locks. A `threading.Lock` works because oslo
monkey-patches `threading` to be eventlet-aware.

`apply_config()` also waits for the pidfile after spawn (up to 30s
under selinux + privsep). Subsequent ticks see a real pid, hit the
hash match, and short-circuit.

## Config rendering

`render_keepalived_conf(network_id, services)` produces a config
with one `virtual_server` block per `(vip, port, proto)` tuple. A
service with `protocol=tcp-udp` produces two blocks. `lb_algo` maps
from the service's `distribution_policy`:

| `distribution_policy`   | Keepalived `lb_algo` |
| ----------------------- | -------------------- |
| `round-robin`           | `wrr`                |
| `least-connection`      | `wlc`                |
| `active-backup`         | `wrr` (PoC simplification — see [limitations](../limitations.md)) |

Each `real_server` block carries an explicit `connect_ip` so a
backend's `health_check_address` override actually probes the
override address (parity with Octavia's macros.j2).

Health checks render as one of:

- `TCP_CHECK` for `tcp`
- `HTTP_GET` for `http` (status code from `health_check_config`)
- `SSL_GET` for `https`
- `MISC_CHECK` running `check_dns.sh` for `dns`
- `MISC_CHECK` running `check_ntp.sh` for `ntp`
- (no HC block) for `none`

The `dns` and `ntp` probe scripts ship with the package under
`agent/plugins/check_scripts/` and follow Keepalived's `MISC_CHECK`
contract.

LVS mode is `NAT` (DR / tunnel don't fit our topology — VIPs are
link-local and on the netns's own veth).

## In-netns kernel knobs

The plugin's `_prepare_netns()` runs once per netns (idempotent on
reruns):

- `modprobe ip_vs` — explicit load so the failure mode is "modprobe
  failed" rather than "keepalived silently does nothing."
- `sysctl net.ipv4.vs.conntrack=1` — registers ip_vs with netfilter
  conntrack so the POSTROUTING MASQUERADE rule below applies cleanly
  to the rewritten packets. Without it, LVS-NAT only works when the
  realserver's default gateway is the director (fine for an operator
  who controls the realserver hosts, but not a generic assumption
  this codebase can make).
- `sysctl net.ipv4.ip_forward=1` — required for any forwarding.
- `iptables -t nat -A POSTROUTING -j MASQUERADE` — wide-open
  POSTROUTING MASQUERADE on the netns. The localsvc namespace only
  has the localport's veth and the underlay-egress veth, so a
  wide-open rule is fine — nothing else here to NAT.

## Underlay-egress integration

By default the netns has only its on-subnet route. For backends on
the chassis underlay (operator infrastructure), the agent provisions
a per-network underlay-egress veth pair (`nlsu<short>0` host /
`nlsu<short>1` ns) outside `br-int`, with a default route in the
netns and SNAT in the host root netns. Defense-in-depth iptables
rules close every escape path:

- Host-side per-network `NLS_UND_<short>` chain whitelists exactly
  the configured backend `(proto, addr, port)` tuples — refreshed on
  every catalog change. **This is the load-bearing tenant-escape
  gate.**
- Chassis-wide `-i nlsu+ -o nlsu+ -j DROP` blocks cross-tenant
  underlay cross-talk.
- `rp_filter = 1` on both ends of every `nlsu` veth.
- In-netns FORWARD permits the tenant→underlay flow plus the
  conntrack-tracked return path, with a catch-all DROP at the tail.

See [`overview.md`](overview.md#underlay-egress-defense-in-depth-against-tenant-escape)
for the full picture and [`limitations.md`](../limitations.md) §4
for what productization could still tighten.

## Diagnostics

Standard kernel-netfilter tooling works:

- `ip netns exec localsvc-<network> ipvsadm -ln` — live virtual
  server table, real server connection counts.
- `ip netns exec localsvc-<network> conntrack -L` — connection
  tracking.
- `journalctl -u neutron-ovn-agent -t local_services` — agent log
  filtered to the local-services extension.
- `cat /var/lib/neutron-local-services/<network>/nat/keepalived.conf`
  — currently-applied config.

## Failure modes

- **Backend HC fails** — keepalived prunes the realserver from the
  ip_vs virtual_server. With `quorum=1` (default) and zero healthy
  backends, the virtual_server is removed entirely; tenant traffic to
  the VIP gets ICMP destination-unreachable from the netns kernel.
- **keepalived crashes** — privsep reaper notices, but the agent
  doesn't actively restart it. Next `apply_config` (event or 10s
  timer) sees no pid and respawns.
- **netns destroyed without keepalived teardown** — keepalived is
  killed by netns deletion (no live netns to attach its sockets to).
  The agent's teardown event sequences plugin cleanup BEFORE netns
  destruction; if a tenant deletes their last binding, the closure
  is clean.
- **Operator points a backend at an unreachable address** — HC
  fails, the backend is excluded from the VS. The service stays up
  but with fewer healthy backends; if all backends are unreachable,
  the VS disappears and tenant traffic gets unreachable.

## When NOT to pick `nat`

- You want JSON access logs / per-flow observability →
  pick `proxy`.
- You need HC fidelity beyond `TCP_CHECK` / `MISC_CHECK` →
  pick `proxy`.
- You want a structural audit boundary between tenant and operator
  backends (compliance regimes) → pick `proxy`.

See [`../exposure-plugins.md`](../exposure-plugins.md) for the full
decision guide.
