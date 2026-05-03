"""Unit tests for the netns/tap provisioning module.

The actual provisioning calls into ``neutron.agent.linux.ip_lib`` which
shells out to ``ip``/``netns`` — out of scope for unit tests. We mock
the ip_lib surface and the ovs_idl, and verify the sequence of calls
the module makes plus the values it derives from the Port_Binding row.

The set-diff CIDR reconciliation is the only "logic" worth testing
beyond mock-the-call-graph, so it gets dedicated cases.
"""

from unittest import mock

import testtools
from neutron.common.ovn import constants as ovn_const

from neutron_local_services import constants as lsc
from neutron_local_services.agent import netns


NET_ID = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
DEV_ID = lsc.DEVICE_ID_PREFIX + NET_ID
LOGICAL_PORT = '11112222-3333-4444-5555-666677778888'
MAC = 'fa:16:3e:00:00:01'
CIDR_V4 = '10.0.0.7/24'
CIDR_V6 = 'fd00::1/64'


def _row(mac_col=None, cidrs='10.0.0.7/24 fd00::1/64',
         device_id=DEV_ID, mtu='1450'):
    """Mock Port_Binding row exposing the attributes netns module touches."""
    row = mock.Mock(spec=['mac', 'logical_port', 'external_ids', 'type', 'uuid'])
    row.mac = mac_col if mac_col is not None else [
        f'{MAC} 10.0.0.7 fd00::1']
    row.logical_port = LOGICAL_PORT
    row.uuid = 'row-uuid'
    row.type = ovn_const.LSP_TYPE_LOCALPORT
    row.external_ids = {
        ovn_const.OVN_DEVID_EXT_ID_KEY: device_id,
        ovn_const.OVN_CIDRS_EXT_ID_KEY: cidrs,
        ovn_const.OVN_NETWORK_MTU_EXT_ID_KEY: mtu,
    }
    return row


class TestNaming(testtools.TestCase):

    def test_netns_name_uses_localsvc_prefix(self):
        self.assertEqual('localsvc-' + NET_ID, netns.netns_name(NET_ID))

    def test_veth_names_fit_ifnamsiz(self):
        # Linux interface names cap at IFNAMSIZ-1 == 15. Our scheme
        # gives 14 chars; if we ever break that, kernel will EINVAL.
        root, ns = netns.veth_names(NET_ID)
        self.assertEqual(14, len(root))
        self.assertEqual(14, len(ns))

    def test_veth_root_and_ns_distinguishable(self):
        root, ns = netns.veth_names(NET_ID)
        self.assertNotEqual(root, ns)
        self.assertTrue(root.endswith('0'))
        self.assertTrue(ns.endswith('1'))


class TestParsePortBinding(testtools.TestCase):

    def test_extracts_mac_cidrs_logical_port_mtu(self):
        mac, cidrs, lp, mtu = netns._parse_port_binding(_row())
        self.assertEqual(MAC, mac)
        # IPv6 should be filtered out.
        self.assertEqual([CIDR_V4], cidrs)
        self.assertEqual(LOGICAL_PORT, lp)
        self.assertEqual(1450, mtu)

    def test_empty_mtu_falls_back_to_zero(self):
        # Live data on the lab showed external_ids[neutron:mtu]='' on
        # our localport — empty string must not crash int().
        _, _, _, mtu = netns._parse_port_binding(_row(mtu=''))
        self.assertEqual(0, mtu)

    def test_garbage_mtu_falls_back_to_zero(self):
        _, _, _, mtu = netns._parse_port_binding(_row(mtu='not-a-number'))
        self.assertEqual(0, mtu)

    def test_no_mac_returns_none(self):
        # ovsdb represents an empty mac column as []. get_mac_and_ips
        # raises ValueError; we soft-fail.
        mac, cidrs, _, _ = netns._parse_port_binding(_row(mac_col=[]))
        self.assertIsNone(mac)
        self.assertEqual([], cidrs)


