"""LocalServicesPlugin — the Neutron service plugin entry point.

Wires together:

* DB CRUD (via `LocalServicesDbMixin`)
* Octavia-conflict guard at `initialize()`
* localport piggyback on binding create/delete (via
  `neutron_local_services.ovn.localport`).
* host_routes injection on binding create/delete and subnet
  create/update (via `neutron_local_services.host_routes`).
"""

from neutron_lib import exceptions as n_exc
from neutron_lib.plugins import directory
from oslo_config import cfg
from oslo_log import log as logging

from neutron_local_services import conf as ls_conf
from neutron_local_services import constants as lsc
from neutron_local_services import host_routes as hr
from neutron_local_services.db import local_services_db
from neutron_local_services.extensions import local_services as ls_ext
from neutron_local_services.ovn import localport as lp


LOG = logging.getLogger(__name__)


class OctaviaConflictError(n_exc.NeutronException):
    message = (
        'neutron-local-services PoC cannot run alongside '
        'ovn-octavia-provider due to device_owner conflicts on '
        'LB-HM ports. See docs/limitations.md.')


class LocalServicesPlugin(local_services_db.LocalServicesDbMixin,
                          ls_ext.LocalServicesPluginBase):
    """Service plugin for the local-services API."""

    supported_extension_aliases = [lsc.ALIAS]

    __native_pagination_support = True
    __native_sorting_support = True

    # Sentinel: "we tried to look up the mech driver and didn't find one".
    # Distinct from None so we don't keep retrying on every binding op.
    _MECH_DRIVER_UNAVAILABLE = object()

    def __init__(self):
        super().__init__()
        ls_conf.register_opts()
        self._initialize_called = False
        self._core_plugin_ref = None
        self._mech_driver_ref = None
        # Neutron's manager._create_and_add_service_plugin does NOT
        # call initialize() on service plugins — only the core plugin's
        # initialize is invoked. Run the conflict guard here in __init__
        # so it actually fires at API-server startup. (The test suite
        # still calls initialize() explicitly to match the pre-fix
        # behavior; that path is preserved as a no-op-after-the-fact.)
        if self._octavia_provider_loaded():
            raise OctaviaConflictError()
        # Subscribe to subnet BEFORE_CREATE / BEFORE_UPDATE so we
        # re-inject our routes if a tenant subnet update would strip
        # them. Constructed once per plugin instance.
        self._host_routes_handler = hr.HostRoutesHandler(self)
        self._host_routes_handler.register()

    def initialize(self):
        """Compatibility hook for the unit-test path.

        Real production startup runs the conflict guard inside
        ``__init__`` because neutron-manager doesn't drive
        ``initialize()`` for service plugins.
        """
        self._initialize_called = True
        if self._octavia_provider_loaded():
            raise OctaviaConflictError()

    @staticmethod
    def _octavia_provider_loaded():
        """Detect ovn-octavia-provider in service_providers config.

        CURSED: the matching is by substring on the service_providers
        list. Octavia registers itself there as
        `LOADBALANCERV2:<name>:<class>` and the OVN provider class path
        contains `ovn_octavia_provider`. This is the cheapest reliable
        signal we found; a more principled check would walk the loaded
        provider drivers, but that's not worth doing for the PoC.
        See docs/architecture/overview.md.
        """
        try:
            providers = cfg.CONF.service_providers.service_provider
        except cfg.NoSuchOptError:
            return False
        return any('ovn_octavia_provider' in p for p in providers or [])

    # ----- core_plugin / mech_driver lazy accessors -----

    @property
    def _core_plugin(self):
        if self._core_plugin_ref is None:
            self._core_plugin_ref = directory.get_plugin()
        return self._core_plugin_ref

    @property
    def _mech_driver(self):
        """OVN mech driver reference, or None if unavailable.

        Used only for the post-create LSP-type sanity check. Returning
        None makes that check a no-op, which is the right behavior in
        unit tests (and in deployments where the mech-driver name shifts
        — let the real check fail loudly there rather than blocking
        port creation entirely).
        """
        if self._mech_driver_ref is None:
            try:
                mm = self._core_plugin.mechanism_manager
                self._mech_driver_ref = mm.mech_drivers['ovn'].obj
            except (AttributeError, KeyError, TypeError):
                self._mech_driver_ref = self._MECH_DRIVER_UNAVAILABLE
        if self._mech_driver_ref is self._MECH_DRIVER_UNAVAILABLE:
            return None
        return self._mech_driver_ref

    # ----- localport lifecycle -----

    def _ensure_localport(self, context, network_id):
        """Create our localport on the network if absent; verify LSP type.

        Idempotent — safe to call on any binding-create. If verification
        fails (the LB-HM piggyback broke upstream), the just-created
        port is rolled back before the error propagates.
        """
        network = self._core_plugin.get_network(context, network_id)
        port = lp.ensure_localport(self._core_plugin, context, network)
        try:
            lp.verify_lsp_type(self._mech_driver, port['id'], network_id)
        except lp.LocalportLSPVerifyError:
            try:
                self._core_plugin.delete_port(context, port['id'])
            except Exception:
                LOG.exception('Failed to roll back localport %s after '
                              'LSP type verification failure', port['id'])
            raise
        return port

    def _maybe_remove_localport(self, context, network_id):
        """Drop our localport if no bindings remain on the network."""
        remaining = self.get_local_service_bindings(
            context, filters={'network_id': [network_id]})
        lp.maybe_remove_localport(
            self._core_plugin, context, network_id,
            has_remaining_bindings=bool(remaining))

    # ----- host_routes refresh -----

    def _refresh_subnet_routes(self, context, network_id):
        """Recompute host_routes for every subnet on the network and
        push updates only where the merged shape differs.

        Called from binding create/delete hooks. The localport must
        already be in place (binding-create order) or about to be
        removed (binding-delete order). The subnet ``BEFORE_UPDATE``
        handler will re-merge during our own ``update_subnet`` call;
        the merge is idempotent so the second pass is a no-op.
        """
        services = hr._enabled_services_for_network(
            self, context, network_id)
        subnets = self._core_plugin.get_subnets(
            context, filters={'network_id': [network_id]})
        for subnet in subnets:
            if subnet.get('ip_version') != 4:
                # PoC is IPv4-only. IPv6 RA Route Information is post-PoC.
                continue
            routes, nexthop = hr.compute_service_routes(
                self._core_plugin, context, subnet, services)
            existing = subnet.get('host_routes') or []
            merged = hr.merge(existing, routes, nexthop)
            if hr.routes_equal(merged, existing):
                continue
            if not subnet.get('enable_dhcp') and routes:
                # Routes only reach guests via DHCP option 121. Without
                # DHCP, guests fall back to IPv4 link-local + ARP, which
                # works on Linux but not Windows. Worth a warning.
                LOG.warning(
                    'Injecting host_routes into subnet %s on network %s '
                    'but DHCP is disabled — guests without IPv4 '
                    'link-local will not pick up the route.',
                    subnet['id'], network_id)
            try:
                self._core_plugin.update_subnet(
                    context, subnet['id'],
                    {'subnet': {'host_routes': merged}})
            except Exception:
                # Refresh failures are not fatal — the binding itself
                # is fine, the agent reconciler will recover. Don't
                # propagate; log and keep going.
                LOG.exception(
                    'Failed to update host_routes on subnet %s '
                    '(network %s) during refresh', subnet['id'], network_id)

    # ----- binding lifecycle wrappers -----

    def create_local_service_binding(self, context, local_service_binding):
        result = super().create_local_service_binding(
            context, local_service_binding)
        try:
            self._ensure_localport(context, result['network_id'])
        except Exception:
            # Roll back the binding so the caller doesn't end up with a
            # binding that has no port on the data plane.
            LOG.exception(
                'Failed to ensure localport for binding %s on network %s; '
                'rolling back the binding',
                result['id'], result['network_id'])
            try:
                super().delete_local_service_binding(context, result['id'])
            except Exception:
                LOG.exception('Failed to roll back binding %s', result['id'])
            raise
        self._refresh_subnet_routes(context, result['network_id'])
        return result

    def delete_local_service_binding(self, context, id_):
        binding = self.get_local_service_binding(context, id_)
        super().delete_local_service_binding(context, id_)
        self._refresh_subnet_routes(context, binding['network_id'])
        self._maybe_remove_localport(context, binding['network_id'])
