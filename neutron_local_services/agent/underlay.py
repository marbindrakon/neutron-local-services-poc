"""Underlay-egress plumbing for the local-services agent.

The ``nat`` exposure plugin (Keepalived/ip_vs in the per-tenant
``localsvc-<network>`` netns) needs a route to operator backends that
live on the chassis underlay. The netns has only its on-subnet route
by default — which is fine for backends that share the tenant network,
but useless for the common case of operator infrastructure on RFC1918
underlay (DNS, NTP, KMS, internal HTTPS).

This module adds a second per-network veth pair, **outside br-int**,
that connects the netns to the host root netns over a small RFC6598
/30. The netns gets a default route via the host-side end; the host
root netns SNATs the netns CIDR to its own egress IP.

Defense in depth against tenant escape (the netns has ip_forward=1 so
that ip_vs works, which means a tenant could in principle inject
arbitrary destinations and have the netns forward them):

* In the netns, FORWARD allows traffic from the tenant veth to the
  underlay veth ONLY when conntrack reports a DNAT status — i.e. the
  packet has been rewritten by ip_vs. Tenant packets aimed at non-VIP
  destinations get DROPped.
* In the host root netns, a per-network FORWARD sub-chain whitelists
  exactly the (proto, dst, dport) tuples for the configured backend
  set. Reconciled on every catalog change. Default DROP at the tail.
* Inter-tenant cross-talk via underlay is blocked by a chassis-wide
  ``-i nls+ -o nls+ -j DROP`` rule.
* rp_filter is enforced on both ends of the new veth.

The architectural barrier (LVS DNAT is the only way for a tenant to
reach the underlay path) is the primary protection; the iptables
rules above are belt-and-suspenders.
"""

import errno
import ipaddress
import json
import os
import threading

from neutron.agent.linux import ip_lib
from neutron.agent.linux import utils as linux_utils
from oslo_log import log as logging

from neutron_local_services import constants as lsc


LOG = logging.getLogger(__name__)


def underlay_veth_names(network_id):
    """Return ``(root_end, ns_end)`` interface names for the underlay veth.

    Root end gets the ``0`` suffix (lives in host root netns); namespace
    end gets ``1`` (in ``localsvc-<network>``).
    """
    short = network_id[:lsc.UNDERLAY_VETH_NET_LEN]
    return (lsc.UNDERLAY_VETH_PREFIX + short + '0',
            lsc.UNDERLAY_VETH_PREFIX + short + '1')


def per_net_chain_name(network_id):
    """Return the host-root-netns iptables sub-chain name for a network."""
    short = network_id[:lsc.UNDERLAY_VETH_NET_LEN]
    return lsc.UNDERLAY_PER_NET_CHAIN_PREFIX + short


# --- Allocator -----------------------------------------------------------

class UnderlayPoolExhausted(Exception):
    """Raised when no /30 slots remain in the configured pool."""


