"""Unit tests for the underlay-egress module.

Covers the /30 allocator, the per-network provision/teardown plumbing,
and the destination-ACL reconciler. The actual ip/iptables/sysctl calls
are out of scope for unit tests — we mock them at the module boundary
and verify the call shape, the address derivation, and the rule sets
the reconciler emits.
"""

import os
import tempfile
from unittest import mock

import testtools

from neutron_local_services import constants as lsc
from neutron_local_services.agent import underlay


NET_A = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
NET_B = '11112222-3333-4444-5555-666677778888'
# First 9 chars match — exercises the per-net chain name length budget.
NET_COLLISION_A = 'aaaaaaaaa-1234-5678-9abc-def012345678'
NET_COLLISION_B = 'aaaaaaaaa-9999-9999-9999-999999999999'


# --- Naming -------------------------------------------------------------


class TestNaming(testtools.TestCase):

    def test_veth_names_fit_ifnamsiz(self):
        # Linux IFNAMSIZ-1=15. Our scheme yields 14.
        root, ns = underlay.underlay_veth_names(NET_A)
        self.assertEqual(14, len(root))
        self.assertEqual(14, len(ns))

    def test_veth_root_and_ns_distinguishable(self):
        root, ns = underlay.underlay_veth_names(NET_A)
        self.assertNotEqual(root, ns)
        self.assertTrue(root.endswith('0'))
        self.assertTrue(ns.endswith('1'))

    def test_veth_uses_underlay_prefix(self):
        root, ns = underlay.underlay_veth_names(NET_A)
        self.assertTrue(root.startswith(lsc.UNDERLAY_VETH_PREFIX))
        self.assertTrue(ns.startswith(lsc.UNDERLAY_VETH_PREFIX))

    def test_per_net_chain_within_iptables_limit(self):
        # Linux iptables chain names cap at 28 chars.
        chain = underlay.per_net_chain_name(NET_A)
        self.assertLessEqual(len(chain), 28)
        self.assertTrue(chain.startswith(lsc.UNDERLAY_PER_NET_CHAIN_PREFIX))


# --- Allocator ----------------------------------------------------------


class TestAllocator(testtools.TestCase):

    def setUp(self):
        super().setUp()
        # Per-test state directory so the allocator's persistence file
        # doesn't leak between tests.
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        # Best-effort — tests don't depend on cleanup.
        try:
            for f in os.listdir(self.tmpdir):
                os.unlink(os.path.join(self.tmpdir, f))
            os.rmdir(self.tmpdir)
        except OSError:
            pass

    def _alloc(self, pool='100.64.0.0/22'):
        return underlay.UnderlayAllocator(pool, state_dir=self.tmpdir)

    def test_allocate_first_fit_starts_at_zero(self):
        a = self._alloc()
        self.assertEqual(0, a.allocate(NET_A))

    def test_allocate_idempotent_for_same_network(self):
        a = self._alloc()
        idx1 = a.allocate(NET_A)
        idx2 = a.allocate(NET_A)
        self.assertEqual(idx1, idx2)

    def test_allocate_distinct_networks_get_distinct_slots(self):
        a = self._alloc()
        idx_a = a.allocate(NET_A)
        idx_b = a.allocate(NET_B)
        self.assertNotEqual(idx_a, idx_b)

    def test_free_releases_slot(self):
        a = self._alloc()
        a.allocate(NET_A)
        a.free(NET_A)
        # Reallocating a different network should now get slot 0.
        self.assertEqual(0, a.allocate(NET_B))

    def test_free_unallocated_is_noop(self):
        a = self._alloc()
        a.free(NET_A)  # Must not raise.
        self.assertIsNone(a.get(NET_A))

    def test_get_returns_none_when_unallocated(self):
        a = self._alloc()
        self.assertIsNone(a.get(NET_A))

    def test_state_persists_across_instances(self):
        a1 = self._alloc()
        idx = a1.allocate(NET_A)
        # Fresh instance reads the same state file.
        a2 = self._alloc()
        self.assertEqual(idx, a2.get(NET_A))

    def test_state_recovers_from_missing_file(self):
        # First instance never allocates → file doesn't exist yet.
        a = self._alloc()
        self.assertEqual(0, a.allocate(NET_A))

    def test_state_drops_stale_entries_outside_pool(self):
        # Persist an out-of-range index, then load — the loader should
        # silently drop it and the next allocate should reuse slot 0.
        with open(os.path.join(self.tmpdir, 'allocations.json'), 'w') as fh:
            fh.write('{"some-old-net": 99999}')
        a = self._alloc()
        self.assertIsNone(a.get('some-old-net'))
        self.assertEqual(0, a.allocate(NET_A))

    def test_addresses_derive_from_pool_and_index(self):
        a = self._alloc(pool='100.64.0.0/22')
        host, ns = a.addresses(NET_A)
        # Slot 0 → .1 / .2 in the first /30.
        self.assertEqual('100.64.0.1', host)
        self.assertEqual('100.64.0.2', ns)

    def test_addresses_for_second_slot(self):
        a = self._alloc(pool='100.64.0.0/22')
        a.allocate(NET_A)  # slot 0
        host, ns = a.addresses(NET_B)  # slot 1
        # Slot 1 → .5 / .6 (second /30 starts at .4).
        self.assertEqual('100.64.0.5', host)
        self.assertEqual('100.64.0.6', ns)

    def test_pool_exhaustion_raises(self):
        # /30 pool has exactly one slot. Allocate, then try a second.
        a = self._alloc(pool='100.64.0.0/30')
        a.allocate(NET_A)
        self.assertRaises(underlay.UnderlayPoolExhausted,
                          a.allocate, NET_B)

    def test_rejects_ipv6_pool(self):
        self.assertRaises(ValueError,
                          underlay.UnderlayAllocator,
                          'fd00::/64', state_dir=self.tmpdir)

    def test_rejects_too_narrow_pool(self):
        # /31 has 0 slots in our /30 layout.
        self.assertRaises(ValueError,
                          underlay.UnderlayAllocator,
                          '100.64.0.0/31', state_dir=self.tmpdir)


