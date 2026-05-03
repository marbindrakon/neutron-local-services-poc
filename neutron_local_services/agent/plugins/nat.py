"""NAT exposure plugin — Keepalived/ip_vs.

Keepalived in the per-tenant netns drives kernel ip_vs NAT-mode
forwarding. The plugin name `nat` reflects the operator choice:
pick `nat` for raw throughput and a single, well-known userspace
binary; pick `proxy` for richer health checks and per-flow
observability.


How it fits together:

* Each managed network already has a netns ``localsvc-<network_id>``
  with the localport's veth, the on-subnet IP (provision()), and any
  /32 VIPs (reconcile_vips()). The kernel inside the netns ARP-responds
  for the VIPs, so guest packets land on the tap.

* This plugin spawns Keepalived in the netns and feeds it a config that
  configures kernel ip_vs to NAT-forward the (vip, port, proto) tuples
  to a list of real servers. Keepalived also drives health checks
  (TCP_CHECK / HTTP_GET / SSL_GET / MISC_CHECK) and prunes dead
  backends from the ipvs table.

* LVS-NAT requires reply traffic to traverse the director, so we add an
  iptables MASQUERADE rule on the netns-side veth in POSTROUTING. With
  the kernel's net.ipv4.vs.conntrack=1 sysctl, LVS registers its
  connection tracking with netfilter, so MASQUERADE plays nicely with
  ip_vs DNAT. Without this, LVS-NAT only works when the realserver's
  default gateway is the director — fine for an operator who controls
  the realserver hosts, but not for a PoC that points at tenant VMs or
  same-host backends.

Process lifecycle:

* ``apply_config()`` writes ``keepalived.conf`` under the per-network
  state dir, then either starts keepalived (no PID file or stale PID)
  or SIGHUP's it (config changed). Keepalived's SIGHUP rebuilds the
  ipvs table without dropping connections it can preserve.

* ``teardown()`` SIGTERMs keepalived (best-effort — the agent destroys
  the netns next, which kills any leftover process), then removes the
  state dir. Idempotent.

State on disk:
    /var/lib/neutron-local-services/<network_id>/lvs/
        keepalived.conf       — rendered config
        keepalived.pid        — keepalived's pidfile (it writes this)
        config.hash           — sha256 of last applied config (skip-noop)
"""

import hashlib
import os
import shutil
import threading
import time

from neutron.agent.linux import ip_lib
from oslo_log import log as logging

from neutron_local_services import constants as lsc
from neutron_local_services.agent.plugins import base


LOG = logging.getLogger(__name__)

# State dir under which we place per-network plugin scratch space.
# Kept in lockstep with NEUTRON_LOCAL_SERVICES_STATE_DIR in the
# DevStack plugin (devstack/settings) — if you change one, change both.
DEFAULT_STATE_DIR = '/var/lib/neutron-local-services'

# Where the package's MISC_CHECK probe scripts live after install.
# pip-installs go under ``<sys.prefix>/lib/neutron-local-services/`` via
# data_files; for the dev-mode editable install we fall back to the
# source tree. The plugin resolves the path at apply_config time so a
# relocation between dev and prod doesn't bake the wrong path into a
# rendered config.
_CHECK_SCRIPT_NAMES = {
    lsc.HC_DNS: 'check_dns.sh',
    lsc.HC_NTP: 'check_ntp.sh',
}


def _state_dir(network_id):
    return os.path.join(DEFAULT_STATE_DIR, network_id, 'nat')


def _conf_path(network_id):
    return os.path.join(_state_dir(network_id), 'keepalived.conf')


def _pid_path(network_id):
    return os.path.join(_state_dir(network_id), 'keepalived.pid')


def _hash_path(network_id):
    return os.path.join(_state_dir(network_id), 'config.hash')


# Per-network mutex protecting apply_config from itself. The agent fires
# reconcile from three places that can interleave on startup: the
# Port_Binding CREATE event, start()'s initial _periodic_reconcile, and
# the looping call. Without serialization two callers observe "no
# pidfile yet" (keepalived takes ~1s to daemonize) and BOTH spawn
# keepalived. flock(2) won't help here — Linux flock is per-OFD, so
# two open() calls in the same process get distinct locks and don't
# block. A plain threading.Lock works for the agent's single-process
# eventlet model (oslo monkey-patches threading to be eventlet-aware).
_apply_locks = {}
_apply_locks_guard = threading.Lock()


