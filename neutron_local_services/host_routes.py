"""host_routes injection.

For each tenant network where local-services are scoped, we want every
DHCP-enabled subnet to advertise an option-121 entry routing the
service VIP via the localport's subnet IP. Two integration points:

1. Subnet ``BEFORE_CREATE``/``BEFORE_UPDATE`` registry handler
   (``HostRoutesHandler``). Fires for every subnet operation, ours or
   otherwise; merges service routes into the user-input dict before
   the precommit DB write. Catches tenant-driven updates that would
   otherwise strip our routes.

2. Plugin ``_refresh_subnet_routes`` (called from binding create /
   delete hooks). Walks the network's subnets and calls
   ``core_plugin.update_subnet`` with the freshly-computed host_routes.
   The handler in (1) re-fires on our update — the merge is idempotent
   so the second pass is a no-op.

Note: neutron's ML2 plugin publishes BEFORE_CREATE / BEFORE_UPDATE on
``resources.SUBNET`` (not PRECOMMIT_*). The architectural
§4 pseudocode references PRECOMMIT_*; in practice on master the
BEFORE_* events fire just-before the precommit DB writer and the
``states`` dict is mutable, which is what we need.

See ``docs/architecture/overview.md`` for the wider design rationale.
"""

from neutron_lib.callbacks import events
from neutron_lib.callbacks import registry
from neutron_lib.callbacks import resources
from oslo_log import log as logging

from neutron_local_services import constants as lsc
from neutron_local_services.ovn import localport as lp


LOG = logging.getLogger(__name__)


def _localport_ipv4_in_subnet(port, subnet):
    """Return the localport's fixed_ip allocated from ``subnet``, or None.

    A network may have multiple subnets; the localport only owns an IP
    in the one it was allocated from. host_routes for *other* subnets
    can't use this localport as a nexthop (the route would be invalid
    on the wire).
    """
    sid = subnet['id']
    for fip in port.get('fixed_ips') or []:
        if fip.get('subnet_id') == sid:
            return fip.get('ip_address')
    return None


def _service_route(vip, nexthop):
    return {'destination': '%s/32' % vip, 'nexthop': nexthop}


def compute_service_routes(core_plugin, context, subnet, services):
    """Service routes (destination/nexthop pairs) for one subnet.

    Returns ``(routes, nexthop)``. ``nexthop`` is the localport's IP on
    this subnet (or None if the localport doesn't have an IP here, in
    which case ``routes`` is empty). Callers need ``nexthop`` to
    identify previously-injected routes for cleanup — see ``merge``.
    """
    network_id = subnet['network_id']
    port = lp.find_port(core_plugin, context, network_id)
    if port is None:
        return [], None
    nexthop = _localport_ipv4_in_subnet(port, subnet)
    if nexthop is None:
        return [], None
    routes = []
    for svc in services:
        # PoC is IPv4-only (see docs/limitations.md §1). IPv6 RA Route Information
        # is post-PoC.
        vip = svc.get('local_ipv4')
        if not vip or not svc.get('enabled', True):
            continue
        routes.append(_service_route(vip, nexthop))
    return routes, nexthop


def merge(existing, service_routes, nexthop):
    """Merge service routes into a subnet's existing host_routes.

    Rules (see docs/architecture/overview.md):

    * Tenant routes are preserved in their original order.
    * Any existing route whose ``nexthop`` matches our localport IP
      but whose ``destination`` is no longer in the desired set is
      dropped — that's a stale service route from a since-unbound
      service. Identifying our own routes by nexthop is the only
      durable invariant we have without keeping side state.
    * On destination conflict between a tenant route and a current
      service route, the service route wins (operator config beats
      tenant input).
    * Service routes are appended after the surviving tenant routes.

    Returns a fresh list. Caller decides whether to issue an update
    based on equality with ``existing`` (the merge is idempotent —
    re-running on a stable state yields the same list shape).
    """
    desired_dests = {r['destination'] for r in service_routes}
    out = []
    for r in (existing or []):
        dest = r.get('destination')
        if r.get('nexthop') == nexthop and dest not in desired_dests:
            # Stale route we previously injected for a service that's
            # no longer bound. Drop it.
            continue
        if dest in desired_dests:
            # Tenant route conflicts with a current service route;
            # service wins.
            continue
        out.append(r)
    out.extend(service_routes)
    return out


def routes_equal(a, b):
    """Order-insensitive equality for two host_routes lists.

    Neutron preserves order on round-trip but we only care that the
    *set* of (destination, nexthop) pairs matches — we don't want to
    issue an update_subnet just because the merge reorders.
    """
    def norm(lst):
        return sorted(
            (r.get('destination'), r.get('nexthop'))
            for r in (lst or [])
            if r.get('destination') and r.get('nexthop'))
    return norm(a) == norm(b)