# --- Chassis chain ------------------------------------------------------


class TestInstallChassisChain(testtools.TestCase):
    """Pin the chassis-wide iptables prelude.

    The chain is jumped from BOTH FORWARD and INPUT. FORWARD covers
    "tenant netns → remote backend" (the design happy path). INPUT
    covers "tenant netns → chassis host IP" (e.g. ``172.18.0.128`` —
    the underlay NIC). Without the INPUT jump the per-net DROP-by-
    default ACL doesn't apply to host-bound traffic, and a process in
    the tenant netns can reach every host-listening socket on the
    chassis. The INPUT rule is a fix for that isolation gap.

    Spy on the helpers (``_ensure_chain`` / ``_ensure_rule`` /
    ``_exec_root``) rather than the iptables binary itself — the
    helpers are the public seam, and asserting on the rule tuples is
    far less brittle than reconstructing the exact ``-C`` / ``-A``
    sequence the helpers emit.
    """

    def setUp(self):
        super().setUp()
        p = mock.patch.object(underlay, '_ensure_chain')
        self.ensure_chain = p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(underlay, '_ensure_rule')
        self.ensure_rule = p.start(); self.addCleanup(p.stop)
        # The chassis-chain code only calls _exec_root when something
        # else (sysctl, modprobe-style commands) needs it; mock so
        # nothing actually shells out under test.
        p = mock.patch.object(underlay, '_exec_root')
        p.start(); self.addCleanup(p.stop)
        underlay._chassis_chain_installed.clear()

    def _has_rule(self, table, chain, args):
        target = (table, chain, list(args))
        return any(
            (c.args[0], c.args[1], list(c.args[2])) == target
            for c in self.ensure_rule.call_args_list)

    def test_jumps_from_forward(self):
        # Pre-existing FORWARD jump remains. Regression canary.
        underlay.install_chassis_chain('100.64.0.0/22')
        self.assertTrue(self._has_rule(
            'filter', 'FORWARD', ['-j', lsc.UNDERLAY_HOST_CHAIN]))

    def test_jumps_from_input_for_underlay_veths(self):
        # New rule that closes the tenant→host-IP isolation gap.
        # The jump is scoped to ``-i nlsu+`` so it doesn't affect
        # unrelated host traffic on other interfaces.
        underlay.install_chassis_chain('100.64.0.0/22')
        self.assertTrue(self._has_rule(
            'filter', 'INPUT',
            ['-i', lsc.UNDERLAY_VETH_PREFIX + '+',
             '-j', lsc.UNDERLAY_HOST_CHAIN]))

    def test_drops_inter_tenant_cross_talk(self):
        underlay.install_chassis_chain('100.64.0.0/22')
        self.assertTrue(self._has_rule(
            'filter', lsc.UNDERLAY_HOST_CHAIN,
            ['-i', lsc.UNDERLAY_VETH_PREFIX + '+',
             '-o', lsc.UNDERLAY_VETH_PREFIX + '+',
             '-j', 'DROP']))

    def test_accepts_established_related(self):
        # The chain runs as both a FORWARD and an INPUT consumer, so
        # the ESTABLISHED/RELATED ACCEPT must come first to let return
        # traffic flow on whichever path it took out.
        underlay.install_chassis_chain('100.64.0.0/22')
        self.assertTrue(self._has_rule(
            'filter', lsc.UNDERLAY_HOST_CHAIN,
            ['-m', 'conntrack', '--ctstate', 'ESTABLISHED,RELATED',
             '-j', 'ACCEPT']))


