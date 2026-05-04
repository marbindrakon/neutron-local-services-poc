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
        # No exception
        db.LocalServicesDbMixin._validate_vip('169.254.169.5')

    def test_none_accepted(self):
        from neutron_local_services.db import local_services_db as db
        db.LocalServicesDbMixin._validate_vip(None)


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
