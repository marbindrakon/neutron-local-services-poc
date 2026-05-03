"""Unit tests for the LVS / Keepalived exposure plugin.

The plugin is mostly file-IO + subprocess-spawn + config rendering.
We unit-test:

* render_keepalived_conf — every distribution_policy, every health_check
  type, multi-protocol services, disabled services/backends, weight
  defaults.
* apply_config — idempotency via the config.hash short-circuit, SIGHUP
  on change, fresh start when keepalived isn't running.
* teardown — SIGTERM + state dir removal, idempotent on a fresh netns.

The kernel / iptables side is mocked at the IPWrapper boundary so the
tests don't need privileges or a netns.
"""

import os
import shutil
import tempfile
from unittest import mock

import testtools

from neutron_local_services import constants as lsc
from neutron_local_services.agent.plugins import nat as lvs  # alias kept to minimize diff


NET_ID = '11111111-1111-1111-1111-111111111111'
NS_NAME = lsc.NETNS_PREFIX + NET_ID


def _svc(**overrides):
    """Build a service dict with sane defaults; tests override the bits
    they care about."""
    base = {
        'id': 'svc-aaaa',
        'name': 'dns',
        'local_ipv4': '169.254.169.5',
        'port': 53,
        'protocol': lsc.PROTO_UDP,
        'distribution_policy': lsc.DIST_ROUND_ROBIN,
        'health_check_type': lsc.HC_NONE,
        'enabled': True,
        'backends': [],
    }
    base.update(overrides)
    return base


def _backend(**overrides):
    base = {
        'address': '10.0.0.10',
        'port': 53,
        'weight': 1,
        'enabled': True,
    }
    base.update(overrides)
    return base