# --- Provision / teardown -----------------------------------------------


class TestProvision(testtools.TestCase):

    def setUp(self):
        super().setUp()
        p = mock.patch.object(underlay, 'ip_lib')
        self.ip_lib = p.start()
        self.addCleanup(p.stop)

        p = mock.patch.object(underlay, '_exec_root')
        self.exec_root = p.start()
        self.addCleanup(p.stop)

        p = mock.patch.object(underlay, '_exec_in_ns')
        self.exec_ns = p.start()
        self.addCleanup(p.stop)

        # Make rule-presence probes (-C) succeed by default — that
        # short-circuits _ensure_rule into a no-op so we can focus on
        # the lifecycle calls. Tests that care override this.
        self.exec_root.return_value = ''
        self.exec_ns.return_value = ''

        self.allocator = mock.Mock()
        self.allocator.addresses.return_value = ('100.64.0.1', '100.64.0.2')
        self.allocator.free = mock.Mock()

        # Default: namespace exists, both veths absent.
        self.ip_lib.network_namespace_exists.return_value = True
        self.ip_lib.device_exists.return_value = False

        # Mock IPWrapper().add_veth and the resulting devices.
        self.root_dev = mock.Mock()
        self.ns_dev = mock.Mock()
        self.root_dev.addr.list.return_value = []
        self.ns_dev.addr.list.return_value = []
        self.ip_lib.IPDevice.side_effect = lambda *a, **kw: (
            self.ns_dev if kw.get('namespace') else self.root_dev)
        wrapper = mock.Mock()
        self.ip_lib.IPWrapper.return_value = wrapper
        self.add_veth = wrapper.add_veth
        self.del_veth = wrapper.del_veth

    def test_skip_when_netns_absent(self):
        self.ip_lib.network_namespace_exists.return_value = False
        result = underlay.provision_for_network(NET_A, self.allocator)
        self.assertIsNone(result)
        self.add_veth.assert_not_called()

    def test_creates_veth_when_absent(self):
        result = underlay.provision_for_network(NET_A, self.allocator)
        self.assertEqual(('100.64.0.1', '100.64.0.2'), result)
        root, ns = underlay.underlay_veth_names(NET_A)
        self.add_veth.assert_called_once_with(
            root, ns, namespace2='localsvc-' + NET_A)

    def test_skip_add_veth_when_ns_side_already_present(self):
        # Mimic the "already provisioned" idempotent path.
        def _exists(name, namespace=None):
            _, ns_veth = underlay.underlay_veth_names(NET_A)
            return name == ns_veth and namespace == 'localsvc-' + NET_A
        self.ip_lib.device_exists.side_effect = _exists
        underlay.provision_for_network(NET_A, self.allocator)
        self.add_veth.assert_not_called()

    def test_assigns_addresses(self):
        underlay.provision_for_network(NET_A, self.allocator)
        self.root_dev.addr.add.assert_called_with('100.64.0.1/30')
        self.ns_dev.addr.add.assert_called_with('100.64.0.2/30')

    def test_brings_both_ends_up(self):
        underlay.provision_for_network(NET_A, self.allocator)
        self.root_dev.link.set_up.assert_called_once()
        self.ns_dev.link.set_up.assert_called_once()

    def test_installs_default_route(self):
        underlay.provision_for_network(NET_A, self.allocator)
        # Default route uses host_ip as gateway.
        ns_calls = [call.args[1] for call in self.exec_ns.call_args_list]
        self.assertTrue(
            any(c == ['ip', 'route', 'replace', 'default', 'via',
                      '100.64.0.1']
                for c in ns_calls),
            'expected default route command, got: %s' % ns_calls)


