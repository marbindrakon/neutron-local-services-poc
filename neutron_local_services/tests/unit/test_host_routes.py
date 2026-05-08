"""Unit tests for the host_routes module and the plugin's
``_refresh_subnet_routes`` orchestration.

The merge helpers are pure; the registry handlers and plugin hook are
exercised against a mocked core_plugin + plugin instance, with
``localport.find_port`` patched to control whether a localport is
"present".
"""

from unittest import mock

import testtools
from neutron_lib.callbacks import events
from oslo_config import cfg
from oslo_config import fixture as config_fixture

from neutron_local_services import constants as lsc
from neutron_local_services import host_routes as hr
from neutron_local_services.ovn import localport as lp
from neutron_local_services.plugin import plugin as plugin_mod


NET_ID = '11111111-1111-1111-1111-111111111111'
SUBNET_ID = '22222222-2222-2222-2222-222222222222'
PORT_ID = '33333333-3333-3333-3333-333333333333'


def _subnet(sid=SUBNET_ID, network_id=NET_ID, host_routes=None,
            enable_dhcp=True, ip_version=4, cidr='10.0.0.0/24'):
    return {'id': sid, 'network_id': network_id,
            'host_routes': list(host_routes or []),
            'enable_dhcp': enable_dhcp, 'ip_version': ip_version,
            'cidr': cidr}


def _our_port(ip='10.0.0.7', sid=SUBNET_ID):
    return {'id': PORT_ID, 'network_id': NET_ID,
            'fixed_ips': [{'subnet_id': sid, 'ip_address': ip}]}


def _service(vip='169.254.169.5', enabled=True):
    return {'id': 'svc', 'local_ipv4': vip, 'enabled': enabled}


class TestMerge(testtools.TestCase):

    def test_appends_service_routes_when_existing_empty(self):
        srv = [{'destination': '169.254.169.5/32', 'nexthop': '10.0.0.7'}]
        out = hr.merge([], srv, '10.0.0.7')
        self.assertEqual(srv, out)

    def test_preserves_unrelated_tenant_routes(self):
        existing = [{'destination': '192.0.2.0/24', 'nexthop': '10.0.0.1'}]
        srv = [{'destination': '169.254.169.5/32', 'nexthop': '10.0.0.7'}]
        out = hr.merge(existing, srv, '10.0.0.7')
        self.assertEqual(existing + srv, out)

    def test_service_wins_on_destination_conflict(self):
        # Tenant tried to set the same destination — service wins.
        existing = [{'destination': '169.254.169.5/32',
                     'nexthop': '10.0.0.99'}]  # tenant-set, wrong nexthop
        srv = [{'destination': '169.254.169.5/32', 'nexthop': '10.0.0.7'}]
        out = hr.merge(existing, srv, '10.0.0.7')
        self.assertEqual(srv, out)

    def test_drops_stale_own_route_when_service_unbound(self):
        # We previously injected a route for 169.254.169.5; the service
        # has been unbound, so service_routes no longer includes it.
        # The route's nexthop is OUR localport IP, which is the
        # invariant we use to identify our own past handiwork.
        existing = [
            {'destination': '169.254.169.5/32', 'nexthop': '10.0.0.7'},
            {'destination': '192.0.2.0/24', 'nexthop': '10.0.0.1'},
        ]
        out = hr.merge(existing, [], '10.0.0.7')
        # Stale 169.254.169.5 dropped; tenant route preserved.
        self.assertEqual(
            [{'destination': '192.0.2.0/24', 'nexthop': '10.0.0.1'}], out)

    def test_keeps_route_with_unrelated_nexthop(self):
        # A route to the same destination but different nexthop is
        # NOT identified as ours — only nexthop == localport_ip is
        # ours. (In practice this case is moot because destination
        # conflict still drops it via the desired_dests check, but we
        # want the nexthop logic itself to be conservative.)
        existing = [{'destination': '198.51.100.0/24', 'nexthop': '10.0.0.1'}]
        out = hr.merge(existing, [], '10.0.0.7')
        self.assertEqual(existing, out)

    def test_idempotent_on_already_merged(self):
        existing = [
            {'destination': '192.0.2.0/24', 'nexthop': '10.0.0.1'},
            {'destination': '169.254.169.5/32', 'nexthop': '10.0.0.7'},
        ]
        srv = [{'destination': '169.254.169.5/32', 'nexthop': '10.0.0.7'}]
        out = hr.merge(existing, srv, '10.0.0.7')
        # Re-running with the same input yields the same shape.
        self.assertEqual(existing, out)


