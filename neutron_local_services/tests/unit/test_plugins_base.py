"""Unit tests for the exposure-plugin abstraction.

The registry is a tiny module — mostly we want to confirm:
* register() rejects unknown / empty names so the API attr map and
  the plugin set can't drift apart silently
* get()/all_plugins() round-trip what register() put in
* reset_for_tests() actually clears the registry between tests
"""

import testtools

from neutron_local_services import constants as lsc
from neutron_local_services.agent.plugins import base


class _GoodPlugin(base.ExposurePlugin):
    name = lsc.EXPOSURE_NAT

    def apply_config(self, netns_name, services):
        pass

    def teardown(self, netns_name):
        pass


class _OtherGoodPlugin(base.ExposurePlugin):
    name = lsc.EXPOSURE_PROXY

    def apply_config(self, netns_name, services):
        pass

    def teardown(self, netns_name):
        pass


class _BadNamePlugin(base.ExposurePlugin):
    name = 'haproxy-but-we-do-not-have-this-yet'

    def apply_config(self, netns_name, services):
        pass

    def teardown(self, netns_name):
        pass


class _EmptyNamePlugin(base.ExposurePlugin):
    name = ''

    def apply_config(self, netns_name, services):
        pass

    def teardown(self, netns_name):
        pass


class TestPluginRegistry(testtools.TestCase):

    def setUp(self):
        super().setUp()
        # Real plugins (lvs.py / envoy.py) register at import; unit
        # tests get a clean slate so we're only testing what we
        # explicitly add. The cleanup re-imports each module so
        # later test files (which expect the real registry) see them.
        base.reset_for_tests()
        self.addCleanup(self._restore_real_plugins)

    def _restore_real_plugins(self):
        import importlib
        base.reset_for_tests()
        from neutron_local_services.agent.plugins import nat
        from neutron_local_services.agent.plugins import proxy
        importlib.reload(nat)
        importlib.reload(proxy)

    def test_register_returns_class_and_stores_instance(self):
        # @register-as-decorator pattern needs the original class back.
        cls = base.register(_GoodPlugin)
        self.assertIs(cls, _GoodPlugin)
        # And the registry has an instance, not the class.
        instance = base.get(lsc.EXPOSURE_NAT)
        self.assertIsInstance(instance, _GoodPlugin)

    def test_register_rejects_empty_name(self):
        # Catches a class that forgot to set ``name``.
        self.assertRaises(ValueError, base.register, _EmptyNamePlugin)

    def test_register_rejects_unknown_name(self):
        # Keeps the API attr-map (constants.EXPOSURE_PLUGINS) and the
        # plugin registry in sync — adding a new plugin requires
        # bumping the constant first.
        self.assertRaises(ValueError, base.register, _BadNamePlugin)

    def test_get_returns_none_for_unregistered(self):
        # The agent uses ``get(name) or skip`` to log-and-move-on for
        # services that point at a plugin we haven't built yet.
        self.assertIsNone(base.get(lsc.EXPOSURE_PROXY))

    def test_all_plugins_returns_every_registered(self):
        base.register(_GoodPlugin)
        base.register(_OtherGoodPlugin)
        names = sorted(p.name for p in base.all_plugins())
        self.assertEqual(sorted([lsc.EXPOSURE_NAT, lsc.EXPOSURE_PROXY]),
                         names)

    def test_reset_for_tests_clears_registry(self):
        base.register(_GoodPlugin)
        base.reset_for_tests()
        self.assertIsNone(base.get(lsc.EXPOSURE_NAT))
        self.assertEqual([], base.all_plugins())
