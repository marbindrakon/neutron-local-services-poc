# `proxy` exposure plugin (Rust userspace L4 proxy)

The `proxy` plugin terminates tenant connections at a userspace
proxy daemon and re-originates them to operator backends. Where the
`nat` plugin's value proposition is "no new binary," the `proxy`
plugin's is "rich health checks, per-flow observability, and a clear
audit boundary between tenant and operator network domains."

This page covers the daemon's process model, the trust split, the
catalog distribution machinery, and the per-tenant isolation
properties.

For the operator-facing decision guide see
[`../exposure-plugins.md`](../exposure-plugins.md).

For the system-level picture see [`overview.md`](overview.md).

## Two binaries — privilege split

The proxy plugin ships two Rust binaries that run as a paired set:

```
nls-proxy-priv  ~340 LOC  CAP_SYS_ADMIN + CAP_NET_BIND_SERVICE +
                          CAP_CHOWN + CAP_DAC_READ_SEARCH
                          (only privileged process; one short-lived
                           thread per bind request that setns()es,
                           binds, returns the fd via SCM_RIGHTS)

nls-proxy       worker    zero capabilities; #![forbid(unsafe_code)]
                          systemd hardening: ProtectKernelTunables,
                          ProtectKernelModules, ProtectKernelLogs,
                          RestrictNamespaces=net, LockPersonality,
                          MemoryDenyWriteExecute, NoNewPrivileges
                          per-tenant tokio current-thread runtimes
```

Trust split:

- **`nls-proxy-priv`** (the helper) is small, audited, has only the
  capabilities required to enter a tenant netns and bind low-numbered
  ports. For each `BindListener` request it spawns a fresh thread
  that `setns()`es into the requested netns, performs `socket /
  bind / listen`, sends the bound fd back via `SCM_RIGHTS`, and
  exits. The helper's main thread never `setns()`es.
- **`nls-proxy`** (the worker) has zero capabilities, is sandboxed,
  and runs forever in the host root netns. It receives bound listener
  fds from the helper. Linux preserves netns binding on the socket,
  so reads on a tenant-bound listener fd see tenant traffic even
  though the worker thread itself is in host netns. Backend
  `connect()` from the worker uses the host netns route table.

The worker imports no `setns` ffi. The only `setns()` caller in the
project is the priv helper, and only on freshly spawned one-shot
threads that exit after the bind completes — no thread pooling, no
async tasks crossing namespaces.

This means the worker process **never has to re-enter a tenant
netns**. Established connection forwarding (`tokio::io::copy_bidirectional`
between two fds) works on any thread regardless of netns: the kernel
routes per-socket based on each socket's bound netns, not the calling
thread's current netns.

## Per-tenant tokio runtime

Each managed tenant netns gets its own OS thread running a
`tokio::runtime::Builder::new_current_thread()` runtime. That thread
holds:

- An `Arc<TenantSlice>` from the catalog watcher.
- A `watch::Receiver<BackendStatus>` for HC reads.
- The TCP and UDP listener fds for this tenant's services.
- The UDP session table — a thread-local `HashMap<(client_addr,
  client_port), Session>` with no `Mutex`, no `DashMap`.

It runs:

- One accept task per TCP listener.
- One recv task per UDP listener.
- Forwarding tasks (`copy_bidirectional` for TCP; per-session for
  UDP).
- A catalog-reload task that swaps configuration when the watch
  channel fires.

**Cross-tenant isolation is structural, not bookkept.** Tenant
threads share only:

- The catalog snapshot (`Arc<Catalog>`, immutable per version,
  swapped via watch on update — readers cannot affect each other or
  the writer).
- The HC backend-status snapshot (immutable per probe round, swapped
  via watch — HC writes; tenants read).
- The priv-helper RPC client (used only at bind/teardown time; not
  on the data path).

There is no shared mutable session table, no shared connection
pool, no shared LRU, no per-tenant bookkeeping the worker has to
keep track of. A bug in tenant T1's thread cannot corrupt T2's state
because T2's state is in a different thread's stack and heap, with
no pointer reachability between them.

A separate watchdog thread walks `/proc/self/task/<tid>/ns/net`
every second and asserts every worker tid's netns inode matches the
host netns inode captured at startup. Mismatch is a hard invariant
violation (impossible by design — the worker doesn't even import
`setns`); the watchdog logs at error level and exits, letting
systemd restart the worker fresh.