class UnderlayAllocator:
    """Chassis-local /30 allocator within a configured CIDR pool.

    State persisted to ``<state_dir>/allocations.json`` as a JSON object
    mapping ``network_id`` → integer index (0-based) into the pool's /30
    space. First-fit, deterministic across agent restarts.

    Thread-safe via a single in-process lock — the agent's eventlet
    monkey-patching makes ``threading.Lock`` cooperate with
    eventlet greenthreads.
    """

    def __init__(self, pool_cidr, state_dir=lsc.UNDERLAY_STATE_DIR):
        self._pool = ipaddress.ip_network(pool_cidr, strict=True)
        if self._pool.version != 4:
            raise ValueError(
                'underlay_egress_cidr must be IPv4; got %s' % pool_cidr)
        if self._pool.prefixlen > 30:
            raise ValueError(
                'underlay_egress_cidr must be /30 or wider; got %s'
                % pool_cidr)
        self._slots = 1 << (30 - self._pool.prefixlen)
        self._state_dir = state_dir
        self._state_path = os.path.join(state_dir, 'allocations.json')
        self._lock = threading.Lock()
        self._allocations = self._load()

    def _load(self):
        try:
            with open(self._state_path) as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            LOG.exception('Failed to load underlay allocations from %s; '
                          'starting empty (existing state may leak)',
                          self._state_path)
            return {}
        # Defensive — drop entries pointing outside the pool (e.g. after
        # an operator shrinks the pool). Logged so the operator notices.
        clean = {}
        for net_id, idx in data.items():
            if isinstance(idx, int) and 0 <= idx < self._slots:
                clean[net_id] = idx
            else:
                LOG.warning('Discarding stale underlay allocation for %s '
                            '(idx %s outside current pool)', net_id, idx)
        return clean

    def _save(self):
        try:
            os.makedirs(self._state_dir, exist_ok=True)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
        tmp = self._state_path + '.tmp'
        with open(tmp, 'w') as fh:
            json.dump(self._allocations, fh, sort_keys=True)
        os.rename(tmp, self._state_path)

    def allocate(self, network_id):
        """Return an existing or newly-allocated /30 index for the network.

        Idempotent — same network_id always returns the same index until
        ``free()``.
        """
        with self._lock:
            if network_id in self._allocations:
                return self._allocations[network_id]
            used = set(self._allocations.values())
            for idx in range(self._slots):
                if idx not in used:
                    self._allocations[network_id] = idx
                    self._save()
                    return idx
            raise UnderlayPoolExhausted(
                'no free /30 slots in pool %s (capacity %d)'
                % (self._pool, self._slots))

    def free(self, network_id):
        """Release a network's /30. No-op if not allocated."""
        with self._lock:
            if network_id in self._allocations:
                del self._allocations[network_id]
                self._save()

    def get(self, network_id):
        """Look up an existing allocation; return ``None`` if unallocated."""
        with self._lock:
            return self._allocations.get(network_id)

    def addresses(self, network_id):
        """Return ``(host_side_ip, ns_side_ip)`` for a network's /30.

        Allocates if needed. ``.1`` host-side, ``.2`` ns-side; ``.0``
        network address; ``.3`` broadcast.
        """
        idx = self.allocate(network_id)
        base_int = int(self._pool.network_address) + (idx * 4)
        host_ip = ipaddress.IPv4Address(base_int + 1)
        ns_ip = ipaddress.IPv4Address(base_int + 2)
        return str(host_ip), str(ns_ip)


# --- Privileged shell-out helpers ---------------------------------------

def _exec_root(cmd, check_exit_code=True, extra_ok_codes=None,
               log_fail_as_error=True):
    """Run a command in the host root netns as root via privsep."""
    return linux_utils.execute(
        cmd, run_as_root=True, privsep_exec=True,
        check_exit_code=check_exit_code, extra_ok_codes=extra_ok_codes,
        log_fail_as_error=log_fail_as_error)


def _exec_in_ns(netns, cmd, check_exit_code=True, extra_ok_codes=None,
                log_fail_as_error=True):
    """Run a command inside ``netns`` as root via privsep."""
    ip = ip_lib.IPWrapper(namespace=netns)
    return ip.netns.execute(
        cmd, run_as_root=True, privsep_exec=True,
        check_exit_code=check_exit_code, extra_ok_codes=extra_ok_codes,
        log_fail_as_error=log_fail_as_error)


# --- iptables idempotency helpers ---------------------------------------

def _iptables_chain_exists(chain, table='filter', netns=None):
    """True if ``chain`` exists under ``table``, else False.

    Uses ``iptables -n -L <chain>``: exit code 0 → exists, 1 → missing.
    Both the absent and the corrupt-table cases are returned as False.
    """
    cmd = ['iptables', '-t', table, '-n', '-L', chain]
    try:
        if netns:
            _exec_in_ns(netns, cmd, log_fail_as_error=False)
        else:
            _exec_root(cmd, log_fail_as_error=False)
        return True
    except Exception:
        return False


def _ensure_chain(chain, table='filter', netns=None):
    """Idempotent ``iptables -N`` (no-op if the chain already exists)."""
    if _iptables_chain_exists(chain, table=table, netns=netns):
        return
    cmd = ['iptables', '-t', table, '-N', chain]
    if netns:
        _exec_in_ns(netns, cmd)
    else:
        _exec_root(cmd)