class TestProvision(testtools.TestCase):

    def setUp(self):
        super().setUp()
        # Patch the entire ip_lib + ovn_utils surface so no real shell-outs.
        p = mock.patch.object(netns, 'ip_lib')
        self.ip_lib = p.start(); self.addCleanup(p.stop)
        self.ovs_idl = mock.Mock()
        self.root_dev = mock.Mock()
        self.ns_dev = mock.Mock()
        # Default: no current addresses on namespace-side device.
        self.ns_dev.addr.list.return_value = []
        # Default: namespace-side veth doesn't exist yet → add_veth path.
        self.ip_lib.device_exists.return_value = False
        self.ip_lib.IPDevice.return_value = self.root_dev
        self.root_dev.exists.return_value = False
        wrapper = mock.Mock()
        wrapper.add_veth.return_value = (self.root_dev, self.ns_dev)
        self.ip_lib.IPWrapper.return_value = wrapper
        self.add_veth = wrapper.add_veth

    def test_provision_creates_veth_when_absent(self):
        ns = netns.provision(self.ovs_idl, 'br-int', _row())
        self.assertEqual('localsvc-' + NET_ID, ns)
        # add_veth was called with our two interface names.
        root, ns_veth = netns.veth_names(NET_ID)
        self.add_veth.assert_called_once_with(
            root, ns_veth, namespace2='localsvc-' + NET_ID)

    def test_provision_sets_mac_on_namespace_side(self):
        netns.provision(self.ovs_idl, 'br-int', _row())
        self.ns_dev.link.set_address.assert_called_once_with(MAC)

    def test_provision_brings_both_ends_up(self):
        netns.provision(self.ovs_idl, 'br-int', _row())
        self.root_dev.link.set_up.assert_called_once()
        self.ns_dev.link.set_up.assert_called_once()

    def test_provision_skips_mtu_when_zero(self):
        netns.provision(self.ovs_idl, 'br-int', _row(mtu=''))
        self.root_dev.link.set_mtu.assert_not_called()
        self.ns_dev.link.set_mtu.assert_not_called()

    def test_provision_sets_mtu_when_present(self):
        netns.provision(self.ovs_idl, 'br-int', _row(mtu='9000'))
        self.root_dev.link.set_mtu.assert_called_with(9000)
        self.ns_dev.link.set_mtu.assert_called_with(9000)

    def test_provision_adds_ipv4_cidr_on_first_run(self):
        netns.provision(self.ovs_idl, 'br-int', _row())
        self.ns_dev.addr.add_multiple.assert_called_once_with([CIDR_V4])
        self.ns_dev.addr.delete_multiple.assert_not_called()

    def test_provision_idempotent_when_already_correct(self):
        # Already-existing veth + matching CIDR → no add, no delete.
        self.ip_lib.device_exists.return_value = True
        self.ip_lib.IPDevice.side_effect = [self.root_dev, self.ns_dev]
        self.ns_dev.addr.list.return_value = [{'cidr': CIDR_V4}]
        netns.provision(self.ovs_idl, 'br-int', _row())
        self.add_veth.assert_not_called()
        self.ns_dev.addr.add_multiple.assert_not_called()
        self.ns_dev.addr.delete_multiple.assert_not_called()

    def test_provision_drops_stale_cidrs(self):
        # An old IP got left over → must be deleted.
        self.ip_lib.device_exists.return_value = True
        self.ip_lib.IPDevice.side_effect = [self.root_dev, self.ns_dev]
        self.ns_dev.addr.list.return_value = [
            {'cidr': '10.0.0.99/24'}, {'cidr': CIDR_V4}]
        netns.provision(self.ovs_idl, 'br-int', _row())
        self.ns_dev.addr.delete_multiple.assert_called_once_with(
            ['10.0.0.99/24'])
        self.ns_dev.addr.add_multiple.assert_not_called()

    def test_provision_ignores_ipv6_in_current_addresses(self):
        # IPv6 LLAs the kernel auto-adds must not be touched by the
        # IPv4 reconciler — otherwise we'd churn-delete them every pass.
        self.ip_lib.device_exists.return_value = True
        self.ip_lib.IPDevice.side_effect = [self.root_dev, self.ns_dev]
        self.ns_dev.addr.list.return_value = [
            {'cidr': CIDR_V4}, {'cidr': 'fe80::1/64'}]
        netns.provision(self.ovs_idl, 'br-int', _row())
        self.ns_dev.addr.delete_multiple.assert_not_called()

    def test_provision_plumbs_root_end_into_brint_with_iface_id(self):
        netns.provision(self.ovs_idl, 'br-int', _row())
        root, _ = netns.veth_names(NET_ID)
        self.ovs_idl.add_port.assert_called_once_with('br-int', root)
        self.ovs_idl.db_set.assert_called_once_with(
            'Interface', root,
            ('external_ids', {'iface-id': LOGICAL_PORT}))

    def test_provision_skips_when_no_mac(self):
        result = netns.provision(self.ovs_idl, 'br-int', _row(mac_col=[]))
        self.assertIsNone(result)
        self.add_veth.assert_not_called()
        self.ovs_idl.add_port.assert_not_called()

    def test_provision_skips_when_no_ipv4_cidr(self):
        # If external_ids only has v6 (or is empty), there's no on-subnet
        # nexthop — guests can't ARP us. Skip with a warning.
        result = netns.provision(self.ovs_idl, 'br-int',
                                 _row(cidrs='fd00::1/64'))
        self.assertIsNone(result)
        self.add_veth.assert_not_called()

    def test_provision_skips_non_marker_device_id(self):
        # Defensive: if a row without our marker somehow makes it past
        # match_fn, we still refuse to provision.
        result = netns.provision(self.ovs_idl, 'br-int',
                                 _row(device_id='ovnmeta-' + NET_ID))
        self.assertIsNone(result)

    def test_provision_deletes_orphan_root_veth_before_recreate(self):
        # Botched prior run can leave just the root-side veth. add_veth
        # would EEXIST without first cleaning it up.
        self.ip_lib.device_exists.return_value = False  # ns side absent
        self.root_dev.exists.return_value = True        # root side present
        netns.provision(self.ovs_idl, 'br-int', _row())
        self.root_dev.link.delete.assert_called_once()
        self.add_veth.assert_called_once()