Thread-count and memory budget for ~hundreds of tenant netns: 200
tenants × (1 data-path thread) + 4 service threads ≈ 204 threads.
Each tokio current-thread runtime is ~16 KB stack + small runtime
overhead — under 4 MB total.

## Catalog distribution

The agent writes a chassis-wide JSON catalog file
(`/var/lib/neutron-local-services/_proxy/catalog.json`,
HMAC-SHA256-signed with a per-boot key) that the worker watches via
inotify (`IN_MOVED_TO` on the directory).

Catalog entry shape:

```json
{
  "net_id": "<uuid>",
  "service_id": "<uuid>",
  "vip": "169.254.169.200",
  "port": 53,
  "proto": "udp",
  "backends": [
    {"addr": "172.18.42.10", "port": 53, "weight": 1}
  ],
  "health_check": {
    "type": "udp_dns" | "tcp" | "http" | "https" | "script",
    "interval_s": 5, "timeout_s": 2,
    "fail_after": 3, "rise_after": 2,
    "script_path": "...", "script_args": ["-q", "example.com"]
  },
  "lb_algo": "wrr",
  "max_concurrent": 1000,
  "max_session_idle_s": 60
}
```

The agent writes `catalog.json.tmp` then `rename(2)` for atomicity;
the worker watches `IN_MOVED_TO`. Catalog has a monotonic generation
counter plus HMAC over a canonical-serialized payload using a
per-boot key (`hmac.key`, mode 0400, owned by `stack`).

On parse error or HMAC mismatch the worker:

- Keeps its previous good `Catalog` published (live listeners stay
  up).
- Increments a counter exposed on `/metrics`.
- Logs the failure with `tracing::error!`.

The worker does **not** tear down listeners on a corrupt catalog —
that would convert a transient catalog write bug into a tenant
outage.

## Boot-id sentinel

The worker writes `worker.boot` (timestamp + pid) atomically at
startup. The agent reads this file on every reconcile and clears its
"already-registered netns" cache when the value changes — this is
how the agent detects worker restarts and re-issues `AddNetns` for
every managed network. Without it, a worker restart wipes the
per-tenant data-path threads but the agent's cache still says
"registered," and the catalog stays loaded with HC reporting HEALTHY
but zero sockets bound (the "worked briefly, stopped" failure
pattern).

## Admin endpoint

Over a unix socket at
`/var/run/neutron-local-services/_proxy/admin.sock`, mode 0600,
group `nls-admin` (the agent's stack user is in this supplementary
group). All routes require a per-boot bearer token from a 0400 file:

- `GET /healthz` — liveness probe.
- `GET /clusters?format=json` — Envoy-shape JSON of every cluster +
  endpoint health. The agent's `get_backend_health()` parses this.
- `GET /listeners` — per-listener concurrency counters.
- `GET /metrics` — Prometheus-format aggregate metrics.

The Envoy-shape `/clusters` JSON means existing scrapers work
unchanged. The format is documented at
<https://www.envoyproxy.io/docs/envoy/latest/operations/admin#get--clusters>.

## Health-check engine

A single dedicated `tokio::runtime` thread runs all HC probes across
all tenants. Probe types:

- `tcp_connect` — connect within `timeout_s`.
- `http_get` — status code match against `health_check_config`.
- `https_get` — TLS connect + HTTP GET, status-code match. Mirrors the
  `nat` plugin's keepalived `SSL_GET`. Cert verification is skipped
  (matches keepalived's default) so self-signed and internal-CA
  backends work without operator-supplied trust anchors. SNI is
  optional via the `sni` field.
- `udp_dns_query` — built-in DNS A query for a configurable name.
- `script` — runs Keepalived `MISC_CHECK`-compatible scripts. Reuses
  the same `agent/plugins/check_scripts/` directory the `nat`
  plugin uses (env vars `BACKEND_ADDR` / `BACKEND_PORT` plus the same
  values as positional args). One script works under both plugins.

Per-backend consecutive-fail and consecutive-rise thresholds
(`fail_after` / `rise_after`). Each probe round produces a fresh
`Arc<HashMap<BackendId, Status>>` published via a `tokio::sync::watch`
channel. State transitions log via `tracing` and surface on
`/clusters`.

HC probes originate from the host root netns (the HC thread's
netns), so HC reaches whatever the chassis can reach — same path the
worker uses for backend connect.

## Resource bounds

Per-`(net_id, vip, port)` quotas, enforced inside the owning tenant
thread (no cross-tenant counter sharing):

- `max_concurrent` connections (TCP).
- `max_session_idle_s` UDP session idle eviction.
- Accept rate cap.

Per-`net_id` aggregate is structurally that thread's cap. Global cap
enforced by the dispatcher refusing to spawn more tenant threads
above a configured limit.

UDP host-side ephemeral sockets are allocated from a chassis-reserved
range via `IP_LOCAL_PORT_RANGE` (sysctl
`net.ipv4.ip_local_reserved_ports` set by the DevStack plugin) so
chassis-wide ephemeral exhaustion is contained.

## Wire format (priv ↔ worker ↔ agent)

`nls-proxy-scm` is the shared SCM_RIGHTS sender/receiver crate.
Frame layout: 4-byte big-endian length prefix, then JSON payload.
SCM_RIGHTS file descriptors travel on a separate `sendmsg()` call
from the prefix to satisfy Linux's `unix(7)` ancillary-data
semantics.

`nls-proxy-wire` defines the JSON RPC message types:

- `BindListener(net_id, vip, port, proto) + netns_fd → listener_fd`
  (priv ↔ worker; priv consults the agent-signed catalog and refuses
  any `(net_id, vip, port, proto)` that isn't an entry, then verifies
  the catalog's nonce inside the tenant netns before binding)
- `AddNetns(net_id) + netns_fd` (agent → worker; the agent opens
  `/run/netns/<localsvc-net_id>` directly — the priv helper is no
  longer in this path)
- `RemoveNetns(net_id)` (agent → worker)

## Failure modes

- **Worker SIGKILL.** Systemd restarts in ~2s. In-flight TCP
  connections drop. New TCP succeeds within ~5s. UDP sessions
  re-establish on the next datagram. Boot-id sentinel triggers
  agent re-registration.
- **Priv helper SIGKILL.** Existing listeners stay up (the worker
  holds the fds). New bind operations pause until the helper
  restarts; the agent's reconcile retries on its 10s timer.
- **Catalog corruption.** Worker keeps last-good state, surfaces the
  error on `/metrics`, doesn't tear down listeners.
- **HC reports a backend down.** That backend is excluded from
  selection on the next forwarding decision. State surfaces on
  `/clusters`.
- **Tenant netns goes away.** Agent sends `CloseNetns(fd)`; worker
  drains connections on that thread, frees per-net state, exits the
  thread.

## Why Rust

- Long-lived privileged daemon holding kernel resources (raw fds,
  netns handles, two socket families) — memory safety matters.
- Strong fd-ownership story (`OwnedFd`, `BorrowedFd`) prevents
  reuse-after-close bugs that bite in C and are subtle in Go.
- Mature ecosystem: `tokio` (async I/O), `nix` (setns / socket /
  SCM_RIGHTS), `serde` + `serde_json` (catalog), `notify` (inotify),
  `tracing` (structured logs), `axum` (admin), `hmac` + `sha2`
  (catalog HMAC), `caps` (drop capabilities). No `DashMap` or
  `Arc<Mutex<>>` on the data path — per-tenant thread isolation
  makes all session state thread-local.
- Worker crate is `#![forbid(unsafe_code)]`. The only `unsafe` block
  in the project lives in the priv helper's `setns` ffi wrapper
  (via `nix`).

## When to pick `proxy`

- You want HC fidelity (Envoy-shape `/clusters`, structured JSON
  state).
- You want per-flow observability (Prometheus metrics, idle eviction
  counters, accept-rate counters).
- You want a clear bytes-in / bytes-out audit boundary for
  compliance.
- You want structural cross-tenant isolation guaranteed by the
  thread/heap layout, not by lock discipline.

See [`../exposure-plugins.md`](../exposure-plugins.md) for the full
decision guide.

## When NOT to pick `proxy`

- The operator wants the smallest possible audit footprint —
  `nat`'s single keepalived binary is hard to beat.
- The deployment can't ship Rust binaries through its existing build
  pipeline.