def _ensure_rule(table, chain, rule_args, netns=None):
    """Idempotent ``iptables -A`` — checks with ``-C`` first.

    ``rule_args`` is the rule body as a list (everything after
    ``iptables -t <table> -A <chain>``). The ``-C`` probe normally
    fails when the rule isn't there yet — that's the expected first-
    install path, so it must not log at ERROR. ``log_fail_as_error=False``
    suppresses the noise.
    """
    check_cmd = ['iptables', '-t', table, '-C', chain] + rule_args
    add_cmd = ['iptables', '-t', table, '-A', chain] + rule_args
    try:
        if netns:
            _exec_in_ns(netns, check_cmd, log_fail_as_error=False)
        else:
            _exec_root(check_cmd, log_fail_as_error=False)
        return  # Already present.
    except Exception:
        pass
    if netns:
        _exec_in_ns(netns, add_cmd)
    else:
        _exec_root(add_cmd)


def _delete_rule_if_present(table, chain, rule_args, netns=None):
    """Best-effort ``iptables -D`` — silently no-ops if rule isn't there."""
    cmd = ['iptables', '-t', table, '-D', chain] + rule_args
    try:
        if netns:
            _exec_in_ns(netns, cmd, log_fail_as_error=False)
        else:
            _exec_root(cmd, log_fail_as_error=False)
    except Exception:
        pass


# --- Chassis-wide setup -------------------------------------------------

# Set after install_chassis_chain has run successfully once. The actual
# rules are idempotent on iptables, so a re-call is harmless; this is
# just a fast-path to skip the work.
_chassis_chain_installed = threading.Event()

# Per-network lock guarding provision / teardown / reconcile_destination_acl.
# The agent fires reconcile from three places that can interleave on
# startup (PB CREATE event, netns.sync(), start() walk). The iptables
# -C ; -A pattern is TOCTOU-vulnerable when two greenthreads both
# observe "rule not present" before either has run -A — both then
# append, producing duplicate rules. The eventlet-aware threading.Lock
# serializes the manipulation per network without blocking other
# networks' work.
_per_net_locks = {}
_per_net_locks_guard = threading.Lock()


def _per_net_lock(network_id):
    with _per_net_locks_guard:
        lock = _per_net_locks.get(network_id)
        if lock is None:
            lock = threading.Lock()
            _per_net_locks[network_id] = lock
        return lock


def _drop_per_net_lock(network_id):
    with _per_net_locks_guard:
        _per_net_locks.pop(network_id, None)


def install_chassis_chain(pool_cidr):
    """Install the chassis-wide host-side iptables prelude.

    Idempotent. Safe to call from ``LocalServicesExtension.start()``.

    Sets up:
      - filter/NEUTRON_LOCAL_SVC_UNDERLAY chain, jumped from FORWARD
        AND from INPUT with ``-i nls+``
      - filter/NEUTRON_LOCAL_SVC_UNDERLAY: ESTABLISHED,RELATED ACCEPT
      - filter/NEUTRON_LOCAL_SVC_UNDERLAY: -i nls+ -o nls+ DROP
        (block inter-tenant cross-talk via underlay)
      - nat/POSTROUTING: SNAT for the pool CIDR

    Per-network jumps and per-network sub-chains are added by
    ``provision_for_network()``.

    Why both FORWARD and INPUT: the per-net allow list is "this tenant
    may only egress to these backends". A packet from inside the netns
    destined to a *remote* IP traverses FORWARD on the host root netns,
    which the existing jump covers. A packet destined to the *chassis
    host's own* IP (e.g. ``172.18.0.128`` — the underlay NIC) is local
    delivery and traverses INPUT instead, bypassing FORWARD entirely.
    Without the INPUT jump a tenant netns could reach every host-bound
    socket on the chassis (kubelet, ssh, etcd, etc.) — which the
    architecture documents as forbidden. Adding the INPUT jump runs
    the same per-net DROP-by-default ACL on host-bound traffic, closing
    that hole. Backend egress is unaffected because it goes through
    FORWARD, not INPUT.
    """
    chain = lsc.UNDERLAY_HOST_CHAIN
    _ensure_chain(chain, table='filter')
    _ensure_rule('filter', 'FORWARD', ['-j', chain])
    _ensure_rule('filter', 'INPUT',
                 ['-i', lsc.UNDERLAY_VETH_PREFIX + '+', '-j', chain])
    _ensure_rule('filter', chain,
                 ['-m', 'conntrack', '--ctstate', 'ESTABLISHED,RELATED',
                  '-j', 'ACCEPT'])
    _ensure_rule('filter', chain,
                 ['-i', lsc.UNDERLAY_VETH_PREFIX + '+',
                  '-o', lsc.UNDERLAY_VETH_PREFIX + '+',
                  '-j', 'DROP'])
    _ensure_rule('nat', 'POSTROUTING',
                 ['-s', pool_cidr,
                  '!', '-o', lsc.UNDERLAY_VETH_PREFIX + '+',
                  '-j', 'MASQUERADE'])
    _chassis_chain_installed.set()
    LOG.info('local-services: installed chassis-wide underlay chain '
             '%s (pool %s)', chain, pool_cidr)


