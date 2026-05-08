"""Smoke tests for the plugin module — no DB, just behavior that can
be exercised without spinning up a Neutron environment."""

from unittest import mock

import testtools
from oslo_config import cfg
from oslo_config import fixture as config_fixture

from neutron_local_services import conf as ls_conf
from neutron_local_services import constants as lsc
from neutron_local_services.plugin import plugin


class TestOctaviaGuard(testtools.TestCase):

    def setUp(self):
        super().setUp()
        self.cfg_fixture = self.useFixture(config_fixture.Config(cfg.CONF))
        # The service_providers group is registered by neutron itself
        # when it's importable; only register if absent so this test
        # runs with or without the neutron tree on sys.path.
        try:
            self.cfg_fixture.register_opt(
                cfg.ListOpt('service_provider', default=[]),
                group='service_providers')
        except cfg.DuplicateOptError:
            pass
        # The plugin starts a periodic reconciler in __init__; tests
        # don't want the greenthread, so patch it out for the whole
        # test class.
        self.reconciler_patch = mock.patch.object(
            plugin.LocalServicesPlugin, '_start_reconciler')
        self.reconciler_patch.start()
        self.addCleanup(self.reconciler_patch.stop)

    def test_no_octavia_initialize_succeeds(self):
        p = plugin.LocalServicesPlugin()
        p.initialize()  # should not raise
        self.assertTrue(p._initialize_called)

    def test_octavia_provider_present_raises(self):
        # The guard fires during __init__ (neutron-manager doesn't
        # drive initialize() for service plugins, so it has to run at
        # construction time).
        self.cfg_fixture.config(
            group='service_providers',
            service_provider=[
                'LOADBALANCERV2:ovn:ovn_octavia_provider.driver.OvnDriver:default'
            ])
        self.assertRaises(plugin.OctaviaConflictError,
                          plugin.LocalServicesPlugin)

    def test_unrelated_provider_initialize_succeeds(self):
        self.cfg_fixture.config(
            group='service_providers',
            service_provider=[
                'LOADBALANCERV2:amphora:octavia.amphora.driver:default'
            ])
        p = plugin.LocalServicesPlugin()
        p.initialize()


class TestVipDenylist(testtools.TestCase):

    def setUp(self):
        super().setUp()
        self.cfg_fixture = self.useFixture(config_fixture.Config(cfg.CONF))
        ls_conf.register_opts(cfg.CONF)

    def test_metadata_ip_rejected(self):
        from neutron_local_services.db import local_services_db as db
        from neutron_lib import exceptions as n_exc
        self.assertRaises(
            n_exc.InvalidInput,
            db.LocalServicesDbMixin._validate_vip,
            '169.254.169.254')

    def test_normal_ip_accepted(self):
        from neutron_local_services.db import local_services_db as db
        # Default allowlist is link-local; 169.254.169.5 is in it.
        db.LocalServicesDbMixin._validate_vip('169.254.169.5')

    def test_none_accepted(self):
        from neutron_local_services.db import local_services_db as db
        db.LocalServicesDbMixin._validate_vip(None)

    def test_outside_allowed_cidr_rejected(self):
        from neutron_local_services.db import local_services_db as db
        from neutron_lib import exceptions as n_exc
        # 10.0.0.5 is unicast and not in the denylist, but the default
        # allowlist is link-local only.
        self.assertRaises(
            n_exc.InvalidInput,
            db.LocalServicesDbMixin._validate_vip,
            '10.0.0.5')

    def test_loopback_rejected(self):
        from neutron_local_services.db import local_services_db as db
        from neutron_lib import exceptions as n_exc
        self.assertRaises(
            n_exc.InvalidInput,
            db.LocalServicesDbMixin._validate_vip,
            '127.0.0.1')

    def test_unspecified_rejected(self):
        from neutron_local_services.db import local_services_db as db
        from neutron_lib import exceptions as n_exc
        self.assertRaises(
            n_exc.InvalidInput,
            db.LocalServicesDbMixin._validate_vip,
            '0.0.0.0')

    def test_multicast_rejected(self):
        from neutron_local_services.db import local_services_db as db
        from neutron_lib import exceptions as n_exc
        self.assertRaises(
            n_exc.InvalidInput,
            db.LocalServicesDbMixin._validate_vip,
            '224.0.0.1')

    def test_operator_can_extend_allowlist(self):
        from neutron_local_services.db import local_services_db as db
        # Operator opts in to a private CIDR for service VIPs.
        self.cfg_fixture.config(group=ls_conf.GROUP,
                                allowed_vip_cidrs=['10.0.0.0/8',
                                                   '169.254.0.0/16',
                                                   'fe80::/10'])
        db.LocalServicesDbMixin._validate_vip('10.5.6.7')

    def test_ipv6_link_local_accepted(self):
        from neutron_local_services.db import local_services_db as db
        db.LocalServicesDbMixin._validate_vip('fe80::1')

    def test_ipv6_global_unicast_rejected_by_default(self):
        from neutron_local_services.db import local_services_db as db
        from neutron_lib import exceptions as n_exc
        self.assertRaises(
            n_exc.InvalidInput,
            db.LocalServicesDbMixin._validate_vip,
            '2001:db8::1')