class TestRoutesEqual(testtools.TestCase):

    def test_equal_when_same(self):
        a = [{'destination': 'A', 'nexthop': 'X'}]
        self.assertTrue(hr.routes_equal(a, list(a)))

    def test_equal_ignoring_order(self):
        a = [{'destination': 'A', 'nexthop': 'X'},
             {'destination': 'B', 'nexthop': 'Y'}]
        b = [{'destination': 'B', 'nexthop': 'Y'},
             {'destination': 'A', 'nexthop': 'X'}]
        self.assertTrue(hr.routes_equal(a, b))

    def test_not_equal_when_different(self):
        a = [{'destination': 'A', 'nexthop': 'X'}]
        b = [{'destination': 'A', 'nexthop': 'Y'}]
        self.assertFalse(hr.routes_equal(a, b))

    def test_empty_equal_to_empty(self):
        self.assertTrue(hr.routes_equal(None, []))


class TestComputeServiceRoutes(testtools.TestCase):

    def setUp(self):
        super().setUp()
        self.core = mock.Mock()
        self.context = mock.Mock()
        self.find_port_patch = mock.patch.object(
            lp, 'find_port', return_value=_our_port())
        self.find_port_mock = self.find_port_patch.start()
        self.addCleanup(self.find_port_patch.stop)

    def test_no_localport_returns_empty(self):
        self.find_port_mock.return_value = None
        routes, nexthop = hr.compute_service_routes(
            self.core, self.context, _subnet(), [_service()])
        self.assertEqual([], routes)
        self.assertIsNone(nexthop)

    def test_localport_in_different_subnet_returns_empty(self):
        # Localport's fixed_ip is on a different subnet than the one
        # we're computing for — can't use it as a nexthop.
        self.find_port_mock.return_value = _our_port(sid='other-subnet')
        routes, nexthop = hr.compute_service_routes(
            self.core, self.context, _subnet(), [_service()])
        self.assertEqual([], routes)
        self.assertIsNone(nexthop)

    def test_builds_route_for_each_service_with_vip(self):
        services = [_service(vip='169.254.169.5'),
                    _service(vip='169.254.169.10')]
        routes, nexthop = hr.compute_service_routes(
            self.core, self.context, _subnet(), services)
        self.assertEqual('10.0.0.7', nexthop)
        self.assertEqual(
            [{'destination': '169.254.169.5/32', 'nexthop': '10.0.0.7'},
             {'destination': '169.254.169.10/32', 'nexthop': '10.0.0.7'}],
            routes)

    def test_skips_service_without_local_ipv4(self):
        services = [{'id': 'svc', 'local_ipv4': None, 'enabled': True}]
        routes, _ = hr.compute_service_routes(
            self.core, self.context, _subnet(), services)
        self.assertEqual([], routes)

    def test_skips_disabled_service(self):
        services = [_service(enabled=False)]
        routes, _ = hr.compute_service_routes(
            self.core, self.context, _subnet(), services)
        self.assertEqual([], routes)

    def test_skips_service_whose_vip_overlaps_subnet_cidr(self):
        # Defense-in-depth: if a VIP slipped through (e.g. operator
        # added a binding before the subnet was created and the
        # subnet covers the VIP), don't publish a /32 that would
        # hijack the on-link route. Both services should be filtered;
        # the unrelated one (link-local VIP) still produces a route.
        subnet = _subnet(cidr='10.0.0.0/24')
        services = [_service(vip='10.0.0.5'),
                    _service(vip='169.254.169.5')]
        with mock.patch.object(hr, 'LOG') as log_mock:
            routes, _ = hr.compute_service_routes(
                self.core, self.context, subnet, services)
        self.assertEqual(
            [{'destination': '169.254.169.5/32', 'nexthop': '10.0.0.7'}],
            routes)
        self.assertTrue(log_mock.info.called)


