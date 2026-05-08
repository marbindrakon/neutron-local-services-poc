"""ovn-agent extension for the local-services PoC.

Subscribes to ``Port_Binding`` events for our localports, hangs
netns + tap plumbing off those events (with a startup sync against
current SB Port_Binding state), and layers link-local /32 VIPs
onto the tap. The reconciler is
event-driven (Port_Binding event → reconcile that network) AND
timer-driven (10s loop walks every network we currently manage and
re-checks against the registry). The timer is belt-and-braces — every
binding-state change fires a Port_Binding update because the plugin
edits the localport's external_ids on each refresh, but a tenant-side
mutation that doesn't touch the localport (e.g. someone toggling a
service's ``enabled`` flag) only reaches the agent via the timer.

Filter strategy: the OVN mech driver mirrors the Neutron port's
``device_id`` into ``LSP.external_ids[neutron:device_id]`` (see
``neutron/plugins/ml2/drivers/ovn/mech_driver/ovsdb/ovn_client.py``).
Our localports carry the ``ovn-lb-hm-localsvc-<network>`` device_id, so
filtering events on ``external_ids`` keeps us tight to our own ports
and skips the rest of the chatter on the chassis.
"""

import time

from neutron.agent.linux import ip_lib
from neutron.agent.ovn.extensions import extension_manager
from neutron.common.ovn import constants as ovn_const
from oslo_config import cfg
from oslo_log import log as logging
from oslo_service import loopingcall
from ovsdbapp.backend.ovs_idl import event as row_event

from neutron_local_services import conf as ls_conf
from neutron_local_services import constants as lsc
from neutron_local_services.agent import netns
from neutron_local_services.agent import registry_client
from neutron_local_services.agent import underlay
from neutron_local_services.agent.plugins import base as plugins_base
# Importing each plugin module is what registers it into
# plugins_base._REGISTRY. Order doesn't matter — the reconciler
# dispatches by name, not import order.
from neutron_local_services.agent.plugins import nat  # noqa: F401
from neutron_local_services.agent.plugins import proxy  # noqa: F401


LOG = logging.getLogger(__name__)
EXT_NAME = 'local_services'

# Register agent-side keystone auth opts at import time so they appear
# in oslo-config-generator output and are available before start().
ls_conf.register_opts()
ls_conf.register_agent_auth_opts()
ls_conf.register_agent_opts()


SB_IDL_TABLES = ['Port_Binding',
                 'Datapath_Binding',
                 'Chassis',
                 'Chassis_Private',
                 ]


def _device_id(row):
    """Pull the Neutron device_id from a Port_Binding's external_ids.

    The ovsdbapp Row exposes ``external_ids`` as a dict-like; missing
    keys yield ``''`` so the substring check below is safe.
    """
    ext_ids = getattr(row, 'external_ids', None) or {}
    return ext_ids.get(ovn_const.OVN_DEVID_EXT_ID_KEY, '') or ''


def _network_id_from_device_id(device_id):
    """Extract the network UUID we encoded into the device_id.

    Format: ``ovn-lb-hm-localsvc-<network_uuid>``. Returns the trailing
    UUID, or ``''`` if the device_id doesn't carry our marker.
    """
    if lsc.DEVICE_ID_PREFIX not in device_id:
        return ''
    return device_id.split(lsc.DEVICE_ID_MARKER, 1)[1]


def _is_our_localport(row):
    """Match Port_Binding rows for our localports.

    Two cheap checks: LSP type must be ``localport`` and the device_id
    in ``external_ids`` must carry our ``localsvc-`` marker. Real
    Octavia LB-HM ports won't have the marker; tenant ports won't have
    type=localport.
    """
    if getattr(row, 'type', None) != ovn_const.LSP_TYPE_LOCALPORT:
        return False
    return lsc.DEVICE_ID_MARKER in _device_id(row)