class TestTeardown(testtools.TestCase):

    def setUp(self):
        super().setUp()
        p = mock.patch.object(underlay, 'ip_lib')
        self.ip_lib = p.start()
        self.addCleanup(p.stop)

        p = mock.patch.object(underlay, '_exec_root')
        self.exec_root = p.start()
        self.addCleanup(p.stop)
        self.exec_root.return_value = ''

        self.allocator = mock.Mock()
        wrapper = mock.Mock()
        self.ip_lib.IPWrapper.return_value = wrapper
        self.del_veth = wrapper.del_veth

    def test_frees_allocation(self):
        underlay.teardown_for_network(NET_A, self.allocator)
        self.allocator.free.assert_called_once_with(NET_A)

    def test_deletes_root_veth_when_present(self):
        self.ip_lib.device_exists.return_value = True
        underlay.teardown_for_network(NET_A, self.allocator)
        root, _ = underlay.underlay_veth_names(NET_A)
        self.del_veth.assert_called_once_with(root)

    def test_skips_veth_delete_when_absent(self):
        self.ip_lib.device_exists.return_value = False
        underlay.teardown_for_network(NET_A, self.allocator)
        self.del_veth.assert_not_called()

    def test_idempotent_double_teardown(self):
        # First call removes everything.
        self.ip_lib.device_exists.return_value = True
        underlay.teardown_for_network(NET_A, self.allocator)
        # Second call: device_exists now False everywhere.
        self.ip_lib.device_exists.return_value = False
        underlay.teardown_for_network(NET_A, self.allocator)  # no raise


# --- Destination ACL reconciler -----------------------------------------


def _svc(svc_id, proto='tcp', backends=None, enabled=True,
         exposure_plugin='nat'):
    backends = backends if backends is not None else []
    return {
        'id': svc_id,
        'protocol': proto,
        'enabled': enabled,
        'exposure_plugin': exposure_plugin,
        'backends': backends,
    }


def _be(addr, port, enabled=True, hc_addr=None, hc_port=None):
    return {
        'address': addr,
        'port': port,
        'enabled': enabled,
        'health_check_address': hc_addr,
        'health_check_port': hc_port,
    }


class TestBackendEndpoints(testtools.TestCase):
    """The pure rule-derivation logic (no mocking)."""

    def test_simple_tcp_service_yields_one_tuple_per_backend(self):
        svc = _svc('s', proto='tcp', backends=[_be('1.1.1.1', 80)])
        self.assertEqual(
            [('tcp', '1.1.1.1', 80)],
            sorted(underlay._backend_endpoints([svc])))

    def test_tcp_udp_service_yields_both_protos(self):
        svc = _svc('s', proto='tcp-udp', backends=[_be('1.1.1.1', 53)])
        self.assertEqual(
            [('tcp', '1.1.1.1', 53), ('udp', '1.1.1.1', 53)],
            sorted(underlay._backend_endpoints([svc])))

    def test_disabled_service_excluded(self):
        svc = _svc('s', backends=[_be('1.1.1.1', 80)], enabled=False)
        self.assertEqual([], list(underlay._backend_endpoints([svc])))

    def test_disabled_backend_excluded(self):
        svc = _svc('s', backends=[
            _be('1.1.1.1', 80, enabled=False),
            _be('2.2.2.2', 80),
        ])
        self.assertEqual(
            [('tcp', '2.2.2.2', 80)],
            sorted(underlay._backend_endpoints([svc])))

    def test_hc_override_address_yields_extra_tuples(self):
        svc = _svc('s', proto='tcp', backends=[
            _be('1.1.1.1', 80, hc_addr='1.1.1.2', hc_port=8080),
        ])
        # data tuple + HC tuples on both proto (we allow both for HC).
        result = sorted(underlay._backend_endpoints([svc]))
        self.assertIn(('tcp', '1.1.1.1', 80), result)
        self.assertIn(('tcp', '1.1.1.2', 8080), result)
        self.assertIn(('udp', '1.1.1.2', 8080), result)

    def test_hc_same_as_data_yields_no_extra(self):
        # When HC fields aren't overridden (or match data), no duplicate
        # endpoints surface.
        svc = _svc('s', proto='tcp', backends=[
            _be('1.1.1.1', 80, hc_addr='1.1.1.1', hc_port=80),
        ])
        result = sorted(underlay._backend_endpoints([svc]))
        self.assertEqual([('tcp', '1.1.1.1', 80)], result)

    def test_multiple_services_combine(self):
        svcs = [
            _svc('s1', proto='tcp', backends=[_be('1.1.1.1', 80)]),
            _svc('s2', proto='udp', backends=[_be('2.2.2.2', 53)]),
        ]
        self.assertEqual(
            [('tcp', '1.1.1.1', 80), ('udp', '2.2.2.2', 53)],
            sorted(underlay._backend_endpoints(svcs)))