class TestHostRoutesHandler(testtools.TestCase):

    def setUp(self):
        super().setUp()
        self.plugin = mock.Mock()
        self.plugin._core_plugin = mock.Mock()
        # Default: one enabled service bound to NET_ID.
        self.plugin.get_local_service_bindings.return_value = [
            {'id': 'b1', 'service_id': 'svc', 'network_id': NET_ID,
             'enabled': True}]
        self.plugin.get_local_service.return_value = _service()
        # No opt-out services by default — mock the list query.
        self.plugin.get_local_services.return_value = []
        self.find_port_patch = mock.patch.object(
            lp, 'find_port', return_value=_our_port())
        self.find_port_mock = self.find_port_patch.start()
        self.addCleanup(self.find_port_patch.stop)
        self.handler = hr.HostRoutesHandler(self.plugin)

    def _payload(self, states, context=None):
        # Hand-roll a payload-shaped object so we don't need the full
        # neutron_lib DBEventPayload constructor (which expects a
        # context-like object).
        return mock.Mock(states=states, context=context or mock.Mock())

    def test_before_create_noop_in_practice(self):
        # BEFORE_CREATE fires before the subnet has an id assigned,
        # and our nexthop lookup keys on subnet_id matching the
        # localport's fixed_ip — so the handler can't actually inject
        # on subnet create. The handler exists for defensive symmetry
        # with BEFORE_UPDATE; verify it's a clean no-op rather than
        # producing a half-formed route.
        subnet_data = {'network_id': NET_ID, 'host_routes': []}
        payload = self._payload(states=(subnet_data,))
        self.handler._on_before_create(
            'subnet', events.BEFORE_CREATE, None, payload)
        self.assertEqual([], subnet_data['host_routes'])

    def test_before_create_noop_when_no_services(self):
        self.plugin.get_local_service_bindings.return_value = []
        subnet_data = {'network_id': NET_ID, 'host_routes': []}
        payload = self._payload(states=(subnet_data,))
        self.handler._on_before_create(
            'subnet', events.BEFORE_CREATE, None, payload)
        self.assertEqual([], subnet_data['host_routes'])

    def test_before_create_noop_when_no_localport_yet(self):
        # New subnet on a network where bindings exist but the
        # localport hasn't materialized — handler can't compute a
        # nexthop and bails out cleanly.
        self.find_port_mock.return_value = None
        subnet_data = {'network_id': NET_ID, 'host_routes': []}
        payload = self._payload(states=(subnet_data,))
        self.handler._on_before_create(
            'subnet', events.BEFORE_CREATE, None, payload)
        self.assertEqual([], subnet_data['host_routes'])

    def test_before_update_noop_when_host_routes_not_in_patch(self):
        # Tenant PUT didn't include host_routes — existing routes are
        # preserved by IPAM, no action needed.
        orig = _subnet()
        patch = {'gateway_ip': '10.0.0.1'}
        payload = self._payload(states=(orig, patch))
        self.handler._on_before_update(
            'subnet', events.BEFORE_UPDATE, None, payload)
        self.assertNotIn('host_routes', patch)

    def test_before_update_re_injects_when_tenant_strips(self):
        # Tenant PUT with host_routes=[] would wipe ours — re-inject.
        orig = _subnet(host_routes=[
            {'destination': '169.254.169.5/32', 'nexthop': '10.0.0.7'}])
        patch = {'host_routes': []}
        payload = self._payload(states=(orig, patch))
        self.handler._on_before_update(
            'subnet', events.BEFORE_UPDATE, None, payload)
        self.assertEqual(
            [{'destination': '169.254.169.5/32', 'nexthop': '10.0.0.7'}],
            patch['host_routes'])

    def test_before_update_idempotent_on_already_merged(self):
        # Our own update_subnet from _refresh_subnet_routes will fire
        # BEFORE_UPDATE again. The merge must be a no-op the second
        # pass — that's what makes the reentrancy safe.
        orig = _subnet(host_routes=[
            {'destination': '169.254.169.5/32', 'nexthop': '10.0.0.7'}])
        patch = {'host_routes': [
            {'destination': '169.254.169.5/32', 'nexthop': '10.0.0.7'}]}
        payload = self._payload(states=(orig, patch))
        self.handler._on_before_update(
            'subnet', events.BEFORE_UPDATE, None, payload)
        self.assertEqual(
            [{'destination': '169.254.169.5/32', 'nexthop': '10.0.0.7'}],
            patch['host_routes'])

    def test_before_create_refuses_when_explicitly_bound(self):
        # Service is explicitly bound to NET_ID with enabled=True
        # (default fixture). A new subnet whose CIDR would cover the
        # service's VIP is refused — the operator chose this binding,
        # so silently dropping the route would be more surprising.
        self.plugin.get_local_service.return_value = _service(
            vip='10.0.0.5')
        subnet_data = {'network_id': NET_ID, 'cidr': '10.0.0.0/24',
                       'host_routes': []}
        payload = self._payload(states=(subnet_data,))
        self.assertRaises(
            hr.SubnetOverlapsServiceVIPError,
            self.handler._on_before_create,
            'subnet', events.BEFORE_CREATE, None, payload)

    def test_before_create_skips_when_only_opt_out(self):
        # No explicit binding; service is implicitly attached via
        # opt-out. The subnet op proceeds with an INFO log;
        # compute_service_routes will drop the conflicting /32 later.
        self.plugin.get_local_service_bindings.return_value = []
        self.plugin.get_local_services.return_value = [
            {'id': 'svc-oo', 'local_ipv4': '10.0.0.5',
             'attachment_policy': lsc.ATTACH_OPT_OUT,
             'enabled': True}]
        self.plugin.get_local_service.return_value = {
            'id': 'svc-oo', 'local_ipv4': '10.0.0.5',
            'attachment_policy': lsc.ATTACH_OPT_OUT,
            'enabled': True}
        subnet_data = {'network_id': NET_ID, 'cidr': '10.0.0.0/24',
                       'host_routes': []}
        payload = self._payload(states=(subnet_data,))
        # Should NOT raise.
        self.handler._on_before_create(
            'subnet', events.BEFORE_CREATE, None, payload)

    def test_before_update_skips_opt_out_overlap_on_host_routes_patch(self):
        # Opt-out service whose VIP sits inside the subnet's CIDR. The
        # overlap precheck lets the subnet op proceed (relying on the
        # per-route filter for safety), so the injection path itself
        # MUST still drop the /32 — otherwise a tenant
        # ``host_routes`` PUT would re-publish the hijacking route.
        self.plugin.get_local_service_bindings.return_value = []
        self.plugin.get_local_services.return_value = [
            {'id': 'svc-oo', 'local_ipv4': '10.0.0.5',
             'attachment_policy': lsc.ATTACH_OPT_OUT,
             'enabled': True}]
        self.plugin.get_local_service.return_value = {
            'id': 'svc-oo', 'local_ipv4': '10.0.0.5',
            'attachment_policy': lsc.ATTACH_OPT_OUT,
            'enabled': True}
        orig = _subnet(cidr='10.0.0.0/24', host_routes=[])
        # Tenant PUT clears host_routes; without the cidr-aware filter
        # we would re-inject 10.0.0.5/32 as an on-link hijack.
        patch = {'host_routes': []}
        payload = self._payload(states=(orig, patch))
        self.handler._on_before_update(
            'subnet', events.BEFORE_UPDATE, None, payload)
        # Either host_routes is left unset (no diff) or set to []. What
        # must NOT appear is a /32 for the overlapping VIP.
        injected = patch.get('host_routes', [])
        self.assertNotIn(
            {'destination': '10.0.0.5/32', 'nexthop': '10.0.0.7'},
            injected)
        self.assertEqual([], injected)

    def test_before_create_allows_non_overlapping_subnet(self):
        # Default service VIP is link-local 169.254.169.5, tenant
        # subnet is 10.0.0.0/24 — no overlap.
        subnet_data = {'network_id': NET_ID, 'cidr': '10.0.0.0/24',
                       'host_routes': []}
        payload = self._payload(states=(subnet_data,))
        # Should not raise.
        self.handler._on_before_create(
            'subnet', events.BEFORE_CREATE, None, payload)