class _LocalServicesPortBindingEvent(extension_manager.OVNExtensionEvent,
                                     row_event.RowEvent):
    """Base class for our Port_Binding events.

    Each subclass sets ``EVENT`` to one of ``ROW_CREATE`` / ``ROW_UPDATE``
    / ``ROW_DELETE`` and provides a ``run()``. The shared ``match_fn``
    rejects anything that isn't one of our localports.
    """

    def __init__(self, agent):
        super().__init__((self.__class__.EVENT,), 'Port_Binding', None,
                         extension_name=EXT_NAME)
        self._agent = agent
        self.event_name = self.__class__.__name__

    def match_fn(self, event, row, old):
        return _is_our_localport(row)


class LocalportPortBindingCreateEvent(_LocalServicesPortBindingEvent):
    """Localport Port_Binding row appeared → provision the netns + VIPs.

    Fires when ovn-controller realizes the localport on this chassis
    (which happens on every chassis where the network has at least one
    bound port). Provisions the namespace + tap, then asks the registry
    for the desired VIPs and lays them onto the ns-side veth.
    """

    EVENT = row_event.RowEvent.ROW_CREATE

    def run(self, event, row, old):
        network_id = _network_id_from_device_id(_device_id(row))
        LOG.info('local-services: provision netns for network %s '
                 '(localport %s appeared on this chassis)',
                 network_id, row.logical_port)
        try:
            netns.provision(self.agent.ovs_idl, self.agent.ovn_bridge, row)
        except Exception:
            # Don't take the IDL thread down — the next event or the
            # agent restart's sync() will pick this up.
            LOG.exception('netns provision failed for network %s',
                          network_id)
            return
        try:
            underlay.provision_for_network(
                network_id, self.agent.underlay_allocator)
        except Exception:
            LOG.exception('underlay provision failed for network %s; '
                          'nat-plugin services with underlay backends '
                          'will fail HC until next reconcile', network_id)
        self.agent.reconcile_network(network_id)


class LocalportPortBindingUpdatedEvent(_LocalServicesPortBindingEvent):
    """Localport Port_Binding row changed → re-run provision + VIPs.

    Provision is idempotent, so an update fires it again to pick up
    any external_ids changes (MAC/CIDR/MTU). VIP reconciliation is
    also re-run because a binding-add or service-VIP-change on the
    server side flows through ``_refresh_subnet_routes`` → mech-driver
    → SB external_ids churn, which is what wakes us up here.
    """

    EVENT = row_event.RowEvent.ROW_UPDATE

    def run(self, event, row, old):
        network_id = _network_id_from_device_id(_device_id(row))
        LOG.info('local-services: reconcile netns for network %s '
                 '(localport %s updated)',
                 network_id, row.logical_port)
        try:
            netns.provision(self.agent.ovs_idl, self.agent.ovn_bridge, row)
        except Exception:
            LOG.exception('netns reconcile failed for network %s',
                          network_id)
            return
        try:
            underlay.provision_for_network(
                network_id, self.agent.underlay_allocator)
        except Exception:
            LOG.exception('underlay reconcile failed for network %s',
                          network_id)
        self.agent.reconcile_network(network_id)


class LocalportPortBindingDeletedEvent(_LocalServicesPortBindingEvent):
    """Localport Port_Binding row went away → tear down the netns.

    Fires when the service plugin's ``_maybe_remove_localport`` cleanup
    runs on the last unbinding. Tearing down the namespace also drops
    every /32 VIP that was on it, so reconcile_vips doesn't need to
    fire here.
    """

    EVENT = row_event.RowEvent.ROW_DELETE

    def run(self, event, row, old):
        network_id = _network_id_from_device_id(_device_id(row))
        LOG.info('local-services: teardown netns for network %s '
                 '(localport %s removed)',
                 network_id, row.logical_port)
        # Fan plugin teardown out BEFORE the netns goes away — plugins
        # need a live netns to send SIGTERM into. (Cleanup of on-disk
        # state runs unconditionally; even if the process is gone the
        # state dir should be removed.)
        ns_name = lsc.NETNS_PREFIX + network_id
        for plugin in plugins_base.all_plugins():
            try:
                plugin.teardown(ns_name)
            except Exception:
                LOG.exception('Plugin %s teardown failed for network %s',
                              plugin.name, network_id)
        # Tear down underlay-egress veth + iptables before destroying
        # the netns. The host-root-netns iptables rules and the
        # host-side veth are independent of the netns lifecycle, so
        # they must be cleaned up explicitly here or they leak.
        try:
            underlay.teardown_for_network(
                network_id, self.agent.underlay_allocator)
        except Exception:
            LOG.exception('underlay teardown failed for network %s',
                          network_id)
        try:
            netns.teardown(self.agent.ovs_idl, network_id)
        except Exception:
            LOG.exception('netns teardown failed for network %s',
                          network_id)
        # Drop the cached desired-state snapshot. If this network is
        # later re-provisioned on the same chassis, reconcile starts
        # from a fresh registry fetch rather than replaying state from
        # the previous lifetime.
        self.agent.forget_network(network_id)