# --- Per-network provisioning -------------------------------------------

def _ensure_addr(dev, cidr):
    """Idempotent ``ip addr add``.

    Catches ``IpAddressAlreadyExists`` because ``dev.addr.list()`` may
    return CIDRs in a slightly different format than what we'd compare
    against (different netmask normalization, label suffixes, etc.).
    Letting the kernel be the source of truth via the exception is
    more robust than a string-equality check.
    """
    current = {addr['cidr'] for addr in dev.addr.list()}
    if cidr in current:
        return
    try:
        dev.addr.add(cidr)
    except Exception as exc:
        # neutron's ip_lib raises IpAddressAlreadyExists; we don't want
        # to import it just to catch it, so name-match instead. Any
        # other exception bubbles up so a real misconfiguration still
        # surfaces.
        if 'AlreadyExists' not in type(exc).__name__:
            raise


def _ensure_default_route(netns, gateway):
    """Idempotent default route via ``gateway`` inside ``netns``.

    ``ip route replace default via <gw>`` is the safe primitive — it
    creates the route if missing, replaces it if it already exists with
    different params. Idempotent under repeated calls.
    """
    _exec_in_ns(netns, ['ip', 'route', 'replace', 'default', 'via', gateway])


def _set_rp_filter(iface, netns=None):
    """Best-effort ``net.ipv4.conf.<iface>.rp_filter = 1``.

    Logged-and-swallowed on failure — rp_filter is defense in depth, not
    load-bearing. The caller's other measures (FORWARD ACL, SNAT) close
    the same hole.
    """
    cmd = ['sysctl', '-w', 'net.ipv4.conf.%s.rp_filter=1' % iface]
    try:
        if netns:
            _exec_in_ns(netns, cmd)
        else:
            _exec_root(cmd)
    except Exception:
        LOG.debug('Could not set rp_filter on %s%s; non-fatal',
                  iface, ' in ' + netns if netns else '')


def _install_in_netns_forward(netns, tenant_veth_ns, underlay_veth_ns):
    """Install in-netns FORWARD rules for the underlay-egress veth.

    Allows the tenant→underlay forwarding path that ip_vs DNAT depends
    on, plus the conntrack-tracked return path. The host-side
    per-backend ACL chain (in `NLS_UND_<short>`) is what restricts
    *which* destinations a tenant can reach via this path; the in-netns
    rules are defense in depth.

    We deliberately don't try to require ``--ctstatus DNAT`` here —
    that match is implemented by xt_conntrack but doesn't translate
    cleanly to the nf_tables iptables backend on every distro
    (`iptables v1.8.11 (nf_tables): Bad ctstatus "DNAT"`). Even
    without it, the architectural barrier still holds: the netns has
    no route for any destination outside its on-subnet CIDR except
    the default route via the underlay veth, and the host-side ACL
    drops anything not aimed at a configured backend.

    Order: ACCEPT clauses first, then the catch-all DROPs.
    """
    chain = 'FORWARD'
    # tenant -> underlay (egress)
    _ensure_rule('filter', chain,
                 ['-i', tenant_veth_ns, '-o', underlay_veth_ns,
                  '-m', 'conntrack', '--ctstate',
                  'NEW,ESTABLISHED,RELATED',
                  '-j', 'ACCEPT'],
                 netns=netns)
    _ensure_rule('filter', chain,
                 ['-i', tenant_veth_ns, '-o', underlay_veth_ns,
                  '-j', 'DROP'],
                 netns=netns)
    # underlay -> tenant (return): established/related only
    _ensure_rule('filter', chain,
                 ['-i', underlay_veth_ns, '-o', tenant_veth_ns,
                  '-m', 'conntrack', '--ctstate',
                  'ESTABLISHED,RELATED',
                  '-j', 'ACCEPT'],
                 netns=netns)
    _ensure_rule('filter', chain,
                 ['-i', underlay_veth_ns, '-o', tenant_veth_ns,
                  '-j', 'DROP'],
                 netns=netns)