class TestPluginRefreshSubnetRoutes(testtools.TestCase):
    """Plugin's ``_refresh_subnet_routes``: walks subnets, calls
    update_subnet only on diff, warns on DHCP-disabled, skips IPv6."""

    def setUp(self):
        super().setUp()
        self.cfg_fixture = self.useFixture(config_fixture.Config(cfg.CONF))
        try:
            self.cfg_fixture.register_opt(
                cfg.ListOpt('service_provider', default=[]),
                group='service_providers')
        except cfg.DuplicateOptError:
            pass
        # Stop the periodic reconciler from actually starting; we don't
        # want a stray greenthread and the test only exercises the
        # synchronous helpers.
        self.reconciler_patch = mock.patch.object(
            plugin_mod.LocalServicesPlugin, '_start_reconciler')
        self.reconciler_patch.start()
        self.addCleanup(self.reconciler_patch.stop)
        self.plugin = plugin_mod.LocalServicesPlugin()
        self.core = mock.Mock()
        self.plugin._core_plugin_ref = self.core
        self.plugin._mech_driver_ref = self.plugin._MECH_DRIVER_UNAVAILABLE
        # One enabled binding/service by default.
        self.plugin.get_local_service_bindings = mock.Mock(return_value=[
            {'id': 'b1', 'service_id': 'svc', 'network_id': NET_ID,
             'enabled': True}])
        self.plugin.get_local_service = mock.Mock(return_value=_service())
        # No opt-out services by default.
        self.plugin.get_local_services = mock.Mock(return_value=[])
        self.find_port_patch = mock.patch.object(
            lp, 'find_port', return_value=_our_port())
        self.find_port_mock = self.find_port_patch.start()
        self.addCleanup(self.find_port_patch.stop)

    def test_updates_subnet_when_routes_missing(self):
        ctx = mock.Mock()
        self.core.get_subnets.return_value = [_subnet(host_routes=[])]
        self.plugin._refresh_subnet_routes(ctx, NET_ID)
        self.assertEqual(1, self.core.update_subnet.call_count)
        body = self.core.update_subnet.call_args.args[2]
        self.assertEqual(
            [{'destination': '169.254.169.5/32', 'nexthop': '10.0.0.7'}],
            body['subnet']['host_routes'])

    def test_skips_update_when_routes_already_present(self):
        ctx = mock.Mock()
        self.core.get_subnets.return_value = [_subnet(host_routes=[
            {'destination': '169.254.169.5/32', 'nexthop': '10.0.0.7'}])]
        self.plugin._refresh_subnet_routes(ctx, NET_ID)
        self.core.update_subnet.assert_not_called()

    def test_drops_stale_route_when_no_services_bound(self):
        ctx = mock.Mock()
        self.plugin.get_local_service_bindings.return_value = []
        self.core.get_subnets.return_value = [_subnet(host_routes=[
            {'destination': '169.254.169.5/32', 'nexthop': '10.0.0.7'}])]
        self.plugin._refresh_subnet_routes(ctx, NET_ID)
        # No services bound → empty service_routes → stale route
        # (nexthop=10.0.0.7) is dropped.
        self.assertEqual(1, self.core.update_subnet.call_count)
        body = self.core.update_subnet.call_args.args[2]
        self.assertEqual([], body['subnet']['host_routes'])

    def test_warns_when_subnet_dhcp_disabled(self):
        ctx = mock.Mock()
        self.core.get_subnets.return_value = [
            _subnet(host_routes=[], enable_dhcp=False)]
        with mock.patch.object(plugin_mod.LOG, 'warning') as warn:
            self.plugin._refresh_subnet_routes(ctx, NET_ID)
        self.assertTrue(warn.called)
        # We still issued the update — the route is pointless without
        # DHCP, but documenting it in host_routes is harmless and lets
        # operators see the intent.
        self.assertEqual(1, self.core.update_subnet.call_count)

    def test_skips_ipv6_subnet(self):
        ctx = mock.Mock()
        self.core.get_subnets.return_value = [
            _subnet(ip_version=6, cidr='fd00::/64')]
        self.plugin._refresh_subnet_routes(ctx, NET_ID)
        self.core.update_subnet.assert_not_called()

    def test_swallows_update_subnet_failure(self):
        ctx = mock.Mock()
        self.core.get_subnets.return_value = [_subnet(host_routes=[])]
        self.core.update_subnet.side_effect = RuntimeError('boom')
        # Should not raise — refresh failures are non-fatal (the
        # binding row itself is fine; the agent reconciler will
        # recover).
        self.plugin._refresh_subnet_routes(ctx, NET_ID)
        self.assertEqual(1, self.core.update_subnet.call_count)