class TestVipSubnetOverlap(testtools.TestCase):
    """Plugin-level VIP/subnet overlap rejection on binding ops."""

    NET_ID = '11111111-1111-1111-1111-111111111111'

    def setUp(self):
        super().setUp()
        self.cfg_fixture = self.useFixture(config_fixture.Config(cfg.CONF))
        try:
            self.cfg_fixture.register_opt(
                cfg.ListOpt('service_provider', default=[]),
                group='service_providers')
        except cfg.DuplicateOptError:
            pass
        ls_conf.register_opts(cfg.CONF)
        # Operator allows private CIDRs as VIPs for this test class so
        # we can exercise overlap with realistic tenant subnets without
        # also testing the link-local default.
        self.cfg_fixture.config(
            group=ls_conf.GROUP,
            allowed_vip_cidrs=['10.0.0.0/8', '169.254.0.0/16', 'fe80::/10'])
        self.reconciler_patch = mock.patch.object(
            plugin.LocalServicesPlugin, '_start_reconciler')
        self.reconciler_patch.start()
        self.addCleanup(self.reconciler_patch.stop)
        self.host_routes_patch = mock.patch.object(
            plugin.hr.HostRoutesHandler, 'register')
        self.host_routes_patch.start()
        self.addCleanup(self.host_routes_patch.stop)
        self.plugin = plugin.LocalServicesPlugin()
        # Stub the DB read paths used by _refuse_vip_subnet_overlap so
        # we don't need a live DB session.
        self.plugin.get_local_service = mock.Mock()
        # And patch the core_plugin lookup.
        self.core_plugin = mock.Mock()
        self.plugin._core_plugin_ref = self.core_plugin

    def _binding_body(self, service_id='svc', network_id=None):
        return {lsc.RESOURCE_LOCAL_SERVICE_BINDING: {
            'service_id': service_id,
            'network_id': network_id or self.NET_ID,
        }}

    def test_overlap_raises(self):
        self.plugin.get_local_service.return_value = {
            'id': 'svc', 'local_ipv4': '10.0.0.5', 'local_ipv6': None}
        self.core_plugin.get_subnets.return_value = [
            {'id': 'sub1', 'cidr': '10.0.0.0/24'}]
        self.assertRaises(
            plugin.VIPOverlapsSubnetError,
            self.plugin.create_local_service_binding,
            mock.Mock(), self._binding_body())

    def test_no_overlap_ok(self):
        self.plugin.get_local_service.return_value = {
            'id': 'svc', 'local_ipv4': '169.254.169.5', 'local_ipv6': None}
        self.core_plugin.get_subnets.return_value = [
            {'id': 'sub1', 'cidr': '10.0.0.0/24'}]
        # Need to also patch the super().create_local_service_binding
        # call; easiest is to bypass it.
        with mock.patch.object(
                plugin.local_services_db.LocalServicesDbMixin,
                'create_local_service_binding',
                return_value={'id': 'b1', 'network_id': self.NET_ID,
                              'service_id': 'svc'}):
            with mock.patch.object(self.plugin, '_reconcile_network'):
                # Should not raise.
                self.plugin.create_local_service_binding(
                    mock.Mock(), self._binding_body())

    def test_ipv6_overlap_raises(self):
        self.plugin.get_local_service.return_value = {
            'id': 'svc', 'local_ipv4': None, 'local_ipv6': 'fe80::1'}
        self.core_plugin.get_subnets.return_value = [
            {'id': 'sub1', 'cidr': 'fe80::/64'}]
        self.assertRaises(
            plugin.VIPOverlapsSubnetError,
            self.plugin.create_local_service_binding,
            mock.Mock(), self._binding_body())

    def test_version_mismatch_does_not_overlap(self):
        # Service has only IPv4 VIP; subnet is IPv6 — no overlap by
        # construction.
        self.plugin.get_local_service.return_value = {
            'id': 'svc', 'local_ipv4': '10.0.0.5', 'local_ipv6': None}
        self.core_plugin.get_subnets.return_value = [
            {'id': 'sub1', 'cidr': '2001:db8::/64'}]
        with mock.patch.object(
                plugin.local_services_db.LocalServicesDbMixin,
                'create_local_service_binding',
                return_value={'id': 'b1', 'network_id': self.NET_ID,
                              'service_id': 'svc'}):
            with mock.patch.object(self.plugin, '_reconcile_network'):
                self.plugin.create_local_service_binding(
                    mock.Mock(), self._binding_body())

    def test_update_enabled_true_re_checks_overlap(self):
        # Existing disabled binding; flip enabled=True after a tenant
        # subnet now covers the VIP.
        self.plugin.get_local_service.return_value = {
            'id': 'svc', 'local_ipv4': '10.0.0.5', 'local_ipv6': None}
        self.plugin.get_local_service_binding = mock.Mock(return_value={
            'id': 'b1', 'service_id': 'svc', 'network_id': self.NET_ID})
        self.core_plugin.get_subnets.return_value = [
            {'id': 'sub1', 'cidr': '10.0.0.0/24'}]
        body = {lsc.RESOURCE_LOCAL_SERVICE_BINDING: {'enabled': True}}
        self.assertRaises(
            plugin.VIPOverlapsSubnetError,
            self.plugin.update_local_service_binding,
            mock.Mock(), 'b1', body)

    def test_subnet_get_failure_fails_closed(self):
        from neutron_lib import exceptions as n_exc
        self.plugin.get_local_service.return_value = {
            'id': 'svc', 'local_ipv4': '169.254.169.5', 'local_ipv6': None}
        self.core_plugin.get_subnets.side_effect = RuntimeError('db down')
        self.assertRaises(
            n_exc.ServiceUnavailable,
            self.plugin.create_local_service_binding,
            mock.Mock(), self._binding_body())


class TestExtensionAttrMap(testtools.TestCase):

    def test_collections_present(self):
        from neutron_local_services.api import local_services as api
        self.assertIn(lsc.COLLECTION_LOCAL_SERVICE,
                      api.RESOURCE_ATTRIBUTE_MAP)
        self.assertIn(lsc.COLLECTION_LOCAL_SERVICE_BACKEND,
                      api.RESOURCE_ATTRIBUTE_MAP)
        self.assertIn(lsc.COLLECTION_LOCAL_SERVICE_BINDING,
                      api.RESOURCE_ATTRIBUTE_MAP)

    def test_required_fields(self):
        from neutron_local_services.api import local_services as api
        svc_attrs = api.RESOURCE_ATTRIBUTE_MAP[lsc.COLLECTION_LOCAL_SERVICE]
        self.assertEqual(False, svc_attrs['port']['allow_put'])
        self.assertEqual(False, svc_attrs['protocol']['allow_put'])