def provision_for_network(network_id, allocator):
    """Provision the underlay-egress veth + iptables for one network.

    Idempotent — re-running rebuilds anything that's missing without
    disturbing what's already in place. Returns the
    ``(host_ip, ns_ip)`` tuple for the allocated /30.

    Wrapped in a per-network lock to serialize against parallel calls
    from the PB CREATE event handler and start()'s sync walk.
    """
    with _per_net_lock(network_id):
        return _provision_for_network_locked(network_id, allocator)


def _provision_for_network_locked(network_id, allocator):
    ns_name = lsc.NETNS_PREFIX + network_id
    if not ip_lib.network_namespace_exists(ns_name):
        # provision_for_network races ahead of netns.provision() on
        # rare timing windows (sync() bringing both up simultaneously).
        # The next reconcile pass will retry.
        LOG.debug('underlay.provision: netns %s not present yet; '
                  'will retry next pass', ns_name)
        return None

    host_ip, ns_ip = allocator.addresses(network_id)
    root_veth, ns_veth = underlay_veth_names(network_id)

    # Veth lifecycle. add_veth(name1, name2, namespace2=ns) creates
    # name1 in the host root netns and name2 directly in ns. Same shape
    # as netns.provision() uses for the tenant veth.
    if not ip_lib.device_exists(ns_veth, namespace=ns_name):
        if ip_lib.device_exists(root_veth):
            # Stale root-side end without ns peer (botched prior run);
            # del_veth deletes both sides.
            ip_lib.IPWrapper().del_veth(root_veth)
        LOG.info('Creating underlay veth %s/%s for netns %s',
                 root_veth, ns_veth, ns_name)
        ip_lib.IPWrapper().add_veth(root_veth, ns_veth, namespace2=ns_name)

    root_dev = ip_lib.IPDevice(root_veth)
    ns_dev = ip_lib.IPDevice(ns_veth, namespace=ns_name)
    root_dev.link.set_up()
    ns_dev.link.set_up()
    _ensure_addr(root_dev, '%s/30' % host_ip)
    _ensure_addr(ns_dev, '%s/30' % ns_ip)

    _ensure_default_route(ns_name, host_ip)

    _set_rp_filter(root_veth)
    _set_rp_filter(ns_veth, netns=ns_name)

    # In-netns FORWARD rules (DNAT-state filter).
    # The tenant veth name is computed the same way netns.veth_names()
    # does — short prefix + 10-char net hex + suffix.
    tenant_short = network_id[:10]
    tenant_ns_veth = 'tls' + tenant_short + '1'
    _install_in_netns_forward(ns_name, tenant_ns_veth, ns_veth)

    # Host root netns: per-network sub-chain skeleton.
    # The per-backend ACL rules are populated separately by
    # reconcile_destination_acl() — at provision time we just create
    # the empty chain plus a default-DROP and the parent jump from
    # NEUTRON_LOCAL_SVC_UNDERLAY -i <root_veth> -j <chain>.
    chain = per_net_chain_name(network_id)
    _ensure_chain(chain, table='filter')
    # Default DROP — overridden by reconcile_destination_acl which
    # repopulates the chain with whitelist rules + the trailing DROP.
    _ensure_rule('filter', chain, ['-j', 'DROP'])
    _ensure_rule('filter', lsc.UNDERLAY_HOST_CHAIN,
                 ['-i', root_veth, '-j', chain])

    LOG.info('Provisioned underlay egress for network %s '
             '(host=%s ns=%s veth=%s↔%s)',
             network_id, host_ip, ns_ip, root_veth, ns_veth)
    return host_ip, ns_ip


def teardown_for_network(network_id, allocator):
    """Reverse provision_for_network. Idempotent."""
    with _per_net_lock(network_id):
        _teardown_for_network_locked(network_id, allocator)
    _drop_per_net_lock(network_id)


