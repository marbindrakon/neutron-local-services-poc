"""Unit tests for the ovn-agent extension skeleton.

We don't have a live OVN agent under test here. Instead we cover the
predicates and event match logic — which is what determines whether
the right rows trigger provision/teardown work.

The Port_Binding row is duck-typed via ``mock.Mock`` with attribute
access for ``type``, ``logical_port``, and ``external_ids``. That
matches the ovsdbapp Row shape closely enough for this test layer.
"""

from unittest import mock

import testtools
from neutron.common.ovn import constants as ovn_const
from ovsdbapp.backend.ovs_idl import event as row_event

from neutron_local_services import constants as lsc
from neutron_local_services.agent import extension as ext


NET_ID = '11111111-1111-1111-1111-111111111111'
OUR_DEV_ID = lsc.DEVICE_ID_PREFIX + NET_ID  # ovn-lb-hm-localsvc-<net>
OCTAVIA_DEV_ID = 'ovn-lb-hm-some-octavia-id'


def _row(lsp_type=ovn_const.LSP_TYPE_LOCALPORT,
         device_id=OUR_DEV_ID, logical_port='lp-1'):
    """Mock Port_Binding row with the attributes our match_fn touches."""
    row = mock.Mock(spec=['type', 'logical_port', 'external_ids'])
    row.type = lsp_type
    row.logical_port = logical_port
    row.external_ids = {ovn_const.OVN_DEVID_EXT_ID_KEY: device_id}
    return row


class TestNetworkIdExtraction(testtools.TestCase):

    def test_extracts_network_uuid(self):
        self.assertEqual(NET_ID,
                         ext._network_id_from_device_id(OUR_DEV_ID))

    def test_returns_empty_for_octavia_device_id(self):
        # Real Octavia LB-HM device_ids share the lb-hm prefix but lack
        # the localsvc- marker. We must not pretend to extract anything.
        self.assertEqual('',
                         ext._network_id_from_device_id(OCTAVIA_DEV_ID))

    def test_returns_empty_for_random_device_id(self):
        self.assertEqual('', ext._network_id_from_device_id('compute:nova'))

    def test_returns_empty_for_blank(self):
        self.assertEqual('', ext._network_id_from_device_id(''))


class TestIsOurLocalport(testtools.TestCase):

    def test_matches_our_localport(self):
        self.assertTrue(ext._is_our_localport(_row()))

    def test_rejects_non_localport_type(self):
        # Tenant VIF, even with our device_id (won't happen in practice
        # but the check is cheap), should not trigger.
        self.assertFalse(ext._is_our_localport(_row(lsp_type='')))

    def test_rejects_octavia_localport(self):
        # Octavia would never realize as type=localport on our piggyback,
        # but if some future Octavia release did, we still wouldn't claim
        # it because the marker substring isn't there.
        self.assertFalse(ext._is_our_localport(_row(device_id=OCTAVIA_DEV_ID)))

    def test_rejects_metadata_localport(self):
        # The metadata service also uses type=localport but with a
        # different device_owner / device_id namespace. We must not fire
        # on its rows.
        self.assertFalse(
            ext._is_our_localport(_row(device_id='ovnmeta-' + NET_ID)))

    def test_handles_missing_external_ids(self):
        row = mock.Mock(spec=['type', 'logical_port', 'external_ids'])
        row.type = ovn_const.LSP_TYPE_LOCALPORT
        row.logical_port = 'lp-x'
        row.external_ids = None
        self.assertFalse(ext._is_our_localport(row))