class TestRenderKeepalivedConf(testtools.TestCase):

    def test_emits_global_defs_with_router_id(self):
        out = lvs.render_keepalived_conf(NET_ID, [])
        self.assertIn('global_defs', out)
        # Keepalived config is whitespace-tolerant; the router_id token
        # itself is what we care about so the netns is identifiable in
        # multi-instance setups.
        self.assertIn('router_id ls-' + NET_ID[:8], out)

    def test_renders_udp_virtual_server(self):
        svc = _svc(backends=[_backend()])
        out = lvs.render_keepalived_conf(NET_ID, [svc])
        self.assertIn('virtual_server 169.254.169.5 53', out)
        self.assertIn('protocol UDP', out)
        self.assertIn('lb_kind NAT', out)
        self.assertIn('real_server 10.0.0.10 53', out)
        self.assertIn('weight 1', out)

    def test_renders_tcp_virtual_server(self):
        svc = _svc(protocol=lsc.PROTO_TCP, port=80, backends=[_backend()])
        out = lvs.render_keepalived_conf(NET_ID, [svc])
        self.assertIn('protocol TCP', out)

    def test_tcp_udp_renders_two_blocks(self):
        # A single tcp-udp service yields two virtual_server stanzas —
        # the kernel can't load-balance both protos under one block.
        svc = _svc(protocol=lsc.PROTO_TCP_UDP, port=8080,
                   backends=[_backend(port=8080)])
        out = lvs.render_keepalived_conf(NET_ID, [svc])
        self.assertEqual(2, out.count('virtual_server 169.254.169.5 8080'))
        self.assertIn('protocol TCP', out)
        self.assertIn('protocol UDP', out)

    def test_distribution_policy_maps_to_lb_algo(self):
        svc = _svc(distribution_policy=lsc.DIST_LEAST_CONNECTION,
                   backends=[_backend()])
        out = lvs.render_keepalived_conf(NET_ID, [svc])
        self.assertIn('lb_algo wlc', out)

    def test_active_backup_renders_as_wrr(self):
        # ip_vs has no native active-backup; we degrade to weighted RR.
        # If a future plugin gets sorry_server / Envoy primary-backup
        # this maps lookup needs to grow.
        svc = _svc(distribution_policy=lsc.DIST_ACTIVE_BACKUP,
                   backends=[_backend()])
        out = lvs.render_keepalived_conf(NET_ID, [svc])
        self.assertIn('lb_algo wrr', out)

    def test_skips_disabled_service(self):
        svc = _svc(enabled=False, backends=[_backend()])
        out = lvs.render_keepalived_conf(NET_ID, [svc])
        self.assertNotIn('virtual_server', out)

    def test_skips_disabled_backend(self):
        svc = _svc(backends=[_backend(),
                             _backend(address='10.0.0.20', enabled=False)])
        out = lvs.render_keepalived_conf(NET_ID, [svc])
        self.assertIn('real_server 10.0.0.10', out)
        self.assertNotIn('real_server 10.0.0.20', out)

    def test_weight_none_renders_as_one(self):
        # API attr default is ATTR_NOT_SPECIFIED → DB stores NULL →
        # we receive None. Don't write 'weight None' into keepalived's
        # config.
        svc = _svc(backends=[_backend(weight=None)])
        out = lvs.render_keepalived_conf(NET_ID, [svc])
        self.assertIn('weight 1', out)

    def test_tcp_check_uses_explicit_connect_ip(self):
        svc = _svc(protocol=lsc.PROTO_TCP, health_check_type=lsc.HC_TCP,
                   backends=[_backend()])
        out = lvs.render_keepalived_conf(NET_ID, [svc])
        self.assertIn('TCP_CHECK', out)
        self.assertIn('connect_ip 10.0.0.10', out)
        self.assertIn('connect_port 53', out)

    def test_health_check_address_override(self):
        # Operator runs the health-check port on a separate IP/port
        # from the load-balanced one; both must be honored.
        svc = _svc(protocol=lsc.PROTO_TCP, health_check_type=lsc.HC_TCP,
                   backends=[_backend(health_check_address='10.0.0.99',
                                      health_check_port=8080)])
        out = lvs.render_keepalived_conf(NET_ID, [svc])
        self.assertIn('connect_ip 10.0.0.99', out)
        self.assertIn('connect_port 8080', out)

    def test_http_get_check(self):
        svc = _svc(protocol=lsc.PROTO_TCP, health_check_type=lsc.HC_HTTP,
                   port=80, backends=[_backend(port=80)])
        out = lvs.render_keepalived_conf(NET_ID, [svc])
        self.assertIn('HTTP_GET', out)
        self.assertIn('status_code 200', out)

    def test_misc_check_dns_uses_shipped_script(self):
        svc = _svc(health_check_type=lsc.HC_DNS, backends=[_backend()])
        out = lvs.render_keepalived_conf(NET_ID, [svc])
        self.assertIn('MISC_CHECK', out)
        # Path should resolve into the source tree (editable install)
        # or one of the install fallbacks. Bare existence of "check_dns.sh"
        # in the rendered config is what matters.
        self.assertIn('check_dns.sh', out)

    def test_misc_check_ntp(self):
        svc = _svc(health_check_type=lsc.HC_NTP, port=123,
                   backends=[_backend(port=123)])
        out = lvs.render_keepalived_conf(NET_ID, [svc])
        self.assertIn('check_ntp.sh', out)
        self.assertIn('MISC_CHECK', out)

    def test_misc_check_falls_back_when_script_missing(self):
        # If the operator strips the package and the scripts are gone,
        # we'd rather render no health check than a config keepalived
        # rejects.
        svc = _svc(health_check_type=lsc.HC_DNS, backends=[_backend()])
        with mock.patch.object(lvs, '_resolve_check_script',
                               return_value=None):
            out = lvs.render_keepalived_conf(NET_ID, [svc])
        self.assertNotIn('MISC_CHECK', out)
        # The real_server still rendered — the service should still
        # load-balance, just without active health checking.
        self.assertIn('real_server 10.0.0.10', out)

    def test_no_health_check_block_when_type_none(self):
        svc = _svc(backends=[_backend()])  # default HC_NONE
        out = lvs.render_keepalived_conf(NET_ID, [svc])
        for token in ('TCP_CHECK', 'HTTP_GET', 'MISC_CHECK', 'SSL_GET'):
            self.assertNotIn(token, out)


