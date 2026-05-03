"""Localport piggyback via the OVN LB-HM device_owner.

One Neutron port per network where local-services are scoped. The OVN
mech driver's `_get_port_options()` checks `is_ovn_lb_hm_port(port)`
and forces `LSP.type = localport` for matches, which is what we want
without having to extend the mech driver.

See `docs/architecture/overview.md` for the full rationale (why LB-HM
and not metadata, what the proper upstream solution looks like, etc.).
"""

import time

from neutron_lib import exceptions as n_exc
from neutron_lib.plugins import utils as p_utils
from oslo_log import log as logging

from neutron.common.ovn import constants as ovn_const

from neutron_local_services import constants as lsc


LOG = logging.getLogger(__name__)


class LocalportLSPVerifyError(n_exc.NeutronException):
    message = ('Local-services localport %(port_id)s on network '
               '%(network_id)s did not become an OVN localport LSP. '
               'Got LSP type=%(lsp_type)r — the LB-HM piggyback may have '
               'broken (mech driver behavior change?). See '
               'docs/architecture/overview.md.')


def device_id_for(network_id):
    """Build the device_id for our localport on a given network.

    Format: ``ovn-lb-hm-localsvc-<network_uuid>``. The ``ovn-lb-hm``
    prefix is what makes the OVN mech driver recognize this as an
    LB-HM port (and create it as a localport); ``localsvc-`` is our
    marker so we can disambiguate from real Octavia LB-HM ports when
    scanning.
    """
    return lsc.DEVICE_ID_PREFIX + network_id


def is_our_port(port):
    """Match a port we own.

    The Octavia OVN provider also uses
    ``OVN_LB_HM_PORT_DISTRIBUTED`` as device_owner, so device_owner
    alone is not sufficient — match on the marker substring inside
    device_id too. (In practice the Octavia conflict guard at plugin
    init prevents the two from coexisting; this is defensive.)
    """
    return (
        port.get('device_owner') == ovn_const.OVN_LB_HM_PORT_DISTRIBUTED
        and lsc.DEVICE_ID_MARKER in (port.get('device_id') or '')
    )


def find_port(core_plugin, context, network_id):
    """Return our localport on this network, or None.

    Filters at the DB layer by network and device_id, then double-checks
    `is_our_port` defensively in case the filter ever surfaces something
    we didn't expect.
    """
    candidates = core_plugin.get_ports(
        context,
        filters={
            'network_id': [network_id],
            'device_owner': [ovn_const.OVN_LB_HM_PORT_DISTRIBUTED],
            'device_id': [device_id_for(network_id)],
        })
    for port in candidates:
        if is_our_port(port):
            return port
    return None


def _pick_subnet(core_plugin, context, network_id):
    """Pick the subnet to allocate the localport's fixed_ip from.

    Prefer DHCP-enabled IPv4 subnets (so the host_routes injector has
    somewhere to land). Fall back to any IPv4 subnet otherwise; warn
    that route injection won't work for the fall-back case.

    Returns the subnet dict, or None if no IPv4 subnet exists.
    """
    subnets = core_plugin.get_subnets(
        context, filters={'network_id': [network_id]})
    ipv4 = [s for s in subnets if s.get('ip_version') == 4]
    if not ipv4:
        return None
    dhcp = [s for s in ipv4 if s.get('enable_dhcp')]
    if dhcp:
        return dhcp[0]
    LOG.warning(
        'Network %s has no DHCP-enabled IPv4 subnet; service VIPs '
        'will only reach guests with IPv4 link-local configured '
        '(no DHCP option-121 route injection possible).', network_id)
    return ipv4[0]


def ensure_localport(core_plugin, context, network):
    """Idempotently ensure our localport exists on the network.

    Returns the existing or newly-created port dict. Caller is
    responsible for any post-create verification (see
    `verify_lsp_type`).
    """
    network_id = network['id']
    existing = find_port(core_plugin, context, network_id)
    if existing:
        return existing

    subnet = _pick_subnet(core_plugin, context, network_id)
    if subnet is None:
        raise n_exc.InvalidInput(
            error_message=(
                'cannot create local-services localport on network %s: '
                'network has no IPv4 subnet' % network_id))

    port_body = {
        'port': {
            'network_id': network_id,
            'project_id': (network.get('project_id') or
                           network.get('tenant_id') or ''),
            'device_owner': ovn_const.OVN_LB_HM_PORT_DISTRIBUTED,
            'device_id': device_id_for(network_id),
            'fixed_ips': [{'subnet_id': subnet['id']}],
            'admin_state_up': True,
            'name': 'local-services-localport-%s' % network_id,
            'port_security_enabled': False,
        }
    }
    port = p_utils.create_port(core_plugin, context, port_body)
    LOG.info('Created local-services localport %s on network %s '
             '(subnet %s, fixed_ips=%s)',
             port['id'], network_id, subnet['id'],
             port.get('fixed_ips'))
    return port


def maybe_remove_localport(core_plugin, context, network_id,
                           has_remaining_bindings):
    """Delete our localport iff no bindings remain on the network.

    The caller computes ``has_remaining_bindings`` because they hold
    the DB session and know the binding state at the right point in
    the transaction.
    """
    if has_remaining_bindings:
        return False
    port = find_port(core_plugin, context, network_id)
    if not port:
        return False
    LOG.info('Deleting local-services localport %s on network %s '
             '(last binding removed)', port['id'], network_id)
    core_plugin.delete_port(context, port['id'])
    return True


def verify_lsp_type(mech_driver, port_id, network_id,
                    retries=5, delay=0.2):
    """Confirm the LSP we just created really is a `localport`.

    The piggyback depends on mech-driver behavior we don't own. A
    silent failure here would manifest as a regular tenant port that
    flooded VIP ARPs across the network — exactly the kind of bug
    that's expensive to diagnose later. So we raise loud.

    Returns the LSP type observed on success, or None if the mech
    driver / NB IDL isn't available (unit-test contexts).

    The brief retry tolerates the post-commit ordering between Neutron's
    port-create transaction and the OVN mech driver's NB write.
    """
    if mech_driver is None or getattr(mech_driver, 'nb_ovn', None) is None:
        LOG.debug('Skipping LSP type verification for %s: no OVN NB IDL '
                  'available on mech driver (unit-test context)', port_id)
        return None
    nb_ovn = mech_driver.nb_ovn
    last_seen = None
    for _ in range(retries):
        try:
            lsp = nb_ovn.lookup('Logical_Switch_Port', port_id, default=None)
        except Exception as exc:
            LOG.debug('LSP lookup raised %s; retrying', exc)
            lsp = None
        if lsp is not None:
            last_seen = getattr(lsp, 'type', '') or ''
            if last_seen == ovn_const.LSP_TYPE_LOCALPORT:
                return last_seen
            # LSP exists but wrong type — the piggyback is broken.
            raise LocalportLSPVerifyError(
                port_id=port_id, network_id=network_id, lsp_type=last_seen)
        time.sleep(delay)
    LOG.warning(
        'LSP %s for network %s not found in OVN NB after %d retries; '
        'mech driver may not have synced yet. Skipping verification.',
        port_id, network_id, retries)
    return None