class TestPortBindingEvents(testtools.TestCase):
    """Exercise the three Port_Binding event subclasses.

    The OVN agent instantiates each event class as ``EventClass(agent)``
    (see ``ovn_neutron_agent._load_sb_idl``). We pass a mock agent and
    confirm the events register against ``Port_Binding`` and reject
    non-matching rows.
    """

    def setUp(self):
        super().setUp()
        self.agent = mock.Mock()

    def test_create_event_registers_for_row_create(self):
        evt = ext.LocalportPortBindingCreateEvent(self.agent)
        self.assertEqual((row_event.RowEvent.ROW_CREATE,), evt.events)
        self.assertEqual('Port_Binding', evt.table)

    def test_update_event_registers_for_row_update(self):
        evt = ext.LocalportPortBindingUpdatedEvent(self.agent)
        self.assertEqual((row_event.RowEvent.ROW_UPDATE,), evt.events)
        self.assertEqual('Port_Binding', evt.table)

    def test_delete_event_registers_for_row_delete(self):
        evt = ext.LocalportPortBindingDeletedEvent(self.agent)
        self.assertEqual((row_event.RowEvent.ROW_DELETE,), evt.events)
        self.assertEqual('Port_Binding', evt.table)

    def test_match_fn_accepts_our_localport(self):
        evt = ext.LocalportPortBindingCreateEvent(self.agent)
        self.assertTrue(evt.match_fn(None, _row(), None))

    def test_match_fn_rejects_octavia(self):
        evt = ext.LocalportPortBindingUpdatedEvent(self.agent)
        self.assertFalse(
            evt.match_fn(None, _row(device_id=OCTAVIA_DEV_ID), None))

    def test_match_fn_rejects_tenant_port(self):
        evt = ext.LocalportPortBindingDeletedEvent(self.agent)
        self.assertFalse(evt.match_fn(None, _row(lsp_type=''), None))

    def test_run_logs_network_id_for_create(self):
        evt = ext.LocalportPortBindingCreateEvent(self.agent)
        with mock.patch.object(ext, 'LOG') as log:
            evt.run(None, _row(), None)
            log.info.assert_called_once()
            args = log.info.call_args.args
            # Expect the network UUID and the logical_port both to be in
            # the formatted log line — those are the only debug breadcrumbs
            # operators have in to confirm the watcher fired.
            self.assertIn(NET_ID, args)
            self.assertIn('lp-1', args)

    def test_run_logs_network_id_for_update(self):
        evt = ext.LocalportPortBindingUpdatedEvent(self.agent)
        with mock.patch.object(ext, 'LOG') as log:
            evt.run(None, _row(logical_port='lp-up'), None)
            args = log.info.call_args.args
            self.assertIn(NET_ID, args)
            self.assertIn('lp-up', args)

    def test_run_logs_network_id_for_delete(self):
        evt = ext.LocalportPortBindingDeletedEvent(self.agent)
        with mock.patch.object(ext, 'LOG') as log:
            evt.run(None, _row(logical_port='lp-del'), None)
            args = log.info.call_args.args
            self.assertIn(NET_ID, args)
            self.assertIn('lp-del', args)


class TestExtensionShape(testtools.TestCase):
    """The contract the OVN agent's extension manager enforces."""

    def setUp(self):
        super().setUp()
        self.extension = ext.LocalServicesExtension()

    def test_inherits_ovn_agent_extension(self):
        # The extension manager rejects anything that doesn't subclass
        # OVNAgentExtension; this is a structural canary.
        from neutron.agent.ovn.extensions import extension_manager
        self.assertIsInstance(
            self.extension, extension_manager.OVNAgentExtension)

    def test_name_is_set(self):
        self.assertIn('local-services', self.extension.name)

    def test_sb_idl_tables_includes_port_binding(self):
        # Without Port_Binding in this list the agent won't monitor it
        # and our events never fire.
        self.assertIn('Port_Binding', self.extension.sb_idl_tables)

    def test_sb_idl_events_returns_classes(self):
        # The OVN agent does ``e(self) for e in events`` — we must
        # return *classes*, not instances.
        events = self.extension.sb_idl_events
        self.assertEqual(3, len(events))
        for cls in events:
            self.assertTrue(callable(cls))
            # Each must be a subclass of our base Port_Binding event.
            self.assertTrue(issubclass(cls, ext._LocalServicesPortBindingEvent))

    def test_no_nb_idl_tables_or_events(self):
        # is SB-only. The NB watcher would only matter if we wanted
        # to react to mech-driver writes, which we don't.
        self.assertEqual([], self.extension.nb_idl_tables)
        self.assertEqual([], self.extension.nb_idl_events)

    def test_no_ovs_idl_events(self):
        self.assertEqual([], self.extension.ovs_idl_events)