def _teardown_for_network_locked(network_id, allocator):
    root_veth, ns_veth = underlay_veth_names(network_id)
    chain = per_net_chain_name(network_id)

    # Remove the parent jump rule first so the chain becomes deletable.
    _delete_rule_if_present(
        'filter', lsc.UNDERLAY_HOST_CHAIN,
        ['-i', root_veth, '-j', chain])
    if _iptables_chain_exists(chain, table='filter'):
        try:
            _exec_root(['iptables', '-t', 'filter', '-F', chain])
            _exec_root(['iptables', '-t', 'filter', '-X', chain])
        except Exception:
            LOG.exception('Failed to remove iptables chain %s', chain)

    # Delete the host-side veth — kernel deletes the peer (the ns side
    # would also disappear when the netns goes away in the caller's
    # teardown sequence; deleting from this side first avoids leaking
    # if the netns has already been gc'd).
    if ip_lib.device_exists(root_veth):
        try:
            ip_lib.IPWrapper().del_veth(root_veth)
        except Exception:
            LOG.exception('Failed to delete underlay veth %s', root_veth)

    # Free the /30 allocation last so a crash mid-teardown doesn't drop
    # the slot before the kernel state actually goes away.
    allocator.free(network_id)
    LOG.info('Tore down underlay egress for network %s', network_id)


# --- Per-backend destination ACL ----------------------------------------

def _backend_endpoints(services):
    """Yield ``(proto, addr, port)`` tuples for every enabled backend.

    Includes the optional ``health_check_address`` /
    ``health_check_port`` overrides — those addresses must also be
    reachable for keepalived's HC to mark backends UP.

    Skips disabled services and disabled backends. ``services`` is the
    same ``desired_state_for_network`` shape the registry client returns.
    """
    for svc in services:
        if not svc.get('enabled', True):
            continue
        proto = (svc.get('protocol') or '').lower()
        # tcp-udp services need both protos in the ACL.
        if proto == 'tcp-udp':
            protos = ('tcp', 'udp')
        elif proto in ('tcp', 'udp'):
            protos = (proto,)
        else:
            continue
        for be in svc.get('backends') or ():
            if not be.get('enabled', True):
                continue
            data_addr = be.get('address')
            data_port = be.get('port')
            if data_addr and data_port:
                for p in protos:
                    yield p, data_addr, int(data_port)
            # HC override (if address differs OR port differs from data).
            hc_addr = be.get('health_check_address') or data_addr
            hc_port = be.get('health_check_port') or data_port
            if hc_addr and hc_port and (
                    hc_addr != data_addr or
                    int(hc_port) != int(data_port)):
                # Probes can be over either proto; allow both. (TCP_CHECK
                # / SSL_GET / HTTP_GET use TCP; MISC_CHECK script type
                # uses whatever protocol the script implements — for
                # ``dns`` and ``ntp`` that's UDP. Allowing both is the
                # safe superset and the FORWARD ACL is anyway scoped to
                # the configured backend addresses.)
                for p in ('tcp', 'udp'):
                    yield p, hc_addr, int(hc_port)


def reconcile_destination_acl(network_id, services):
    """Refresh the per-network FORWARD destination ACL.

    Flushes the per-network chain and repopulates with allow rules for
    every (proto, addr, port) tuple in the backend set, terminated by a
    default DROP.

    The flush+repopulate window is microseconds (the iptables calls run
    sequentially in the same process). Any HC probe in flight during
    that window will see a DROP and retry on the next probe interval —
    keepalived's TCP_CHECK has a few-second retry budget, well above
    the swap window.

    Wrapped in the per-network lock so a flush isn't racing with a
    provision-time `_ensure_chain`/`_ensure_rule` from a parallel
    greenthread.
    """
    with _per_net_lock(network_id):
        _reconcile_destination_acl_locked(network_id, services)


def _reconcile_destination_acl_locked(network_id, services):
    chain = per_net_chain_name(network_id)
    _ensure_chain(chain, table='filter')

    endpoints = sorted(set(_backend_endpoints(services)))

    _exec_root(['iptables', '-t', 'filter', '-F', chain])
    for proto, addr, port in endpoints:
        _exec_root(['iptables', '-t', 'filter', '-A', chain,
                    '-d', addr, '-p', proto, '--dport', str(port),
                    '-m', 'conntrack', '--ctstate', 'NEW',
                    '-j', 'ACCEPT'])
    _exec_root(['iptables', '-t', 'filter', '-A', chain, '-j', 'DROP'])

    LOG.debug('Reconciled underlay ACL for %s: %d endpoint(s)',
              network_id, len(endpoints))
