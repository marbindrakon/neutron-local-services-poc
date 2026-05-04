"""Unit tests for the localport piggyback helpers and the plugin's
binding-lifecycle hooks.

The core_plugin and mech_driver are mocked out — these tests verify
our logic (idempotent create, marker-based scan, cleanup-on-last-
unbinding, LSP-type verification rollback) without needing a live
Neutron stack.
"""

from unittest import mock

import testtools
from neutron.common.ovn import constants as ovn_const
from oslo_config import cfg
from oslo_config import fixture as config_fixture

from neutron_local_services import constants as lsc
from neutron_local_services.ovn import localport as lp
# Import the plugin module at module-load time. The neutron import
# chain it triggers registers the `service_providers.service_provider`
# opt; doing it here (vs. inside setUp) avoids a DuplicateOptError when
# the cfg_fixture pre-registers that opt before the neutron import runs.
from neutron_local_services.plugin import plugin as plugin_mod


NET_ID = '11111111-1111-1111-1111-111111111111'
SUBNET_ID = '22222222-2222-2222-2222-222222222222'
PORT_ID = '33333333-3333-3333-3333-333333333333'


def _make_subnet(enable_dhcp=True, ip_version=4, sid=SUBNET_ID):
    return {'id': sid, 'network_id': NET_ID,
            'enable_dhcp': enable_dhcp, 'ip_version': ip_version,
            'cidr': '10.0.0.0/24'}


def _make_network():
    return {'id': NET_ID, 'project_id': 'tproj'}


def _make_our_port(pid=PORT_ID, network_id=NET_ID, marker=lsc.DEVICE_ID_MARKER):
    return {
        'id': pid,
        'network_id': network_id,
        'device_owner': ovn_const.OVN_LB_HM_PORT_DISTRIBUTED,
        'device_id': lsc.DEVICE_ID_PREFIX + network_id,
        'fixed_ips': [{'subnet_id': SUBNET_ID, 'ip_address': '10.0.0.5'}],
    }


def _make_octavia_port(pid='octavia-port-id'):
    """A real Octavia LB-HM port — same device_owner, no marker."""
    return {
        'id': pid,
        'network_id': NET_ID,
        'device_owner': ovn_const.OVN_LB_HM_PORT_DISTRIBUTED,
        'device_id': 'ovn-lb-hm-some-octavia-id',
        'fixed_ips': [{'subnet_id': SUBNET_ID, 'ip_address': '10.0.0.6'}],
    }


class TestDeviceIdAndMatch(testtools.TestCase):

    def test_device_id_starts_with_lb_hm_prefix(self):
        # Critical: the OVN mech driver matches 'ovn-lb-hm' as the
        # prefix on device_id. If our prefix doesn't begin with that,
        # the LSP won't be created as a localport.
        self.assertTrue(
            lp.device_id_for(NET_ID).startswith('ovn-lb-hm'))

    def test_device_id_contains_marker(self):
        self.assertIn(lsc.DEVICE_ID_MARKER, lp.device_id_for(NET_ID))

    def test_device_id_contains_network(self):
        self.assertIn(NET_ID, lp.device_id_for(NET_ID))

    def test_is_our_port_matches_marker(self):
        self.assertTrue(lp.is_our_port(_make_our_port()))

    def test_is_our_port_rejects_octavia(self):
        # Real Octavia LB-HM port shares device_owner but lacks our
        # marker. We must not claim it.
        self.assertFalse(lp.is_our_port(_make_octavia_port()))

    def test_is_our_port_rejects_random_port(self):
        self.assertFalse(lp.is_our_port({
            'device_owner': 'compute:nova', 'device_id': 'whatever'}))