class TestVipReconcilerWiring(testtools.TestCase):
    """reconciler entry points on the extension.

    The flow is: PB event → provision() → reconcile_vips_for_network →
    registry → netns.reconcile_vips. Each hop has a discrete failure
    mode the agent must survive (don't take the IDL thread down on a
    transient API blip; don't crash the loop on a missing namespace).
    """

    def setUp(self):
        super().setUp()
        self.extension = ext.LocalServicesExtension()
        # consume_api would normally get called by the OVN agent; stub
        # it out so ovs_idl/ovn_bridge property forwarding doesn't NPE.
        self.extension.agent_api = mock.Mock()
        # Stand-in registry; the test confirms call-shape only.
        self.fake_registry = mock.Mock()
        self.extension._registry = self.fake_registry
        # Patch netns at the module the extension imported it under.
        p = mock.patch.object(ext, 'netns')
        self.netns = p.start(); self.addCleanup(p.stop)

    def test_reconcile_calls_registry_then_netns(self):
        self.fake_registry.desired_vips_for_network.return_value = {
            '169.254.169.5/32'}
        self.extension.reconcile_vips_for_network(NET_ID)
        self.fake_registry.desired_vips_for_network.assert_called_once_with(
            NET_ID)
        self.netns.reconcile_vips.assert_called_once_with(
            NET_ID, {'169.254.169.5/32'})

    def test_reconcile_skips_blank_network_id(self):
        # Defensive: device_id parse can return '' when the marker is
        # missing. Don't fan out an API call for that.
        self.extension.reconcile_vips_for_network('')
        self.fake_registry.desired_vips_for_network.assert_not_called()
        self.netns.reconcile_vips.assert_not_called()

    def test_reconcile_swallows_registry_exception(self):
        # API blip → log and move on; netns is not called.
        self.fake_registry.desired_vips_for_network.side_effect = (
            RuntimeError('boom'))
        self.extension.reconcile_vips_for_network(NET_ID)  # no raise
        self.netns.reconcile_vips.assert_not_called()

    def test_reconcile_swallows_netns_exception(self):
        self.fake_registry.desired_vips_for_network.return_value = set()
        self.netns.reconcile_vips.side_effect = OSError('iproute2 fail')
        self.extension.reconcile_vips_for_network(NET_ID)  # no raise

    def test_create_event_triggers_reconcile_after_provision(self):
        # Events go through the combined ``reconcile_network``
        # entry point so plugins also tick. Spy on that one.
        evt = ext.LocalportPortBindingCreateEvent(self.extension)
        self.netns.provision.side_effect = None
        self.extension.reconcile_network = mock.Mock()
        evt.run(None, _row(), None)
        self.extension.reconcile_network.assert_called_once_with(NET_ID)

    def test_create_event_skips_reconcile_when_provision_raises(self):
        # An exception in provision should not cause us to chase VIPs
        # for a network whose tap isn't ready.
        evt = ext.LocalportPortBindingCreateEvent(self.extension)
        self.netns.provision.side_effect = OSError('rt-netlink down')
        self.extension.reconcile_network = mock.Mock()
        evt.run(None, _row(), None)
        self.extension.reconcile_network.assert_not_called()

    def test_delete_event_does_not_trigger_reconcile(self):
        # Tearing down the namespace drops every VIP with it; calling
        # reconcile after teardown would just thrash the registry for
        # no reason.
        evt = ext.LocalportPortBindingDeletedEvent(self.extension)
        self.extension.reconcile_network = mock.Mock()
        evt.run(None, _row(), None)
        self.extension.reconcile_network.assert_not_called()

    def test_list_managed_networks_filters_to_localsvc_prefix(self):
        with mock.patch.object(ext, 'ip_lib') as ip_lib_mock:
            ip_lib_mock.list_network_namespaces.return_value = [
                'localsvc-' + NET_ID,
                'ovnmeta-something',
                'qrouter-other',
                'localsvc-' + 'b' * 36,
            ]
            managed = self.extension._list_managed_networks()
        self.assertEqual({NET_ID, 'b' * 36}, managed)

    def test_periodic_reconcile_walks_managed_networks(self):
        # The periodic loop now goes through the combined entry point
        # ``reconcile_network`` so plugins also tick on the timer.
        with mock.patch.object(ext, 'ip_lib') as ip_lib_mock:
            ip_lib_mock.list_network_namespaces.return_value = [
                'localsvc-' + NET_ID]
            self.extension.reconcile_network = mock.Mock()
            self.extension._periodic_reconcile()
            self.extension.reconcile_network.assert_called_once_with(NET_ID)