def _opt_in_svc(sid='svc-in', vip='169.254.1.1'):
    return {'id': sid, 'local_ipv4': vip, 'enabled': True,
            'attachment_policy': lsc.ATTACH_OPT_IN}


def _opt_out_svc(sid='svc-out', vip='169.254.2.2', enabled=True):
    return {'id': sid, 'local_ipv4': vip, 'enabled': enabled,
            'attachment_policy': lsc.ATTACH_OPT_OUT}


class TestEffectiveAttachment(testtools.TestCase):
    """``_enabled_services_for_network`` — the new opt-in/opt-out
    decision function.

    Covers the full effective-attachment table from
    ``docs/architecture/overview.md``: opt-in needs an enabled binding,
    opt-out is implicit unless an ``enabled=False`` marker excludes it,
    and ``service.enabled=False`` always wins."""

    def setUp(self):
        super().setUp()
        self.plugin = mock.Mock()
        # Default: no bindings, no services. Tests override per case.
        self.plugin.get_local_service_bindings.return_value = []
        self.plugin.get_local_services.return_value = []

        def _fake_get(_ctx, sid):
            for svc in (self._service_table or {}).values():
                if svc['id'] == sid:
                    return svc
            raise KeyError(sid)

        self._service_table = {}
        self.plugin.get_local_service.side_effect = _fake_get

    def _set_services(self, *services):
        """Populate the service catalog. Opt-out services are also
        returned by ``get_local_services(filters={'attachment_policy':
        ['opt-out'], 'enabled': [True]})`` to mirror server behavior."""
        self._service_table = {s['id']: s for s in services}
        self.plugin.get_local_services.return_value = [
            s for s in services
            if s.get('attachment_policy') == lsc.ATTACH_OPT_OUT
            and s.get('enabled', True)]

    def _set_bindings(self, *bindings):
        self.plugin.get_local_service_bindings.return_value = list(bindings)

    def _binding(self, service_id, enabled=True):
        return {'id': 'b-' + service_id, 'service_id': service_id,
                'network_id': NET_ID, 'enabled': enabled}

    # opt-in cases ---------------------------------------------------
    def test_opt_in_no_binding_not_effective(self):
        self._set_services(_opt_in_svc())
        result = hr._enabled_services_for_network(
            self.plugin, mock.Mock(), NET_ID)
        self.assertEqual([], result)

    def test_opt_in_with_enabled_binding_is_effective(self):
        svc = _opt_in_svc()
        self._set_services(svc)
        self._set_bindings(self._binding(svc['id'], enabled=True))
        result = hr._enabled_services_for_network(
            self.plugin, mock.Mock(), NET_ID)
        self.assertEqual([svc['id']], [s['id'] for s in result])

    def test_opt_in_with_disabled_binding_not_effective(self):
        svc = _opt_in_svc()
        self._set_services(svc)
        self._set_bindings(self._binding(svc['id'], enabled=False))
        result = hr._enabled_services_for_network(
            self.plugin, mock.Mock(), NET_ID)
        self.assertEqual([], result)

    # opt-out cases --------------------------------------------------
    def test_opt_out_no_binding_is_effective(self):
        # The headline case: opt-out service auto-attaches.
        svc = _opt_out_svc()
        self._set_services(svc)
        result = hr._enabled_services_for_network(
            self.plugin, mock.Mock(), NET_ID)
        self.assertEqual([svc['id']], [s['id'] for s in result])

    def test_opt_out_with_disabled_binding_is_excluded(self):
        # The tenant has explicitly opted out by creating an
        # ``enabled=False`` binding.
        svc = _opt_out_svc()
        self._set_services(svc)
        self._set_bindings(self._binding(svc['id'], enabled=False))
        result = hr._enabled_services_for_network(
            self.plugin, mock.Mock(), NET_ID)
        self.assertEqual([], result)

    def test_opt_out_with_enabled_binding_is_effective_no_dup(self):
        # An ``enabled=True`` binding for an opt-out service is a
        # redundant but legal write — it shouldn't double-count.
        svc = _opt_out_svc()
        self._set_services(svc)
        self._set_bindings(self._binding(svc['id'], enabled=True))
        result = hr._enabled_services_for_network(
            self.plugin, mock.Mock(), NET_ID)
        self.assertEqual([svc['id']], [s['id'] for s in result])

    def test_disabled_opt_out_service_not_effective(self):
        # ``service.enabled=False`` overrides the implicit attachment.
        # The mock filters it out at the catalog query, just like the DB.
        svc = _opt_out_svc(enabled=False)
        self._set_services(svc)
        result = hr._enabled_services_for_network(
            self.plugin, mock.Mock(), NET_ID)
        self.assertEqual([], result)

    # mixed --------------------------------------------------------
    def test_mixed_opt_in_and_opt_out(self):
        # An opt-in service with a binding alongside an unrelated
        # opt-out service: both are effective.
        in_svc = _opt_in_svc(sid='svc-in', vip='169.254.10.1')
        out_svc = _opt_out_svc(sid='svc-out', vip='169.254.10.2')
        self._set_services(in_svc, out_svc)
        self._set_bindings(self._binding(in_svc['id'], enabled=True))
        result = hr._enabled_services_for_network(
            self.plugin, mock.Mock(), NET_ID)
        self.assertEqual({in_svc['id'], out_svc['id']},
                         {s['id'] for s in result})

    def test_disabled_binding_for_other_service_doesnt_exclude_opt_out(self):
        # Network has an ``enabled=False`` row for service A (some opt-in
        # in placeholder state) but service B is opt-out — B is still
        # effective.
        in_svc = _opt_in_svc(sid='svc-a')
        out_svc = _opt_out_svc(sid='svc-b')
        self._set_services(in_svc, out_svc)
        self._set_bindings(self._binding(in_svc['id'], enabled=False))
        result = hr._enabled_services_for_network(
            self.plugin, mock.Mock(), NET_ID)
        self.assertEqual({out_svc['id']}, {s['id'] for s in result})