class TestFindPort(testtools.TestCase):

    def test_finds_our_port(self):
        core = mock.Mock()
        core.get_ports.return_value = [_make_our_port()]
        result = lp.find_port(core, mock.Mock(), NET_ID)
        self.assertEqual(PORT_ID, result['id'])

    def test_returns_none_when_absent(self):
        core = mock.Mock()
        core.get_ports.return_value = []
        self.assertIsNone(lp.find_port(core, mock.Mock(), NET_ID))

    def test_skips_octavia_port_even_if_filter_returns_it(self):
        # Defensive: even if some bug surfaces an Octavia port to us,
        # is_our_port double-checks the marker and we skip it.
        core = mock.Mock()
        core.get_ports.return_value = [_make_octavia_port()]
        self.assertIsNone(lp.find_port(core, mock.Mock(), NET_ID))

    def test_filter_targets_our_device_id(self):
        # The DB-layer filter should narrow on device_id, not just
        # device_owner — both for performance and to avoid scanning
        # any real LB-HM ports.
        core = mock.Mock()
        core.get_ports.return_value = []
        lp.find_port(core, mock.Mock(), NET_ID)
        kwargs = core.get_ports.call_args.kwargs
        filt = kwargs.get('filters') or core.get_ports.call_args.args[1]
        self.assertEqual([NET_ID], filt['network_id'])
        self.assertEqual([lp.device_id_for(NET_ID)], filt['device_id'])
        self.assertEqual(
            [ovn_const.OVN_LB_HM_PORT_DISTRIBUTED], filt['device_owner'])


class TestEnsureLocalport(testtools.TestCase):

    def setUp(self):
        super().setUp()
        self.core = mock.Mock()
        self.context = mock.Mock()
        # Default: no existing port, one DHCP-enabled IPv4 subnet.
        self.core.get_ports.return_value = []
        self.core.get_subnets.return_value = [_make_subnet()]
        self.core.create_port.return_value = _make_our_port()
        # Patch out p_utils.create_port → core.create_port shim so we
        # can assert on the body shape directly.
        self.create_port_patch = mock.patch.object(
            lp.p_utils, 'create_port',
            side_effect=lambda plug, ctx, body: _make_our_port())
        self.create_port_mock = self.create_port_patch.start()
        self.addCleanup(self.create_port_patch.stop)

    def test_creates_when_absent(self):
        port = lp.ensure_localport(self.core, self.context, _make_network())
        self.assertEqual(PORT_ID, port['id'])
        self.assertEqual(1, self.create_port_mock.call_count)
        body = self.create_port_mock.call_args.args[2]['port']
        self.assertEqual(NET_ID, body['network_id'])
        self.assertEqual(
            ovn_const.OVN_LB_HM_PORT_DISTRIBUTED, body['device_owner'])
        self.assertEqual(lp.device_id_for(NET_ID), body['device_id'])
        self.assertEqual(False, body['port_security_enabled'])
        self.assertEqual(
            [{'subnet_id': SUBNET_ID}], body['fixed_ips'])

    def test_idempotent_when_present(self):
        self.core.get_ports.return_value = [_make_our_port()]
        port = lp.ensure_localport(self.core, self.context, _make_network())
        self.assertEqual(PORT_ID, port['id'])
        self.assertEqual(0, self.create_port_mock.call_count)

    def test_prefers_dhcp_enabled_subnet(self):
        # Two subnets — one without DHCP, one with. Must pick the DHCP
        # one so the host_routes injector has somewhere to land.
        no_dhcp = _make_subnet(enable_dhcp=False, sid='no-dhcp-sub')
        with_dhcp = _make_subnet(enable_dhcp=True, sid='dhcp-sub')
        self.core.get_subnets.return_value = [no_dhcp, with_dhcp]
        lp.ensure_localport(self.core, self.context, _make_network())
        body = self.create_port_mock.call_args.args[2]['port']
        self.assertEqual([{'subnet_id': 'dhcp-sub'}], body['fixed_ips'])

    def test_falls_back_to_non_dhcp_subnet_with_warning(self):
        no_dhcp = _make_subnet(enable_dhcp=False, sid='only-sub')
        self.core.get_subnets.return_value = [no_dhcp]
        with mock.patch.object(lp.LOG, 'warning') as warn:
            lp.ensure_localport(self.core, self.context, _make_network())
        body = self.create_port_mock.call_args.args[2]['port']
        self.assertEqual([{'subnet_id': 'only-sub'}], body['fixed_ips'])
        self.assertTrue(warn.called)

    def test_skips_ipv6_subnet(self):
        v6 = _make_subnet(ip_version=6, sid='v6-sub')
        v4 = _make_subnet(ip_version=4, sid='v4-sub')
        self.core.get_subnets.return_value = [v6, v4]
        lp.ensure_localport(self.core, self.context, _make_network())
        body = self.create_port_mock.call_args.args[2]['port']
        self.assertEqual([{'subnet_id': 'v4-sub'}], body['fixed_ips'])

    def test_raises_when_no_ipv4_subnet(self):
        self.core.get_subnets.return_value = [
            _make_subnet(ip_version=6, sid='v6-only')]
        from neutron_lib import exceptions as n_exc
        self.assertRaises(
            n_exc.InvalidInput,
            lp.ensure_localport, self.core, self.context, _make_network())
        self.assertEqual(0, self.create_port_mock.call_count)