class TestReconcileNetwork(testtools.TestCase):
    """combined reconciler: one fetch → both VIPs and plugins."""

    def setUp(self):
        super().setUp()
        # Real plugin module registered itself at import; tests need a
        # clean registry, then they install a fake plugin.
        from neutron_local_services.agent.plugins import base as plugins_base
        plugins_base.reset_for_tests()
        self.addCleanup(self._restore_real_plugins)
        self.plugins_base = plugins_base

        self.extension = ext.LocalServicesExtension()
        self.extension.agent_api = mock.Mock()
        self.fake_registry = mock.Mock()
        self.extension._registry = self.fake_registry

        p = mock.patch.object(ext, 'netns')
        self.netns = p.start(); self.addCleanup(p.stop)

        # Install a fake plugin under the lvs name so the reconciler
        # finds it. Tests inspect the calls to confirm dispatch shape.
        self.fake_lvs = mock.MagicMock()
        self.fake_lvs.name = lsc.EXPOSURE_NAT
        plugins_base._REGISTRY[lsc.EXPOSURE_NAT] = self.fake_lvs

    def _restore_real_plugins(self):
        # Re-import each plugin module to re-register it into the
        # registry the rest of the suite expects.
        self.plugins_base.reset_for_tests()
        # Force-reload so the module's import-time register() call
        # re-runs (Python's import cache would otherwise short-circuit
        # the second `from ... import` and skip the side effect).
        import importlib
        from neutron_local_services.agent.plugins import nat
        from neutron_local_services.agent.plugins import proxy
        importlib.reload(nat)
        importlib.reload(proxy)

    def _svc(self, **kw):
        base = {'id': 'svc-aa', 'local_ipv4': '169.254.169.5',
                'port': 53, 'protocol': lsc.PROTO_UDP,
                'exposure_plugin': lsc.EXPOSURE_NAT,
                'enabled': True, 'backends': []}
        base.update(kw)
        return base

    def test_one_fetch_drives_both_paths(self):
        # The whole point of the combined entry: one API call powers
        # vips and plugin config. A regression where this re-fetched
        # twice would silently double the agent's API load.
        self.fake_registry.desired_state_for_network.return_value = [
            self._svc()]
        self.extension.reconcile_network(NET_ID)
        self.fake_registry.desired_state_for_network.assert_called_once_with(
            NET_ID)
        self.netns.reconcile_vips.assert_called_once_with(
            NET_ID, {'169.254.169.5/32'})
        self.fake_lvs.apply_config.assert_called_once_with(
            'localsvc-' + NET_ID, [self.fake_registry.desired_state_for_network
                                   .return_value[0]])

    def test_groups_services_by_exposure_plugin(self):
        envoy_plugin = mock.MagicMock(); envoy_plugin.name = lsc.EXPOSURE_PROXY
        self.plugins_base._REGISTRY[lsc.EXPOSURE_PROXY] = envoy_plugin

        svc_lvs = self._svc(id='svc-lvs', exposure_plugin=lsc.EXPOSURE_NAT)
        svc_envoy = self._svc(id='svc-envoy',
                              local_ipv4='169.254.169.6',
                              exposure_plugin=lsc.EXPOSURE_PROXY)
        self.fake_registry.desired_state_for_network.return_value = [
            svc_lvs, svc_envoy]
        self.extension.reconcile_network(NET_ID)

        self.fake_lvs.apply_config.assert_called_once_with(
            'localsvc-' + NET_ID, [svc_lvs])
        envoy_plugin.apply_config.assert_called_once_with(
            'localsvc-' + NET_ID, [svc_envoy])
        # And both VIPs reach the netns.
        self.netns.reconcile_vips.assert_called_once_with(
            NET_ID, {'169.254.169.5/32', '169.254.169.6/32'})

    def test_calls_apply_with_empty_list_to_let_plugin_clean_up(self):
        # No services: the LVS plugin still needs to know so it can
        # render an empty config (or drop one). This is what happens
        # when the last service on a network is unbound but the netns
        # still exists (e.g., another plugin holds it open).
        self.fake_registry.desired_state_for_network.return_value = []
        self.extension.reconcile_network(NET_ID)
        self.fake_lvs.apply_config.assert_called_once_with(
            'localsvc-' + NET_ID, [])

    def test_unknown_plugin_logged_and_skipped(self):
        # Operator created a service with exposure_plugin=envoy but
        # the agent only loaded LVS. We log and move on rather than
        # crash the reconcile pass for the LVS services in the same
        # call.
        svc = self._svc(exposure_plugin=lsc.EXPOSURE_PROXY)
        self.fake_registry.desired_state_for_network.return_value = [svc]
        with mock.patch.object(ext, 'LOG') as log:
            self.extension.reconcile_network(NET_ID)
        # warning fired
        self.assertTrue(log.warning.called)
        # LVS plugin was called with an empty list (no services for it).
        self.fake_lvs.apply_config.assert_called_once_with(
            'localsvc-' + NET_ID, [])
        # And VIPs still reconcile (the kernel needs them even if no
        # plugin claims the service — guests would otherwise lose ARP
        # responses for the configured VIP).
        self.netns.reconcile_vips.assert_called_once_with(
            NET_ID, {'169.254.169.5/32'})

    def test_swallows_registry_exception(self):
        self.fake_registry.desired_state_for_network.side_effect = (
            RuntimeError('boom'))
        self.extension.reconcile_network(NET_ID)  # no raise
        self.netns.reconcile_vips.assert_not_called()
        self.fake_lvs.apply_config.assert_not_called()

    def test_swallows_plugin_exception(self):
        # One plugin's failure must not skip the others. Add a second
        # plugin that explodes; LVS still runs.
        boom = mock.MagicMock(); boom.name = lsc.EXPOSURE_PROXY
        boom.apply_config.side_effect = RuntimeError('plugin oops')
        self.plugins_base._REGISTRY[lsc.EXPOSURE_PROXY] = boom
        svc = self._svc()
        self.fake_registry.desired_state_for_network.return_value = [svc]
        self.extension.reconcile_network(NET_ID)  # no raise
        self.fake_lvs.apply_config.assert_called_once()
        boom.apply_config.assert_called_once()  # was called, even if it raised

    def test_skips_blank_network_id(self):
        self.extension.reconcile_network('')
        self.fake_registry.desired_state_for_network.assert_not_called()
        self.netns.reconcile_vips.assert_not_called()
        self.fake_lvs.apply_config.assert_not_called()

    def test_create_event_calls_combined_reconcile(self):
        # PB CREATE event must drive plugin apply too, not just VIPs.
        evt = ext.LocalportPortBindingCreateEvent(self.extension)
        self.netns.provision.side_effect = None
        self.extension.reconcile_network = mock.Mock()
        evt.run(None, _row(), None)
        self.extension.reconcile_network.assert_called_once_with(NET_ID)

    def test_delete_event_tears_plugins_down_before_netns(self):
        # On binding-removed, plugins SIGTERM their processes BEFORE
        # the agent destroys the namespace (which would orphan or
        # truncate-kill them). Order matters here — the test enforces
        # both presence and call order.
        evt = ext.LocalportPortBindingDeletedEvent(self.extension)
        call_order = []
        self.fake_lvs.teardown.side_effect = (
            lambda *a, **kw: call_order.append('plugin'))
        self.netns.teardown.side_effect = (
            lambda *a, **kw: call_order.append('netns'))
        evt.run(None, _row(), None)
        self.assertEqual(['plugin', 'netns'], call_order)
        self.fake_lvs.teardown.assert_called_once_with('localsvc-' + NET_ID)