class TestPluginReconcileNetwork(testtools.TestCase):
    """``_reconcile_network`` — the canonical state-driver invoked by
    binding hooks AND the periodic loop."""

    def setUp(self):
        super().setUp()
        self.cfg_fixture = self.useFixture(config_fixture.Config(cfg.CONF))
        try:
            self.cfg_fixture.register_opt(
                cfg.ListOpt('service_provider', default=[]),
                group='service_providers')
        except cfg.DuplicateOptError:
            pass
        # Block the periodic reconciler from starting under test.
        self.reconciler_patch = mock.patch.object(
            plugin_mod.LocalServicesPlugin, '_start_reconciler')
        self.reconciler_patch.start()
        self.addCleanup(self.reconciler_patch.stop)
        self.plugin = plugin_mod.LocalServicesPlugin()
        self.core = mock.Mock()
        self.plugin._core_plugin_ref = self.core
        self.plugin._mech_driver_ref = self.plugin._MECH_DRIVER_UNAVAILABLE
        # Default no bindings, no services, one IPv4 subnet on the net.
        self.plugin.get_local_service_bindings = mock.Mock(return_value=[])
        self.plugin.get_local_service = mock.Mock(return_value=None)
        self.plugin.get_local_services = mock.Mock(return_value=[])
        self.core.get_subnets.return_value = [_subnet(host_routes=[])]
        self.core.get_network.return_value = {
            'id': NET_ID, 'project_id': 'tenant-1'}
        self.find_port_patch = mock.patch.object(
            lp, 'find_port', return_value=_our_port())
        self.find_port_mock = self.find_port_patch.start()
        self.addCleanup(self.find_port_patch.stop)
        self.ensure_port_patch = mock.patch.object(
            lp, 'ensure_localport', return_value=_our_port())
        self.ensure_port_patch.start()
        self.addCleanup(self.ensure_port_patch.stop)
        self.maybe_remove_patch = mock.patch.object(
            lp, 'maybe_remove_localport', return_value=False)
        self.maybe_remove_mock = self.maybe_remove_patch.start()
        self.addCleanup(self.maybe_remove_patch.stop)

    def test_opt_out_service_creates_localport_on_fresh_network(self):
        # No bindings, but an opt-out service exists → reconcile ensures
        # the localport and injects routes.
        out_svc = _opt_out_svc(sid='svc-out', vip='169.254.99.1')
        self.plugin.get_local_services.return_value = [out_svc]
        self.plugin._reconcile_network(mock.Mock(), NET_ID)
        # ensure_localport was called (via _ensure_localport).
        from neutron_local_services.ovn import localport as lpmod
        lpmod.ensure_localport.assert_called_once()
        # update_subnet was called to inject the VIP route.
        self.core.update_subnet.assert_called_once()
        body = self.core.update_subnet.call_args.args[2]
        self.assertEqual(
            [{'destination': '169.254.99.1/32', 'nexthop': '10.0.0.7'}],
            body['subnet']['host_routes'])

    def test_opt_out_marker_removes_localport_when_alone(self):
        # Same opt-out service, but this network has the
        # ``enabled=False`` opt-out marker → reconcile drops the port.
        out_svc = _opt_out_svc(sid='svc-out', vip='169.254.99.1')
        self.plugin.get_local_services.return_value = [out_svc]
        self.plugin.get_local_service_bindings.return_value = [
            {'id': 'b1', 'service_id': out_svc['id'],
             'network_id': NET_ID, 'enabled': False}]
        self.plugin._reconcile_network(mock.Mock(), NET_ID)
        self.maybe_remove_mock.assert_called_once()
        # has_remaining_bindings is False because the only attachment
        # was excluded by the opt-out marker.
        kwargs = self.maybe_remove_mock.call_args.kwargs
        self.assertFalse(kwargs['has_remaining_bindings'])

    def test_opt_out_marker_keeps_localport_when_other_service_attached(self):
        # Opt-out service excluded on this network, but an opt-in
        # service is bound → localport stays.
        out_svc = _opt_out_svc(sid='svc-out', vip='169.254.99.1')
        in_svc = _opt_in_svc(sid='svc-in', vip='169.254.10.1')
        self.plugin.get_local_services.return_value = [out_svc]
        self.plugin.get_local_service.return_value = in_svc
        self.plugin.get_local_service_bindings.return_value = [
            {'id': 'b-out', 'service_id': out_svc['id'],
             'network_id': NET_ID, 'enabled': False},
            {'id': 'b-in', 'service_id': in_svc['id'],
             'network_id': NET_ID, 'enabled': True},
        ]
        self.plugin._reconcile_network(mock.Mock(), NET_ID)
        # _ensure_localport runs; _maybe_remove does NOT.
        from neutron_local_services.ovn import localport as lpmod
        lpmod.ensure_localport.assert_called_once()
        self.maybe_remove_mock.assert_not_called()