class TestTeardown(testtools.TestCase):

    def setUp(self):
        super().setUp()
        p = mock.patch.object(netns, 'ip_lib')
        self.ip_lib = p.start(); self.addCleanup(p.stop)
        self.ovs_idl = mock.Mock()
        self.wrapper = mock.Mock()
        self.ip_lib.IPWrapper.return_value = self.wrapper

    def test_teardown_full_when_namespace_exists(self):
        self.wrapper.netns.exists.return_value = True
        self.ip_lib.device_exists.return_value = True
        ok = netns.teardown(self.ovs_idl, NET_ID)
        self.assertTrue(ok)
        root, _ = netns.veth_names(NET_ID)
        self.ovs_idl.del_port.assert_called_once_with(root)
        self.wrapper.garbage_collect_namespace.assert_called_once()

    def test_teardown_noop_when_nothing_exists(self):
        self.wrapper.netns.exists.return_value = False
        self.ip_lib.device_exists.return_value = False
        ok = netns.teardown(self.ovs_idl, NET_ID)
        self.assertFalse(ok)
        self.wrapper.garbage_collect_namespace.assert_not_called()

    def test_teardown_clears_orphan_root_veth_when_ns_missing(self):
        # Half-state: ns gone but root veth still in br-int. Must clean
        # up so a re-provision can succeed.
        self.wrapper.netns.exists.return_value = False
        self.ip_lib.device_exists.return_value = True
        netns.teardown(self.ovs_idl, NET_ID)
        root, _ = netns.veth_names(NET_ID)
        self.ovs_idl.del_port.assert_called_once_with(root)


class TestProvisionVipPartition(testtools.TestCase):
    """provision() must NOT touch /32 link-local VIPs that reconcile_vips owns.

    Together provision() and reconcile_vips() write to the same ns-side
    veth's address list. They partition by prefix length: provision
    handles ``/<32`` (on-subnet); reconcile_vips handles ``/32``. A bug
    where provision deleted "stale" /32s would cause an oscillation —
    reconcile_vips would re-add them every 10s, then provision (on next
    PB event) would delete them again. Lock that down here.
    """

    def setUp(self):
        super().setUp()
        p = mock.patch.object(netns, 'ip_lib')
        self.ip_lib = p.start(); self.addCleanup(p.stop)
        self.ovs_idl = mock.Mock()
        self.root_dev = mock.Mock()
        self.ns_dev = mock.Mock()
        self.ip_lib.device_exists.return_value = True
        self.ip_lib.IPDevice.side_effect = [self.root_dev, self.ns_dev]

    def test_provision_leaves_existing_vips_alone(self):
        # The on-subnet IP is already in place; a VIP is also already
        # in place from a prior reconcile_vips. provision must be a
        # no-op on both — neither in to_add nor to_del.
        self.ns_dev.addr.list.return_value = [
            {'cidr': CIDR_V4},
            {'cidr': '169.254.169.5/32'},
            {'cidr': '169.254.10.20/32'},
        ]
        netns.provision(self.ovs_idl, 'br-int', _row())
        self.ns_dev.addr.delete_multiple.assert_not_called()
        self.ns_dev.addr.add_multiple.assert_not_called()

    def test_provision_still_reconciles_on_subnet_with_vips_present(self):
        # Stale on-subnet IP must still be cleaned even when /32 VIPs
        # are present alongside.
        self.ns_dev.addr.list.return_value = [
            {'cidr': '10.0.0.99/24'},                    # stale, drop
            {'cidr': '169.254.169.5/32'},                # VIP, keep
        ]
        netns.provision(self.ovs_idl, 'br-int', _row())
        self.ns_dev.addr.delete_multiple.assert_called_once_with(
            ['10.0.0.99/24'])
        # And /32 VIPs must NOT appear in delete_multiple.
        for call in self.ns_dev.addr.delete_multiple.call_args_list:
            for cidr_list in call.args:
                for cidr in cidr_list:
                    self.assertFalse(cidr.endswith('/32'),
                                     'provision deleted a /32: %s' % cidr)