class TestMaybeRemoveLocalport(testtools.TestCase):

    def setUp(self):
        super().setUp()
        self.core = mock.Mock()
        self.context = mock.Mock()

    def test_keeps_port_when_bindings_remain(self):
        self.core.get_ports.return_value = [_make_our_port()]
        removed = lp.maybe_remove_localport(
            self.core, self.context, NET_ID,
            has_remaining_bindings=True)
        self.assertFalse(removed)
        self.core.delete_port.assert_not_called()

    def test_deletes_port_when_no_bindings_remain(self):
        self.core.get_ports.return_value = [_make_our_port()]
        removed = lp.maybe_remove_localport(
            self.core, self.context, NET_ID,
            has_remaining_bindings=False)
        self.assertTrue(removed)
        self.core.delete_port.assert_called_once_with(self.context, PORT_ID)

    def test_noop_when_port_already_absent(self):
        self.core.get_ports.return_value = []
        removed = lp.maybe_remove_localport(
            self.core, self.context, NET_ID,
            has_remaining_bindings=False)
        self.assertFalse(removed)
        self.core.delete_port.assert_not_called()


class TestVerifyLSPType(testtools.TestCase):

    def test_no_mech_driver_returns_none(self):
        self.assertIsNone(lp.verify_lsp_type(None, PORT_ID, NET_ID))

    def test_no_nb_idl_returns_none(self):
        md = mock.Mock(spec=['nb_ovn'])
        md.nb_ovn = None
        self.assertIsNone(lp.verify_lsp_type(md, PORT_ID, NET_ID))

    def test_passes_when_type_is_localport(self):
        md = mock.Mock()
        lsp = mock.Mock(type=ovn_const.LSP_TYPE_LOCALPORT)
        md.nb_ovn.lookup.return_value = lsp
        result = lp.verify_lsp_type(md, PORT_ID, NET_ID)
        self.assertEqual(ovn_const.LSP_TYPE_LOCALPORT, result)

    def test_raises_when_type_wrong(self):
        # If the LSP exists but isn't a localport, the LB-HM piggyback
        # is broken — fail loudly so we notice during stack tests.
        md = mock.Mock()
        lsp = mock.Mock(type='')  # plain LSP, no special type
        md.nb_ovn.lookup.return_value = lsp
        self.assertRaises(
            lp.LocalportLSPVerifyError,
            lp.verify_lsp_type, md, PORT_ID, NET_ID, retries=1, delay=0)

    def test_returns_none_when_lsp_never_appears(self):
        # Mech driver sync race: log a warning, return None, don't
        # block the API call. Real production wants to wait on a
        # callback rather than poll, but PoC accepts the soft fail.
        md = mock.Mock()
        md.nb_ovn.lookup.return_value = None
        with mock.patch.object(lp.LOG, 'warning'):
            result = lp.verify_lsp_type(
                md, PORT_ID, NET_ID, retries=2, delay=0)
        self.assertIsNone(result)