class TestReconcileDestinationAcl(testtools.TestCase):

    def setUp(self):
        super().setUp()
        p = mock.patch.object(underlay, '_exec_root')
        self.exec_root = p.start()
        self.addCleanup(p.stop)
        self.exec_root.return_value = ''

        # Make the chain-existence probe say "yes" so _ensure_chain
        # short-circuits.
        p = mock.patch.object(underlay, '_iptables_chain_exists',
                              return_value=True)
        self.chain_exists = p.start()
        self.addCleanup(p.stop)

    def _executed_iptables(self):
        """Return the list of (op, rule_args) tuples actually called."""
        out = []
        for call in self.exec_root.call_args_list:
            cmd = call.args[0]
            if cmd and cmd[0] == 'iptables':
                out.append(cmd)
        return out

    def test_flushes_chain(self):
        underlay.reconcile_destination_acl(NET_A, [])
        chain = underlay.per_net_chain_name(NET_A)
        cmds = self._executed_iptables()
        self.assertIn(['iptables', '-t', 'filter', '-F', chain], cmds)

    def test_appends_default_drop(self):
        underlay.reconcile_destination_acl(NET_A, [])
        chain = underlay.per_net_chain_name(NET_A)
        cmds = self._executed_iptables()
        self.assertIn(
            ['iptables', '-t', 'filter', '-A', chain, '-j', 'DROP'],
            cmds)

    def test_emits_rule_per_endpoint(self):
        services = [
            _svc('s1', proto='tcp',
                 backends=[_be('172.18.0.10', 80),
                           _be('172.18.0.11', 80)]),
        ]
        underlay.reconcile_destination_acl(NET_A, services)
        chain = underlay.per_net_chain_name(NET_A)
        cmds = self._executed_iptables()
        self.assertIn(
            ['iptables', '-t', 'filter', '-A', chain,
             '-d', '172.18.0.10', '-p', 'tcp', '--dport', '80',
             '-m', 'conntrack', '--ctstate', 'NEW', '-j', 'ACCEPT'],
            cmds)
        self.assertIn(
            ['iptables', '-t', 'filter', '-A', chain,
             '-d', '172.18.0.11', '-p', 'tcp', '--dport', '80',
             '-m', 'conntrack', '--ctstate', 'NEW', '-j', 'ACCEPT'],
            cmds)

    def test_idempotent_same_input(self):
        services = [
            _svc('s1', proto='tcp', backends=[_be('1.1.1.1', 80)]),
        ]
        underlay.reconcile_destination_acl(NET_A, services)
        first_calls = list(self._executed_iptables())
        self.exec_root.reset_mock()
        underlay.reconcile_destination_acl(NET_A, services)
        second_calls = list(self._executed_iptables())
        # Same input must produce the same call sequence (the flush+
        # repopulate semantics — there's no diff, but the call set
        # should be identical between runs).
        self.assertEqual(first_calls, second_calls)

    def test_tcp_udp_service_emits_both_protos(self):
        services = [
            _svc('s', proto='tcp-udp',
                 backends=[_be('172.18.42.10', 53)]),
        ]
        underlay.reconcile_destination_acl(NET_A, services)
        cmds = self._executed_iptables()
        protos_seen = set()
        for cmd in cmds:
            if '-p' in cmd:
                protos_seen.add(cmd[cmd.index('-p') + 1])
        self.assertIn('tcp', protos_seen)
        self.assertIn('udp', protos_seen)

    def test_disabled_service_emits_only_default_drop(self):
        services = [
            _svc('s', backends=[_be('1.1.1.1', 80)], enabled=False),
        ]
        underlay.reconcile_destination_acl(NET_A, services)
        chain = underlay.per_net_chain_name(NET_A)
        cmds = self._executed_iptables()
        accept_cmds = [c for c in cmds if c[-1] == 'ACCEPT']
        self.assertEqual([], accept_cmds)
        # Default DROP still installed.
        self.assertIn(
            ['iptables', '-t', 'filter', '-A', chain, '-j', 'DROP'],
            cmds)