class LocalServicesExtension(extension_manager.OVNAgentExtension):
    """ovn-agent extension entry point for the local-services PoC.

    Subscribes to localport Port_Binding events, hangs netns provisioning
    off them, and runs a startup sync against current SB state. VIP
    reconciliation layers on top: each PB event triggers an
    immediate per-network reconcile, and a periodic loop re-checks
    every known network against the registry every
    ``[local_services] reconciler_interval`` seconds.
    """

    def __init__(self):
        super().__init__()
        self._registry = None
        self._loop = None
        self._underlay_allocator = None
        # Per-network last-known-good desired state. We hold onto the
        # most recent successful ``desired_state_for_network`` result
        # per network so that a transient registry / Keystone outage
        # doesn't cause the reconciler to withdraw VIPs and listener
        # config that are still operator-desired. The cache is keyed
        # by network_id and dropped on Port_Binding teardown.
        self._lkg_state = {}
        self._lkg_ts = {}  # monotonic timestamp of last successful fetch

    @property
    def name(self):
        return 'local-services OVN agent extension'

    @property
    def ovs_idl_events(self):
        return []

    @property
    def nb_idl_tables(self):
        return []

    @property
    def nb_idl_events(self):
        return []

    @property
    def sb_idl_tables(self):
        return SB_IDL_TABLES

    @property
    def sb_idl_events(self):
        return [LocalportPortBindingCreateEvent,
                LocalportPortBindingUpdatedEvent,
                LocalportPortBindingDeletedEvent]

    # The OVN agent passes itself as `agent_api`, so chassis / ovn_bridge
    # / *_idl come straight off it. (See OVNNeutronAgent in
    # neutron/agent/ovn/agent/ovn_neutron_agent.py.)
    @property
    def ovs_idl(self):
        return self.agent_api.ovs_idl

    @property
    def sb_idl(self):
        return self.agent_api.sb_idl

    @property
    def ovn_bridge(self):
        return self.agent_api.ovn_bridge

    @property
    def chassis(self):
        return self.agent_api.chassis

    @property
    def registry(self):
        """Lazy keystoneauth1-backed REST client for the plugin API.

        Lazy because cfg.CONF isn't fully parsed at import time (the
        opts are registered then, but values come later). First call
        from start() / events / loop is when we actually need it.
        """
        if self._registry is None:
            self._registry = registry_client.RegistryClient()
        return self._registry

    @property
    def underlay_allocator(self):
        """Lazy ``UnderlayAllocator`` for per-network /30 assignment.

        Same lazy pattern as ``registry`` — config values aren't
        available at import time.
        """
        if self._underlay_allocator is None:
            pool_cidr = cfg.CONF[ls_conf.AGENT_AUTH_GROUP].underlay_egress_cidr
            self._underlay_allocator = underlay.UnderlayAllocator(pool_cidr)
        return self._underlay_allocator

    def reconcile_vips_for_network(self, network_id):
        """Pull desired VIPs from the registry, set-diff against the tap.

        Wrapped in try/except so a registry hiccup never takes down the
        IDL thread (events) or the periodic loop. On a fetch failure
        we skip the reconcile rather than withdraw — the next pass will
        retry once the registry is reachable.

        Kept as a thin wrapper around the VIP-only path for the unit-test
        suite; the agent's events and timer go through ``reconcile_network``,
        which fetches state once and dispatches to both VIP
        reconciliation and the exposure-plugin layer.
        """
        if not network_id:
            return
        try:
            desired = self.registry.desired_vips_for_network(network_id)
        except registry_client.RegistryFetchError as exc:
            LOG.warning('Registry fetch failed for network %s; '
                        'skipping VIP reconcile to preserve current '
                        'on-host state: %s', network_id, exc)
            return
        except Exception:
            LOG.exception('Failed to fetch desired VIPs for network %s',
                          network_id)
            return
        try:
            netns.reconcile_vips(network_id, desired)
        except Exception:
            LOG.exception('reconcile_vips failed for network %s',
                          network_id)

    def staleness_seconds(self, network_id):
        """Age (in seconds) of the cached desired state for a network.

        Returns ``None`` if we have never fetched successfully for this
        network. Operators can sample this to alert on a registry that
        has been unreachable for too long; the agent itself just logs.
        """
        ts = self._lkg_ts.get(network_id)
        if ts is None:
            return None
        return max(0.0, time.monotonic() - ts)

    def forget_network(self, network_id):
        """Drop cached last-known-good state for a torn-down network.

        Called from the Port_Binding delete event handler so a
        re-provisioned network on the same chassis doesn't reconcile
        against stale cached state from its previous lifetime.
        """
        self._lkg_state.pop(network_id, None)
        self._lkg_ts.pop(network_id, None)

    def reconcile_network(self, network_id):
        """Full reconcile: VIPs + plugin apply_config.

        One registry fetch per pass — backends and services are read in
        a single ``desired_state_for_network`` call so the VIP set
        and the plugin config don't double-poll the API.

        On a ``RegistryFetchError`` (Keystone catalog miss, transport
        failure, non-2xx, or partial fan-out failure) we fall back to
        the most recent successful fetch for this network so a transient
        registry blip never causes us to withdraw VIPs / listener config
        that the plugin still considers desired. If we have never seen
        a successful fetch (cold start, registry was down at agent
        boot), we skip the pass entirely; the next tick will retry.

        A per-plugin failure logs and moves on so one broken plugin
        doesn't sink the others.
        """
        if not network_id:
            return
        try:
            services = self.registry.desired_state_for_network(network_id)
        except registry_client.RegistryFetchError as exc:
            cached = self._lkg_state.get(network_id)
            if cached is None:
                LOG.warning('Registry fetch failed for network %s and '
                            'no last-known-good state is cached; '
                            'skipping reconcile pass: %s',
                            network_id, exc)
                return
            age = self.staleness_seconds(network_id)
            LOG.warning('Registry fetch failed for network %s; '
                        'reconciling against last-known-good state '
                        '(%d service(s), age %.1fs): %s',
                        network_id, len(cached), age, exc)
            services = cached
        except Exception:
            LOG.exception('Failed to fetch desired state for network %s',
                          network_id)
            return
        else:
            self._lkg_state[network_id] = services
            self._lkg_ts[network_id] = time.monotonic()

        # Keep the kernel ARP-respond addresses on the tap.
        vips = {'%s/32' % s['local_ipv4']
                for s in services if s.get('local_ipv4')}
        try:
            netns.reconcile_vips(network_id, vips)
        except Exception:
            LOG.exception('reconcile_vips failed for network %s',
                          network_id)

        # Dispatch to each registered plugin with its slice of the
        # service set. We always call apply_config — even with an empty
        # services list — so a plugin can clean up any leftover config
        # when its last service goes away.
        ns_name = lsc.NETNS_PREFIX + network_id
        by_plugin = {p.name: [] for p in plugins_base.all_plugins()}
        for svc in services:
            plugin_name = svc.get('exposure_plugin') or lsc.EXPOSURE_NAT
            if plugin_name not in by_plugin:
                # Operator referenced a plugin that isn't loaded.
                # Log once per pass; the agent
                # can't render config it doesn't know how to render.
                LOG.warning('Service %s asks for unknown plugin %r; '
                            'skipping. Loaded plugins: %s',
                            svc.get('id'), plugin_name,
                            sorted(by_plugin))
                continue
            by_plugin[plugin_name].append(svc)
        for plugin in plugins_base.all_plugins():
            try:
                plugin.apply_config(ns_name, by_plugin[plugin.name])
            except Exception:
                LOG.exception('Plugin %s apply_config failed for '
                              'network %s', plugin.name, network_id)

        # Underlay-egress destination ACL — refresh the host-side
        # FORWARD whitelist for nat-plugin backends. Refresh ALL
        # services (not just nat) for two reasons: (1) the proxy
        # plugin's worker reaches backends from the host root netns,
        # so its backends don't need this ACL — but the operator can
        # still configure underlay backends for both plugins, and the
        # ACL costs nothing extra; (2) keeping a single ACL set per
        # network avoids a "did the operator switch this service
        # plugin?" tracking headache.
        try:
            underlay.reconcile_destination_acl(network_id, services)
        except Exception:
            LOG.exception('underlay ACL reconcile failed for network %s',
                          network_id)

    def _list_managed_networks(self):
        """Networks for which we currently have a localsvc- namespace.

        Authoritative source is "what's actually on the host" — the SB
        IDL also knows but a netns set is what reconcile_vips operates
        on. If sync() never ran (degenerate case) this returns the
        empty set and the next PB event will populate it via
        provision().
        """
        managed = set()
        try:
            for ns in ip_lib.list_network_namespaces():
                if ns.startswith(lsc.NETNS_PREFIX):
                    managed.add(ns[len(lsc.NETNS_PREFIX):])
        except Exception:
            LOG.exception('Failed to list namespaces for periodic reconcile')
        return managed

    def _periodic_reconcile(self):
        """Walk every managed network and refresh VIPs + plugin config.

        The combined path (``reconcile_network``) is what events go
        through too, so the periodic loop and the event-driven path
        produce the same on-host state.
        """
        for network_id in self._list_managed_networks():
            self.reconcile_network(network_id)

    def start(self):
        """Bring up the extension: SB → host sync, then start the loop.

        Provisions a netns for every localport we currently see in SB,
        tears down any orphan ``localsvc-`` namespace from a prior
        agent lifetime, runs an initial VIP reconcile against every
        managed network, then starts the periodic loop.
        """
        super().start()

        # Install the chassis-wide host-side iptables prelude before any
        # per-network provisioning so the parent chain exists when we
        # try to add a per-net jump.
        try:
            pool_cidr = cfg.CONF[ls_conf.AGENT_AUTH_GROUP].underlay_egress_cidr
            underlay.install_chassis_chain(pool_cidr)
        except Exception:
            LOG.exception('Failed to install chassis-wide underlay '
                          'iptables chain; nat-plugin services with '
                          'underlay backends will fail until next '
                          'agent restart')

        try:
            netns.sync(self.sb_idl, self.ovs_idl, self.ovn_bridge)
        except Exception:
            LOG.exception('local-services startup sync failed; '
                          'event-driven path will recover on next change')

        # After netns.sync() has provisioned every netns we know about,
        # provision the underlay-egress veth + per-network iptables
        # chain for each. Idempotent — a re-run from a periodic tick
        # would be a no-op if state is already in place.
        for network_id in self._list_managed_networks():
            try:
                underlay.provision_for_network(
                    network_id, self.underlay_allocator)
            except Exception:
                LOG.exception('Underlay startup provision failed for '
                              'network %s', network_id)

        # Initial VIP pass — sync() just brought the namespaces up but
        # didn't put VIPs on them.
        self._periodic_reconcile()

        # Start the timer. FixedIntervalLoopingCall + initial_delay
        # rather than a thread so we plug into the OVN agent's
        # eventlet-aware scheduler the same way the metadata extension
        # does its periodic work.
        interval = cfg.CONF[ls_conf.GROUP].reconciler_interval
        self._loop = loopingcall.FixedIntervalLoopingCall(
            self._periodic_reconcile)
        self._loop.start(interval=interval, initial_delay=interval)
        LOG.info('local-services VIP reconciler running every %ss', interval)