class TestReconcileVips(testtools.TestCase):
    """link-local /32 VIP reconciliation on the ns-side veth."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(netns, 'ip_lib')
        self.ip_lib = p.start(); self.addCleanup(p.stop)
        self.ns_dev = mock.Mock()
        self.ip_lib.IPDevice.return_value = self.ns_dev
        self.ip_lib.network_namespace_exists.return_value = True
        self.ip_lib.device_exists.return_value = True
        self.ns_dev.addr.list.return_value = []

    def test_adds_all_vips_on_first_run(self):
        netns.reconcile_vips(NET_ID, {'169.254.169.5/32',
                                      '169.254.10.10/32'})
        self.ns_dev.addr.add_multiple.assert_called_once()
        added = set(self.ns_dev.addr.add_multiple.call_args.args[0])
        self.assertEqual({'169.254.169.5/32', '169.254.10.10/32'}, added)
        self.ns_dev.addr.delete_multiple.assert_not_called()

    def test_idempotent_when_already_correct(self):
        self.ns_dev.addr.list.return_value = [
            {'cidr': '169.254.169.5/32'}]
        netns.reconcile_vips(NET_ID, {'169.254.169.5/32'})
        self.ns_dev.addr.add_multiple.assert_not_called()
        self.ns_dev.addr.delete_multiple.assert_not_called()

    def test_drops_stale_vips(self):
        # An old VIP got left from a prior binding that's been removed.
        self.ns_dev.addr.list.return_value = [
            {'cidr': '169.254.169.5/32'}, {'cidr': '169.254.99.99/32'}]
        netns.reconcile_vips(NET_ID, {'169.254.169.5/32'})
        self.ns_dev.addr.delete_multiple.assert_called_once_with(
            ['169.254.99.99/32'])
        self.ns_dev.addr.add_multiple.assert_not_called()

    def test_partial_changeover(self):
        # One VIP swapped for another in the same call.
        self.ns_dev.addr.list.return_value = [
            {'cidr': '169.254.10.10/32'}]
        netns.reconcile_vips(NET_ID, {'169.254.20.20/32'})
        self.ns_dev.addr.add_multiple.assert_called_once_with(
            ['169.254.20.20/32'])
        self.ns_dev.addr.delete_multiple.assert_called_once_with(
            ['169.254.10.10/32'])

    def test_skips_when_namespace_missing(self):
        # The 10s timer can race ahead of provision(); a missing ns
        # must not raise.
        self.ip_lib.network_namespace_exists.return_value = False
        added, removed = netns.reconcile_vips(NET_ID, {'169.254.1.1/32'})
        self.assertEqual(set(), added)
        self.assertEqual(set(), removed)
        self.ns_dev.addr.add_multiple.assert_not_called()

    def test_skips_when_veth_missing(self):
        # ns is up but the veth hasn't been moved into it yet — same
        # race-during-provision situation.
        self.ip_lib.device_exists.return_value = False
        added, removed = netns.reconcile_vips(NET_ID, {'169.254.1.1/32'})
        self.assertEqual(set(), added)
        self.assertEqual(set(), removed)

    def test_ignores_on_subnet_inputs(self):
        # Caller mistakenly passes a non-/32 CIDR — we filter rather
        # than error so a misconfigured plugin can't break the agent.
        netns.reconcile_vips(NET_ID, {'10.0.0.7/24', '169.254.5.5/32'})
        self.ns_dev.addr.add_multiple.assert_called_once_with(
            ['169.254.5.5/32'])

    def test_ignores_on_subnet_in_current_addresses(self):
        # On-subnet IP and IPv6 LLA already present must NOT be deleted
        # by reconcile_vips — provision() / kernel own them.
        self.ns_dev.addr.list.return_value = [
            {'cidr': '10.0.0.7/24'},
            {'cidr': 'fe80::1/64'},
            {'cidr': '169.254.169.5/32'},  # actually-stale VIP
        ]
        netns.reconcile_vips(NET_ID, set())  # no VIPs desired
        self.ns_dev.addr.delete_multiple.assert_called_once_with(
            ['169.254.169.5/32'])

    def test_returns_added_and_removed_sets(self):
        self.ns_dev.addr.list.return_value = [
            {'cidr': '169.254.99.99/32'}]
        added, removed = netns.reconcile_vips(
            NET_ID, {'169.254.5.5/32'})
        self.assertEqual({'169.254.5.5/32'}, added)
        self.assertEqual({'169.254.99.99/32'}, removed)


class TestSync(testtools.TestCase):
    """Startup sync: SB Port_Binding → set of provisioned netnses."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(netns, 'ip_lib')
        self.ip_lib = p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(netns, 'provision')
        self.provision = p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(netns, 'teardown')
        self.teardown = p.start(); self.addCleanup(p.stop)
        self.sb_idl = mock.Mock()
        self.ovs_idl = mock.Mock()

    def _sb_returns(self, *rows):
        chain = mock.Mock()
        chain.execute.return_value = list(rows)
        self.sb_idl.db_list_rows.return_value = chain

    def test_sync_provisions_each_localport(self):
        a = _row(); a.logical_port = 'lp-a'
        a.external_ids[ovn_const.OVN_DEVID_EXT_ID_KEY] = (
            lsc.DEVICE_ID_PREFIX + 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa')
        b = _row(); b.logical_port = 'lp-b'
        b.external_ids[ovn_const.OVN_DEVID_EXT_ID_KEY] = (
            lsc.DEVICE_ID_PREFIX + 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb')
        self._sb_returns(a, b)
        self.provision.return_value = 'localsvc-X'  # mock truthy ns
        self.ip_lib.list_network_namespaces.return_value = []
        netns.sync(self.sb_idl, self.ovs_idl, 'br-int')
        self.assertEqual(2, self.provision.call_count)

    def test_sync_skips_non_localport_rows(self):
        tenant = _row()
        tenant.type = ''  # tenant VIF
        self._sb_returns(tenant)
        self.ip_lib.list_network_namespaces.return_value = []
        netns.sync(self.sb_idl, self.ovs_idl, 'br-int')
        self.provision.assert_not_called()

    def test_sync_skips_non_marker_localports(self):
        # Octavia or metadata localports — match_fn at the agent layer
        # would reject; sync re-checks defensively.
        meta = _row(device_id='ovnmeta-' + NET_ID)
        self._sb_returns(meta)
        self.ip_lib.list_network_namespaces.return_value = []
        netns.sync(self.sb_idl, self.ovs_idl, 'br-int')
        self.provision.assert_not_called()

    def test_sync_tears_down_orphan_namespaces(self):
        # No localport rows but a stale localsvc- ns is on the box.
        self._sb_returns()
        orphan_net = 'cccccccc-cccc-cccc-cccc-cccccccccccc'
        self.ip_lib.list_network_namespaces.return_value = [
            'localsvc-' + orphan_net,
            'ovnmeta-something',  # not ours, must not touch
            'qrouter-other',
        ]
        netns.sync(self.sb_idl, self.ovs_idl, 'br-int')
        self.teardown.assert_called_once_with(self.ovs_idl, orphan_net)

    def test_sync_keeps_namespaces_with_active_localports(self):
        live_net = 'dddddddd-dddd-dddd-dddd-dddddddddddd'
        a = _row()
        a.external_ids[ovn_const.OVN_DEVID_EXT_ID_KEY] = (
            lsc.DEVICE_ID_PREFIX + live_net)
        self._sb_returns(a)
        self.provision.return_value = 'localsvc-' + live_net
        self.ip_lib.list_network_namespaces.return_value = [
            'localsvc-' + live_net]
        netns.sync(self.sb_idl, self.ovs_idl, 'br-int')
        self.teardown.assert_not_called()