class TestApplyConfig(testtools.TestCase):

    def setUp(self):
        super().setUp()
        # Redirect state dir into a tempdir so we don't pollute /var.
        self._tmp = tempfile.mkdtemp(prefix='lvs-test-')
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        p = mock.patch.object(lvs, 'DEFAULT_STATE_DIR', self._tmp)
        p.start(); self.addCleanup(p.stop)

        # Mock IPWrapper so the plugin's "ip netns exec" path doesn't
        # try to open a namespace. The plugin's ``_exec_in_ns`` calls
        # ``IPWrapper(namespace=ns).netns.execute(cmd, run_as_root=True)``
        # so we intercept at the IPWrapper class level.
        self.ip_wrapper_cls = mock.MagicMock()
        self.ip_wrapper_inst = self.ip_wrapper_cls.return_value
        self.ip_wrapper_inst.netns.execute.return_value = ''

        # And mock os.kill so we don't actually signal the host's PIDs.
        p2 = mock.patch.object(lvs.os, 'kill')
        self.os_kill = p2.start(); self.addCleanup(p2.stop)
        # _process_alive uses os.kill(pid, 0); default to "alive"
        # unless overridden by tests.
        self.os_kill.return_value = None

        self.plugin = lvs.NatPlugin(ip_wrapper_cls=self.ip_wrapper_cls)
        # Don't actually wait 30s for a fake pidfile to appear in tests.
        self.plugin.KEEPALIVED_START_TIMEOUT = 0
        self.plugin.KEEPALIVED_START_POLL = 0

    def _ran_keepalived(self):
        """True if any execute() call was a `keepalived -f ...` invocation."""
        for call in self.ip_wrapper_inst.netns.execute.call_args_list:
            cmd = call.args[0]
            if cmd and cmd[0] == 'keepalived':
                return True
        return False

    def _ran_kill_signal(self, signal_name):
        """True if any execute() ran `kill -<signal_name> <pid>`."""
        token = '-' + signal_name
        for call in self.ip_wrapper_inst.netns.execute.call_args_list:
            cmd = call.args[0]
            if cmd and cmd[0] == 'kill' and len(cmd) >= 2 and cmd[1] == token:
                return True
        return False

    def test_first_apply_writes_conf_and_starts_keepalived(self):
        # Pretend keepalived is NOT yet running (no pidfile).
        self.os_kill.side_effect = OSError
        self.plugin.apply_config(NS_NAME, [_svc(backends=[_backend()])])
        # Config and hash files exist on disk.
        self.assertTrue(os.path.exists(lvs._conf_path(NET_ID)))
        self.assertTrue(os.path.exists(lvs._hash_path(NET_ID)))
        # And we tried to run keepalived in the netns.
        self.assertTrue(self._ran_keepalived())

    def test_second_apply_with_same_config_is_noop(self):
        # First call writes the config + hash. Then we stub a
        # "running" keepalived: pidfile written, os.kill(pid, 0)
        # succeeds (default side_effect=None).
        self.os_kill.side_effect = OSError
        self.plugin.apply_config(NS_NAME, [_svc(backends=[_backend()])])
        with open(lvs._pid_path(NET_ID), 'w') as fh:
            fh.write('12345')
        self.os_kill.side_effect = None
        self.ip_wrapper_inst.netns.execute.reset_mock()

        self.plugin.apply_config(NS_NAME, [_svc(backends=[_backend()])])
        # Hash matched + alive → no respawn AND no kill -HUP.
        self.assertFalse(self._ran_keepalived())
        self.assertFalse(self._ran_kill_signal('HUP'))

    def test_changed_config_sighups_running_keepalived(self):
        # First apply with one backend; second apply with two — config
        # hash changes so the plugin should SIGHUP via privsep
        # (`ip netns exec ... kill -HUP <pid>`), not respawn.
        self.os_kill.side_effect = OSError
        self.plugin.apply_config(NS_NAME, [_svc(backends=[_backend()])])
        with open(lvs._pid_path(NET_ID), 'w') as fh:
            fh.write('12345')
        self.os_kill.side_effect = None
        self.os_kill.reset_mock()
        self.ip_wrapper_inst.netns.execute.reset_mock()

        self.plugin.apply_config(NS_NAME, [_svc(backends=[
            _backend(), _backend(address='10.0.0.20')])])
        # `kill -HUP 12345` should have been the only keepalived-touching
        # exec. No fresh `keepalived` spawn.
        self.assertTrue(self._ran_kill_signal('HUP'))
        self.assertFalse(self._ran_keepalived())
        # And the kill targeted the right pid.
        kill_calls = [c for c in self.ip_wrapper_inst.netns.execute.call_args_list
                      if c.args[0] and c.args[0][0] == 'kill']
        self.assertEqual(['kill', '-HUP', '12345'], kill_calls[0].args[0])

    def test_starts_fresh_when_pidfile_stale(self):
        # Pid file exists but the process is dead → restart, not SIGHUP.
        self.plugin._ensure_state_dir(NET_ID)
        with open(lvs._pid_path(NET_ID), 'w') as fh:
            fh.write('99999')
        self.os_kill.side_effect = OSError
        self.plugin.apply_config(NS_NAME, [_svc(backends=[_backend()])])
        self.assertTrue(self._ran_keepalived())

    def test_apply_config_skips_unexpected_netns(self):
        # Defensive: if the agent ever passes us something that doesn't
        # start with the localsvc- prefix, log and move on rather than
        # write state into a path derived from garbage.
        self.os_kill.side_effect = OSError
        self.plugin.apply_config('ovnmeta-other', [_svc()])
        # No state dir under our tempdir.
        self.assertEqual([], os.listdir(self._tmp))
        # And no keepalived launched.
        self.assertFalse(self._ran_keepalived())