def _apply_lock_for(network_id):
    with _apply_locks_guard:
        lock = _apply_locks.get(network_id)
        if lock is None:
            lock = threading.Lock()
            _apply_locks[network_id] = lock
        return lock


def _drop_apply_lock(network_id):
    with _apply_locks_guard:
        _apply_locks.pop(network_id, None)


def _resolve_check_script(name):
    """Return an absolute path to a shipped MISC_CHECK script.

    Looks in two places: a ``data_files`` install location next to the
    package (production), and the source tree (editable install / dev).
    Returns ``None`` if the script can't be found — the caller turns
    that into "no MISC_CHECK block in the rendered config" rather than
    erroring out, since a missing script is operator-fixable while the
    services it's attached to should keep working.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, 'check_scripts', name),
        os.path.join('/usr/local/lib/neutron-local-services', name),
        os.path.join('/usr/lib/neutron-local-services', name),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


_LB_ALGO = {
    lsc.DIST_ROUND_ROBIN: 'wrr',  # weighted round robin (degenerates to rr
                                  # when all weights are equal/None)
    lsc.DIST_LEAST_CONNECTION: 'wlc',
    lsc.DIST_ACTIVE_BACKUP: 'wrr',  # ip_vs has no active-backup; emulate
                                    # via weights (a future improvement: switch to sorry_server
                                    # / Envoy primary-backup if needed).
}


def _proto_keepalived(proto):
    """Map our protocol string to the literal keepalived expects."""
    return {lsc.PROTO_TCP: 'TCP', lsc.PROTO_UDP: 'UDP'}.get(proto)


def _expand_protocols(svc):
    """A tcp-udp service renders as two virtual_server blocks."""
    proto = svc.get('protocol')
    if proto == lsc.PROTO_TCP_UDP:
        return [lsc.PROTO_TCP, lsc.PROTO_UDP]
    if proto in (lsc.PROTO_TCP, lsc.PROTO_UDP):
        return [proto]
    return []


def _render_health_check(svc, backend):
    """Return the keepalived health-check block for a (svc, backend).

    The block goes inside a ``real_server`` stanza. We pick from the
    service's ``health_check_type``; backends inherit that type and may
    override the address/port via ``health_check_address`` /
    ``health_check_port``. ``HC_NONE`` returns an empty string (no
    block).

    For the MISC_CHECK types (dns, ntp) we resolve the script path at
    render time. If the script can't be found we degrade to no health
    check rather than write a config keepalived will reject — operator
    sees the warning, the service still load-balances.
    """
    hc_type = svc.get('health_check_type') or lsc.HC_NONE
    if hc_type == lsc.HC_NONE:
        return ''

    hc_addr = backend.get('health_check_address') or backend.get('address')
    hc_port = backend.get('health_check_port') or backend.get('port')

    # connect_ip / connect_port are emitted explicitly so a backend-
    # level health_check_address override actually probes that address
    # rather than the load-balanced one. Matches octavia's macros.j2.
    if hc_type == lsc.HC_TCP:
        return (
            '        TCP_CHECK {\n'
            '            connect_ip %s\n'
            '            connect_port %d\n'
            '            connect_timeout 3\n'
            '        }\n' % (hc_addr, hc_port))
    if hc_type == lsc.HC_HTTP:
        return (
            '        HTTP_GET {\n'
            '            url { path / status_code 200 }\n'
            '            connect_ip %s\n'
            '            connect_port %d\n'
            '            connect_timeout 3\n'
            '        }\n' % (hc_addr, hc_port))
    if hc_type == lsc.HC_HTTPS:
        return (
            '        SSL_GET {\n'
            '            url { path / status_code 200 }\n'
            '            connect_ip %s\n'
            '            connect_port %d\n'
            '            connect_timeout 3\n'
            '        }\n' % (hc_addr, hc_port))
    if hc_type in (lsc.HC_DNS, lsc.HC_NTP):
        script = _resolve_check_script(_CHECK_SCRIPT_NAMES[hc_type])
        if not script:
            LOG.warning(
                'health_check_type=%s but probe script %s not found '
                'on disk; rendering no health-check (backends will be '
                'considered up unconditionally)',
                hc_type, _CHECK_SCRIPT_NAMES[hc_type])
            return ''
        return (
            '        MISC_CHECK {\n'
            '            misc_path "%s %s %d"\n'
            '            misc_timeout 3\n'
            '        }\n' % (script, hc_addr, hc_port))
    return ''


def render_keepalived_conf(network_id, services):
    """Render the full keepalived.conf for one network.

    ``services`` is the list of service dicts (each with a ``backends``
    list). Empty list yields a minimal config with no virtual_server
    blocks — keepalived starts cleanly and just sits there, which is
    what we want when the last binding goes away but the netns lives on
    (e.g., another plugin is still using it).
    """
    blocks = []

    blocks.append(
        'global_defs {\n'
        '    router_id ls-%s\n'
        '    enable_script_security\n'
        '    script_user root\n'
        '}\n' % network_id[:8])

    for svc in services:
        if not svc.get('enabled', True):
            continue
        vip = svc.get('local_ipv4')
        port = svc.get('port')
        if not vip or not port:
            continue
        backends = [b for b in svc.get('backends') or []
                    if b.get('enabled', True)]
        algo = _LB_ALGO.get(
            svc.get('distribution_policy') or lsc.DIST_ROUND_ROBIN, 'wrr')

        for proto in _expand_protocols(svc):
            kp_proto = _proto_keepalived(proto)
            if not kp_proto:
                continue

            block = []
            block.append('virtual_server %s %d {' % (vip, port))
            block.append('    delay_loop 6')
            block.append('    lb_algo %s' % algo)
            block.append('    lb_kind NAT')
            block.append('    protocol %s' % kp_proto)
            block.append('    # service: %s (%s)' %
                         (svc.get('name', ''), svc.get('id', '')))

            for b in backends:
                addr = b.get('address')
                bport = b.get('port')
                if not addr or not bport:
                    continue
                weight = b.get('weight')
                # weight=None means "API attr default" — degrade to 1
                # rather than write keepalived a non-int.
                if weight in (None, '') or not isinstance(weight, int):
                    weight = 1
                block.append('    real_server %s %d {' % (addr, bport))
                block.append('        weight %d' % weight)
                block.append(_render_health_check(svc, b).rstrip('\n'))
                block.append('    }')

            block.append('}')
            blocks.append('\n'.join(block) + '\n')

    return '\n'.join(blocks)


def _read_pid(pid_path):
    try:
        with open(pid_path) as fh:
            return int((fh.read() or '0').strip())
    except (OSError, ValueError):
        return None


def _process_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        # ESRCH — pid is gone.
        return False
    except PermissionError:
        # EPERM — pid exists but we can't signal it. Happens when the
        # agent runs as ``stack`` and keepalived runs as ``root`` (the
        # subprocess inherits root because privsep escalates). Process
        # IS alive; signaling it later would still fail, but this code
        # path only cares about liveness.
        return True
    except OSError:
        return False
    return True


class NatPlugin(base.ExposurePlugin):
    """Keepalived/ip_vs plugin. Selected by ``exposure_plugin=nat``."""

    name = lsc.EXPOSURE_NAT

    # Seconds to wait after spawning keepalived for it to write its
    # pidfile. Bigger than you'd think (10-15s on selinux + privsep).
    # Overridden in unit tests.
    KEEPALIVED_START_TIMEOUT = 30
    KEEPALIVED_START_POLL = 0.25

    def __init__(self, ip_wrapper_cls=None):
        # ip_wrapper_cls is a seam for unit tests — they pass a Mock to
        # avoid real ``ip netns exec`` calls.
        self._ip_wrapper_cls = ip_wrapper_cls or ip_lib.IPWrapper

    def _ensure_state_dir(self, network_id):
        path = _state_dir(network_id)
        os.makedirs(path, mode=0o755, exist_ok=True)

    def _exec_in_ns(self, netns_name, cmd):
        """Run ``cmd`` (list) inside ``netns_name``, return stdout.

        ``privsep_exec=True`` routes through neutron's privsep daemon
        instead of rootwrap. Matters because rootwrap's filters don't
        cover the diverse set of utilities we shell into here
        (sysctl, iptables, kill, modprobe, keepalived); the privsep
        daemon executes anything it gets handed (subject to neutron's
        existing privsep grants for ``execute_in_namespace``). This
        matches ``ProcessManager._kill_process`` in neutron's own
        external_process.py.
        """
        ip = self._ip_wrapper_cls(namespace=netns_name)
        return ip.netns.execute(cmd, run_as_root=True, privsep_exec=True)

    def _prepare_netns(self, netns_name):
        """One-shot per-netns kernel knobs the LVS plugin depends on.

        Idempotent — sysctls and iptables -C/-A are no-ops once
        applied. Failures are logged-and-swallowed (a stale namespace
        with iptables already in place shouldn't sink the reconcile
        pass).
        """
        # ip_vs needs to be loaded for ipvsadm/keepalived to do
        # anything; on most distros it auto-loads on first ipvsadm
        # invocation, but loading explicitly makes the failure mode
        # clearer ("modprobe failed" vs "keepalived silently does
        # nothing").
        try:
            self._exec_in_ns(netns_name, ['modprobe', 'ip_vs'])
        except Exception:
            LOG.debug('modprobe ip_vs in %s failed (probably already '
                      'loaded; ignoring)', netns_name)

        # Allow ip_vs to register with netfilter conntrack so iptables
        # MASQUERADE on POSTROUTING applies cleanly to the rewritten
        # packets. Without this, LVS-NAT only works when the realserver
        # has the director as its default gateway.
        try:
            self._exec_in_ns(netns_name,
                             ['sysctl', '-w', 'net.ipv4.vs.conntrack=1'])
        except Exception:
            LOG.warning('Could not set net.ipv4.vs.conntrack=1 in %s; '
                        'LVS-NAT replies may not return to the director '
                        'unless the realserver routes through us',
                        netns_name)
        try:
            self._exec_in_ns(netns_name,
                             ['sysctl', '-w', 'net.ipv4.ip_forward=1'])
        except Exception:
            LOG.warning('Could not enable ip_forward in %s; LVS-NAT '
                        'will not forward', netns_name)

        # MASQUERADE on POSTROUTING so backend replies see src=netns IP
        # and route back through the director. The localsvc namespace
        # only has the localport's veth, so a wide-open POSTROUTING
        # MASQUERADE is fine — there's nothing else here to NAT.
        # ``-C`` first so we don't stack duplicates across reconcile
        # ticks; if -C fails the rule isn't there yet, so -A.
        try:
            self._exec_in_ns(
                netns_name,
                ['iptables', '-t', 'nat', '-C', 'POSTROUTING',
                 '-j', 'MASQUERADE'])
        except Exception:
            try:
                self._exec_in_ns(
                    netns_name,
                    ['iptables', '-t', 'nat', '-A', 'POSTROUTING',
                     '-j', 'MASQUERADE'])
            except Exception:
                LOG.warning('Could not install POSTROUTING MASQUERADE '
                            'in %s; LVS-NAT may fail unless realserver '
                            'has the director as default gateway',
                            netns_name)

    def _start_keepalived(self, network_id, netns_name):
        """Start keepalived in the netns. Caller has written the conf.

        After spawn we briefly poll for the pidfile so subsequent
        reconciler ticks (which our in-process lock serializes against
        this one) see a real pid and short-circuit on the hash match
        instead of double-spawning.
        """
        conf = _conf_path(network_id)
        pid_path = _pid_path(network_id)
        cmd = ['keepalived', '-f', conf, '-p', pid_path, '-D']
        LOG.info('Starting keepalived for network %s in %s',
                 network_id, netns_name)
        self._exec_in_ns(netns_name, cmd)
        # Poll for pidfile. Empirically keepalived under selinux +
        # neutron's privsep daemon takes 10-15s to write its pidfile
        # on the lab. The default budget is generous enough to ride
        # that out under heavy first-start load; tests override.
        # Subsequent reconcile ticks (gated by our in-process apply
        # lock) will see a real pid and noop. If we time out anyway,
        # the worst-case symptom is one extra ``keepalived: daemon is
        # already running`` log line per tick until the pidfile catches
        # up — functionally harmless.
        deadline = time.monotonic() + self.KEEPALIVED_START_TIMEOUT
        while time.monotonic() < deadline:
            pid = _read_pid(pid_path)
            if _process_alive(pid):
                return
            time.sleep(self.KEEPALIVED_START_POLL)
        LOG.warning(
            'keepalived for network %s did not write a pidfile within '
            '%ss; subsequent reconcile passes may log "daemon already '
            'running" until the pidfile lands',
            network_id, self.KEEPALIVED_START_TIMEOUT)

    def _reload_keepalived(self, network_id, pid=None):
        """SIGHUP keepalived — picks up the new config without dropping conns.

        Routed through the netns ``kill`` (via privsep) because the
        agent runs as ``stack`` while keepalived runs as ``root`` —
        a direct ``os.kill`` returns EPERM. ``ip netns exec ... kill``
        executes via the rootwrap daemon and works.
        """
        if pid is None:
            pid = _read_pid(_pid_path(network_id))
        if not _process_alive(pid):
            LOG.info('keepalived for network %s not alive; will start fresh',
                     network_id)
            return False
        netns_name = lsc.NETNS_PREFIX + network_id
        try:
            self._exec_in_ns(netns_name, ['kill', '-HUP', str(pid)])
        except Exception as exc:
            LOG.warning('SIGHUP keepalived pid %s failed: %s', pid, exc)
            return False
        LOG.info('SIGHUP keepalived (pid %s) for network %s',
                 pid, network_id)
        return True

    def apply_config(self, netns_name, services):
        # Pull the network_id back out of the netns name so the state
        # dir / pidfile path is stable across plugin instances. (The
        # agent passes both for plugins that don't want to do this
        # parse, but ours does.)
        if not netns_name.startswith(lsc.NETNS_PREFIX):
            LOG.warning('LVS plugin: unexpected netns name %s; skipping',
                        netns_name)
            return
        network_id = netns_name[len(lsc.NETNS_PREFIX):]

        self._ensure_state_dir(network_id)
        with _apply_lock_for(network_id):
            self._apply_locked(network_id, netns_name, services)

    def _apply_locked(self, network_id, netns_name, services):
        # Cheap escape hatch: if there are no services for the nat
        # plugin AND we don't already have a keepalived running for
        # this network, do nothing. Without this, every reconcile
        # spawns keepalived unconditionally (it just sits there with
        # no virtual_servers), eating ~30s of pidfile-wait per pass
        # and starving sibling plugins (e.g. `proxy`) that share the
        # reconciler.
        if not services:
            existing_pid = _read_pid(_pid_path(network_id))
            if not _process_alive(existing_pid):
                return
        new_conf = render_keepalived_conf(network_id, services)
        new_hash = hashlib.sha256(new_conf.encode('utf-8')).hexdigest()
        hash_path = _hash_path(network_id)

        try:
            with open(hash_path) as fh:
                old_hash = fh.read().strip()
        except OSError:
            old_hash = None

        # The in-process apply lock + ``_start_keepalived``'s
        # wait-for-pidfile mean that once we hold the lock, the pidfile
        # accurately reflects whether keepalived is up.
        pid = _read_pid(_pid_path(network_id))
        running = _process_alive(pid)

        if old_hash == new_hash and running:
            return

        conf_path = _conf_path(network_id)
        tmp_path = conf_path + '.tmp'
        with open(tmp_path, 'w') as fh:
            fh.write(new_conf)
        os.replace(tmp_path, conf_path)
        with open(hash_path, 'w') as fh:
            fh.write(new_hash)

        # Per-netns kernel knobs. Cheap to re-run; idempotent.
        self._prepare_netns(netns_name)

        if running:
            if self._reload_keepalived(network_id, pid):
                return
        # Either we weren't running or SIGHUP failed. Start fresh.
        self._start_keepalived(network_id, netns_name)

    def teardown(self, netns_name):
        if not netns_name.startswith(lsc.NETNS_PREFIX):
            return
        network_id = netns_name[len(lsc.NETNS_PREFIX):]
        pid = _read_pid(_pid_path(network_id))
        if _process_alive(pid):
            # Same EPERM caveat as _reload_keepalived: route via privsep.
            try:
                self._exec_in_ns(netns_name,
                                 ['kill', '-TERM', str(pid)])
            except Exception:
                LOG.debug('keepalived pid %s already gone (or netns '
                          'unreachable)', pid)
        # State dir cleanup. The agent will destroy the netns next,
        # which kills any remaining in-netns process; leftover files on
        # disk are this plugin's mess to clean up.
        path = _state_dir(network_id)
        if os.path.isdir(path):
            try:
                shutil.rmtree(path)
            except OSError:
                LOG.exception('Failed to remove %s', path)
        _drop_apply_lock(network_id)


# Register on import. The agent extension imports this module at
# extension load time (see extension.py).
base.register(NatPlugin)