class HostRoutesHandler:
    """Registry-callback wiring for SUBNET BEFORE_CREATE / BEFORE_UPDATE.

    Bound to a plugin instance so the handler can reach
    ``get_local_service_bindings`` and ``_core_plugin``. The plugin
    constructs and registers exactly one of these at __init__.
    """

    def __init__(self, plugin):
        self._plugin = plugin

    def register(self):
        registry.subscribe(
            self._on_before_create, resources.SUBNET, events.BEFORE_CREATE)
        registry.subscribe(
            self._on_before_update, resources.SUBNET, events.BEFORE_UPDATE)

    # ----- handlers -----

    def _on_before_create(self, resource, event, trigger, payload):
        # ML2 publishes BEFORE_CREATE with states=(subnet_data,) where
        # subnet_data is the user-input dict (mutable).
        states = payload.states or ()
        if not states:
            return
        subnet_data = states[0]
        if not isinstance(subnet_data, dict):
            return
        self._inject(payload.context, subnet_data, subnet_data)

    def _on_before_update(self, resource, event, trigger, payload):
        # ML2 publishes BEFORE_UPDATE with states=(orig, s) where orig
        # is the existing-subnet dict and s is the user patch dict
        # (mutable).
        states = payload.states or ()
        if len(states) < 2:
            return
        orig, patch = states[0], states[1]
        if not isinstance(patch, dict):
            return
        # If the tenant didn't touch host_routes at all, the existing
        # value (which already has our routes) is preserved by the IPAM
        # update path. No-op.
        if 'host_routes' not in patch:
            return
        # network_id never changes on a subnet update, so read from orig.
        self._inject(payload.context, orig, patch)

    # ----- shared injection logic -----

    def _inject(self, context, subnet_for_lookup, target):
        """Compute service routes for ``subnet_for_lookup`` and mutate
        ``target['host_routes']`` to be the merged list.

        Distinct args because BEFORE_CREATE has no ``orig`` — the
        user-input dict acts as both the lookup source (for
        ``network_id`` / ``id``) and the mutation target.
        """
        network_id = subnet_for_lookup.get('network_id')
        if not network_id:
            return
        # Fabricate a 'subnet'-shaped dict for compute_service_routes.
        # On CREATE the dict won't have an ``id`` yet, so we can't look
        # up the localport's fixed_ip by subnet_id — but we can still
        # contribute routes if the localport happens to live in this
        # very subnet. In practice the binding-create path runs *after*
        # subnet creation, so this only matters for the (rare) case
        # where a subnet is created on a network that already has a
        # binding; the binding's localport sits in *another* subnet of
        # the same network and our nexthop computation correctly
        # returns None for the new subnet.
        subnet_dict = {
            'id': subnet_for_lookup.get('id') or target.get('id'),
            'network_id': network_id,
        }
        services = _enabled_services_for_network(
            self._plugin, context, network_id)
        if not services:
            return
        routes, nexthop = compute_service_routes(
            self._plugin._core_plugin, context, subnet_dict, services)
        if not routes:
            return
        existing = target.get('host_routes') or []
        merged = merge(existing, routes, nexthop)
        if not routes_equal(merged, existing):
            target['host_routes'] = merged


def _enabled_services_for_network(plugin, context, network_id):
    """Return services effectively attached to ``network_id``.

    A service is attached when:

    * ``service.enabled=True`` AND
    * Either:
        - ``attachment_policy='opt-in'`` and a binding row exists with
          ``enabled=True`` (explicit opt-in); or
        - ``attachment_policy='opt-out'`` and no binding row exists with
          ``enabled=False`` (implicit attachment, exclusion via the
          ``enabled=False`` opt-out marker).

    A redundant ``enabled=True`` binding for an opt-out service is a
    no-op: the service is already implicitly attached.
    """
    # Fetch ALL bindings for this network (both enabled and disabled). We
    # need both states: enabled rows are inclusions for opt-in services,
    # disabled rows are exclusions for opt-out services.
    bindings = plugin.get_local_service_bindings(
        context, filters={'network_id': [network_id]})
    enabled_svc_ids = {
        b['service_id'] for b in bindings if b.get('enabled', True)}
    excluded_svc_ids = {
        b['service_id'] for b in bindings if not b.get('enabled', True)}

    services = []
    seen = set()

    # Explicit attachments via enabled bindings. Covers opt-in services
    # the tenant has opted into and the (redundant) case of an opt-out
    # service with an enabled binding.
    for sid in enabled_svc_ids:
        try:
            svc = plugin.get_local_service(context, sid)
        except Exception:
            LOG.exception(
                'Failed to fetch service %s while computing host_routes '
                'for network %s; skipping', sid, network_id)
            continue
        if svc.get('enabled', True) and sid not in seen:
            services.append(svc)
            seen.add(sid)

    # Implicit attachments: every enabled opt-out service applies to
    # this network unless it's been explicitly excluded.
    opt_out = plugin.get_local_services(
        context,
        filters={'attachment_policy': [lsc.ATTACH_OPT_OUT],
                 'enabled': [True]})
    for svc in opt_out:
        sid = svc['id']
        if sid in excluded_svc_ids or sid in seen:
            continue
        services.append(svc)
        seen.add(sid)

    return services