class TestTeardown(testtools.TestCase):

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.mkdtemp(prefix='lvs-test-')
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        p = mock.patch.object(lvs, 'DEFAULT_STATE_DIR', self._tmp)
        p.start(); self.addCleanup(p.stop)
        p2 = mock.patch.object(lvs.os, 'kill')
        self.os_kill = p2.start(); self.addCleanup(p2.stop)
        self.plugin = lvs.NatPlugin(ip_wrapper_cls=mock.MagicMock())

    def test_teardown_signals_and_removes_state(self):
        os.makedirs(lvs._state_dir(NET_ID))
        with open(lvs._pid_path(NET_ID), 'w') as fh:
            fh.write('4242')
        # Plugin uses ip_lib's IPWrapper for the in-netns kill via
        # privsep; we mock that boundary.
        ip_wrapper_cls = mock.MagicMock()
        ip_wrapper_cls.return_value.netns.execute.return_value = ''
        self.plugin = lvs.NatPlugin(ip_wrapper_cls=ip_wrapper_cls)

        self.plugin.teardown(NS_NAME)
        # `kill -TERM 4242` was the in-ns command issued.
        kill_calls = [c for c in ip_wrapper_cls.return_value.netns.execute.call_args_list
                      if c.args[0] and c.args[0][0] == 'kill']
        self.assertEqual(1, len(kill_calls))
        self.assertEqual(['kill', '-TERM', '4242'], kill_calls[0].args[0])
        # State dir gone.
        self.assertFalse(os.path.isdir(lvs._state_dir(NET_ID)))

    def test_teardown_idempotent_no_state(self):
        # Never applied — teardown should be a no-op (no kill issued).
        ip_wrapper_cls = mock.MagicMock()
        self.plugin = lvs.NatPlugin(ip_wrapper_cls=ip_wrapper_cls)
        self.plugin.teardown(NS_NAME)  # no raise
        ip_wrapper_cls.return_value.netns.execute.assert_not_called()

    def test_teardown_skips_unexpected_netns(self):
        ip_wrapper_cls = mock.MagicMock()
        self.plugin = lvs.NatPlugin(ip_wrapper_cls=ip_wrapper_cls)
        self.plugin.teardown('ovnmeta-something')
        ip_wrapper_cls.return_value.netns.execute.assert_not_called()