class TestPluginReconcileLoop(testtools.TestCase):
    """``_reconcile_loop`` — the periodic timer body. Walks every
    network, isolates failures, swallows the listing error."""

    def setUp(self):
        super().setUp()
        self.cfg_fixture = self.useFixture(config_fixture.Config(cfg.CONF))
        try:
            self.cfg_fixture.register_opt(
                cfg.ListOpt('service_provider', default=[]),
                group='service_providers')
        except cfg.DuplicateOptError:
            pass
        self.reconciler_patch = mock.patch.object(
            plugin_mod.LocalServicesPlugin, '_start_reconciler')
        self.reconciler_patch.start()
        self.addCleanup(self.reconciler_patch.stop)
        # n_context.get_admin_context() reaches into oslo.policy which
        # tries to load policy files — not available in unit tests.
        # Stub it out with a lightweight mock context.
        self.ctx_patch = mock.patch.object(
            plugin_mod.n_context, 'get_admin_context',
            return_value=mock.Mock())
        self.ctx_patch.start()
        self.addCleanup(self.ctx_patch.stop)
        self.plugin = plugin_mod.LocalServicesPlugin()
        self.core = mock.Mock()
        self.plugin._core_plugin_ref = self.core
        # Patch the per-network reconcile so we just count calls.
        self.reconcile_net_patch = mock.patch.object(
            self.plugin, '_reconcile_network')
        self.reconcile_net_mock = self.reconcile_net_patch.start()
        self.addCleanup(self.reconcile_net_patch.stop)

    def test_walks_every_network(self):
        self.core.get_networks.return_value = [
            {'id': 'net-a'}, {'id': 'net-b'}, {'id': 'net-c'}]
        self.plugin._reconcile_loop()
        ids = [c.args[1] for c in self.reconcile_net_mock.call_args_list]
        self.assertEqual(['net-a', 'net-b', 'net-c'], ids)

    def test_one_failing_network_does_not_block_others(self):
        self.core.get_networks.return_value = [
            {'id': 'net-a'}, {'id': 'net-b'}, {'id': 'net-c'}]
        # net-b raises; net-a and net-c should still see reconcile calls.
        def _maybe_raise(_ctx, net_id):
            if net_id == 'net-b':
                raise RuntimeError('boom')
        self.reconcile_net_mock.side_effect = _maybe_raise
        self.plugin._reconcile_loop()
        ids = [c.args[1] for c in self.reconcile_net_mock.call_args_list]
        self.assertEqual(['net-a', 'net-b', 'net-c'], ids)

    def test_get_networks_failure_skips_pass_cleanly(self):
        self.core.get_networks.side_effect = RuntimeError('db down')
        # Should not raise — periodic timer resumes on the next tick.
        self.plugin._reconcile_loop()
        self.reconcile_net_mock.assert_not_called()
