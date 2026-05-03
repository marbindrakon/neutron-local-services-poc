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
        self.plugin = plugin_mod.LocalServicesPlugin()
        self.core = mock.Mock()
        self.plugin._core_plugin_ref = self.core
        self.plugin._mech_driver_ref = self.plugin._MECH_DRIVER_UNAVAILABLE
        # One enabled binding/service by default.
        self.plugin.get_local_service_bindings = mock.Mock(return_value=[
            {'id': 'b1', 'service_id': 'svc', 'network_id': NET_ID,
             'enabled': True}])
        self.plugin.get_local_service = mock.Mock(return_value=_service())
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