class TestPluginBindingHooks(testtools.TestCase):
    """End-to-end hook wiring on LocalServicesPlugin: when a binding is
    created/deleted, the right localport calls fire."""

    def setUp(self):
        super().setUp()
        self.cfg_fixture = self.useFixture(config_fixture.Config(cfg.CONF))
        try:
            self.cfg_fixture.register_opt(
                cfg.ListOpt('service_provider', default=[]),
                group='service_providers')
        except cfg.DuplicateOptError:
            pass
        # Block the periodic reconciler from starting under test; we
        # don't want a stray greenthread firing _reconcile_loop in the
        # background.
        self.reconciler_patch = mock.patch.object(
            plugin_mod.LocalServicesPlugin, '_start_reconciler')
        self.reconciler_patch.start()
        self.addCleanup(self.reconciler_patch.stop)
        # Build the plugin fresh; patch directory.get_plugin so the
        # _core_plugin property resolves to our mock.
        self.plugin_mod = plugin_mod
        self.plugin = plugin_mod.LocalServicesPlugin()
        self.core = mock.Mock()
        self.core.get_network.return_value = _make_network()
        self.core.get_subnets.return_value = [_make_subnet()]
        self.core.get_ports.return_value = []
        # Pre-set the cached refs so `_core_plugin` and `_mech_driver`
        # don't try to reach into a real Neutron plugin directory.
        self.plugin._core_plugin_ref = self.core
        # Sentinel: forces _mech_driver to return None → verify_lsp_type
        # short-circuits, which is what we want in unit tests.
        self.plugin._mech_driver_ref = self.plugin._MECH_DRIVER_UNAVAILABLE
        self.create_port_patch = mock.patch.object(
            lp.p_utils, 'create_port',
            side_effect=lambda plug, ctx, body: _make_our_port())
        self.create_port_mock = self.create_port_patch.start()
        self.addCleanup(self.create_port_patch.stop)
        # tests aren't exercising the host_routes path; stub it
        # to a no-op so we don't have to mock the binding/service
        # lookups it uses. Dedicated coverage lives in
        # test_host_routes.py.
        self.refresh_patch = mock.patch.object(
            self.plugin, '_refresh_subnet_routes')
        self.refresh_mock = self.refresh_patch.start()
        self.addCleanup(self.refresh_patch.stop)
        # _reconcile_network -> _enabled_services_for_network calls
        # both get_local_service_bindings and get_local_services. Tests
        # in this class only care about the binding-driven path, so
        # default opt-out catalog is empty unless a test overrides.
        self.get_services_patch = mock.patch.object(
            self.plugin, 'get_local_services', return_value=[])
        self.get_services_patch.start()
        self.addCleanup(self.get_services_patch.stop)
        # Default: no bindings on the network. Tests override per case.
        self.get_bindings_patch = mock.patch.object(
            self.plugin, 'get_local_service_bindings', return_value=[])
        self.get_bindings_patch.start()
        self.addCleanup(self.get_bindings_patch.stop)

    def _enabled_opt_in_svc(self, sid='svc'):
        """Stage an enabled opt-in service in the catalog mocks so
        ``_enabled_services_for_network`` sees the binding's service
        as effective."""
        svc = {'id': sid, 'enabled': True,
               'attachment_policy': 'opt-in', 'local_ipv4': '1.1.1.1'}
        # get_local_service is keyed by id; the simple stub returns
        # the same svc for any id since these tests only use one.
        self.plugin.get_local_service = mock.Mock(return_value=svc)
        return svc

    def test_create_binding_ensures_localport(self):
        ctx = mock.Mock()
        binding_dict = {
            'id': 'binding-id', 'network_id': NET_ID,
            'service_id': 'svc', 'project_id': 'tproj', 'enabled': True}
        super_create = mock.Mock(return_value=binding_dict)
        # Post-create the binding is in the catalog: the reconcile pass
        # must see it as an enabled opt-in attachment so it ensures the
        # localport.
        self.get_bindings_patch.stop()
        self.plugin.get_local_service_bindings = mock.Mock(
            return_value=[binding_dict])
        self._enabled_opt_in_svc()
        with mock.patch.object(
                self.plugin_mod.local_services_db.LocalServicesDbMixin,
                'create_local_service_binding', super_create):
            result = self.plugin.create_local_service_binding(
                ctx, {'local_service_binding': {
                    'service_id': 'svc', 'network_id': NET_ID}})
        self.assertEqual('binding-id', result['id'])
        self.assertEqual(1, self.create_port_mock.call_count)

    def test_create_binding_rolls_back_on_localport_failure(self):
        ctx = mock.Mock()
        binding_dict = {
            'id': 'binding-id', 'network_id': NET_ID,
            'service_id': 'svc', 'project_id': 'tproj', 'enabled': True}
        super_create = mock.Mock(return_value=binding_dict)
        super_delete = mock.Mock()
        # Force ensure_localport to fail by making create_port raise.
        self.create_port_mock.side_effect = RuntimeError('boom')
        # Post-create the binding is in the catalog so reconcile tries
        # to ensure the localport.
        self.get_bindings_patch.stop()
        self.plugin.get_local_service_bindings = mock.Mock(
            return_value=[binding_dict])
        self._enabled_opt_in_svc()
        with mock.patch.object(
                self.plugin_mod.local_services_db.LocalServicesDbMixin,
                'create_local_service_binding', super_create), \
             mock.patch.object(
                self.plugin_mod.local_services_db.LocalServicesDbMixin,
                'delete_local_service_binding', super_delete):
            self.assertRaises(
                RuntimeError,
                self.plugin.create_local_service_binding,
                ctx, {'local_service_binding': {
                    'service_id': 'svc', 'network_id': NET_ID}})
        super_delete.assert_called_once_with(ctx, 'binding-id')

    def test_delete_binding_keeps_port_when_others_remain(self):
        ctx = mock.Mock()
        binding = {'id': 'binding-id', 'network_id': NET_ID,
                   'service_id': 'svc', 'project_id': 'tproj',
                   'enabled': True}
        other_binding = {'id': 'other-binding', 'network_id': NET_ID,
                         'service_id': 'other-svc', 'project_id': 'tproj',
                         'enabled': True}
        # Pretend a different binding still exists on the network.
        get_bindings = mock.Mock(return_value=[other_binding])
        super_delete = mock.Mock()
        self.core.get_ports.return_value = [_make_our_port()]
        self._enabled_opt_in_svc(sid='other-svc')
        with mock.patch.object(
                self.plugin, 'get_local_service_binding',
                return_value=binding), \
             mock.patch.object(
                self.plugin_mod.local_services_db.LocalServicesDbMixin,
                'delete_local_service_binding', super_delete), \
             mock.patch.object(
                self.plugin, 'get_local_service_bindings', get_bindings):
            self.plugin.delete_local_service_binding(ctx, 'binding-id')
        self.core.delete_port.assert_not_called()

    def test_delete_binding_removes_port_when_last(self):
        ctx = mock.Mock()
        binding = {'id': 'binding-id', 'network_id': NET_ID,
                   'service_id': 'svc', 'project_id': 'tproj',
                   'enabled': True}
        get_bindings = mock.Mock(return_value=[])  # No bindings remain
        super_delete = mock.Mock()
        self.core.get_ports.return_value = [_make_our_port()]
        with mock.patch.object(
                self.plugin, 'get_local_service_binding',
                return_value=binding), \
             mock.patch.object(
                self.plugin_mod.local_services_db.LocalServicesDbMixin,
                'delete_local_service_binding', super_delete), \
             mock.patch.object(
                self.plugin, 'get_local_service_bindings', get_bindings):
            self.plugin.delete_local_service_binding(ctx, 'binding-id')
        self.core.delete_port.assert_called_once_with(ctx, PORT_ID)
