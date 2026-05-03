"""Per-network netns + tap plumbing for the local-services agent.

Brings up a `localsvc-<network>` namespace on the chassis, plumbs a
veth into it from br-int with `iface-id` set to the localport LSP's
logical_port, and puts the localport's subnet IP on the namespace-
side interface. Link-local /32 VIPs layer on top of the same veth.

Two functions are responsible for addresses on the namespace-side
interface and they MUST partition cleanly:

* ``provision()`` manages the on-subnet CIDR (from the Port_Binding
  row's ``external_ids[neutron:cidrs]``). Identified by prefix length
  ``< 32`` — a localport's fixed_ip is never on a /32 subnet in
  practice.
* ``reconcile_vips()`` manages the link-local /32 VIPs (from the
  registry). Identified by ``prefix_len == 32``.

If the predicate ever becomes ambiguous, the two functions will fight
over the same address: provision will see a "stale" VIP and delete it,
reconcile_vips will re-add it the next pass, and so on. Keep the
prefix-length partition, or thread an explicit "scope" tag through.

Design choice: VETH, not "move the OVS internal port". The OVN mech
driver doesn't auto-create an OVS internal port for `type=localport`
LSPs — ovn-controller simply matches the L2 forwarding flows. We
provide the userspace endpoint by creating a veth, registering one
end in br-int as the iface-id'd port (which ovn-controller binds to
the LSP), and leaving the other end inside our namespace. Same shape
the metadata extension uses.

Naming:
- Namespace: ``localsvc-<network_uuid>`` (44 chars, no Linux limit).
- Veth: 14-char ``tls<net[:10]><0|1>`` to fit Linux's IFNAMSIZ-1=15
  limit (matches the metadata pattern of ``tap<datapath[:10]><0|1>``).

This module is deliberately small. It does NOT handle:
- IPv6 (PoC scope decision — see ``docs/limitations.md``).
- Multi-chassis filtering — for now we provision wherever the
  localport row appears in our SB IDL view. Safe on a single-chassis
  deployment, wrong on multi-chassis where the network may not be
  bound on this chassis.
"""

from neutron.agent.linux import ip_lib
from neutron.common.ovn import constants as ovn_const
from neutron.common.ovn import utils as ovn_utils
from oslo_log import log as logging

from neutron_local_services import constants as lsc


LOG = logging.getLogger(__name__)

# Linux interface names cap at IFNAMSIZ-1 == 15. Match the metadata
# agent's "tap<10>X" shape: 3 + 10 + 1 = 14 chars.
_VETH_PREFIX = 'tls'
_VETH_NET_LEN = 10


def _is_ipv4(cidr):
    return ':' not in cidr


def _is_on_subnet_cidr(cidr):
    """True if ``cidr`` is an IPv4 CIDR with prefix length < 32.

    The on-subnet CIDR set provision() manages comes from a localport's
    fixed_ip → always a real subnet (e.g. /24 or /26). The VIP set
    reconcile_vips() manages is /32 link-local. Prefix length is the
    durable partition between the two.
    """
    if not _is_ipv4(cidr):
        return False
    if '/' not in cidr:
        return False
    try:
        prefix = int(cidr.rsplit('/', 1)[1])
    except ValueError:
        return False
    return prefix < 32


def _is_vip_cidr(cidr):
    """True if ``cidr`` is an IPv4 /32. Companion to ``_is_on_subnet_cidr``."""
    if not _is_ipv4(cidr):
        return False
    return cidr.endswith('/32')


def netns_name(network_id):
    """Return the namespace name for a given network."""
    return lsc.NETNS_PREFIX + network_id


def veth_names(network_id):
    """Return ``(root_end, ns_end)`` veth interface names.

    Root end gets the ``0`` suffix (lives in init netns, plumbed into
    br-int); namespace end gets ``1`` (moved into ``localsvc-<net>``,
    carries MAC + IPs).
    """
    short = network_id[:_VETH_NET_LEN]
    return _VETH_PREFIX + short + '0', _VETH_PREFIX + short + '1'


def _parse_port_binding(row):
    """Pull (mac, ipv4_cidrs, logical_port, mtu) from a Port_Binding row.

    Falls back to ``None``/``0`` for any field that isn't usable —
    callers must check before provisioning.
    """
    try:
        mac, _ips = ovn_utils.get_mac_and_ips_from_port_binding(row)
    except (ValueError, IndexError):
        LOG.warning('Localport %s has no usable MAC; skipping',
                    row.logical_port)
        return None, [], row.logical_port, 0

    ext_ids = getattr(row, 'external_ids', None) or {}
    cidr_blob = ext_ids.get(ovn_const.OVN_CIDRS_EXT_ID_KEY, '') or ''
    ipv4_cidrs = [c for c in cidr_blob.split(' ')
                  if c and ':' not in c]  # IPv6 has ':' — skip

    mtu_raw = ext_ids.get(ovn_const.OVN_NETWORK_MTU_EXT_ID_KEY, '') or '0'
    try:
        mtu = int(mtu_raw)
    except ValueError:
        mtu = 0

    return mac, ipv4_cidrs, row.logical_port, mtu


def provision(ovs_idl, ovn_bridge, row):
    """Provision the per-network netns + plumb the localport's veth.

    Idempotent — re-running is safe. Returns the namespace name on
    success, ``None`` on a soft skip (no MAC, no IPv4 CIDR).
    """
    ext_ids = getattr(row, 'external_ids', None) or {}
    device_id = ext_ids.get(ovn_const.OVN_DEVID_EXT_ID_KEY, '')
    if lsc.DEVICE_ID_MARKER not in device_id:
        # Defensive — match_fn should have filtered this already.
        return None
    network_id = device_id.split(lsc.DEVICE_ID_MARKER, 1)[1]

    mac, ipv4_cidrs, logical_port, mtu = _parse_port_binding(row)
    if not mac:
        return None
    if not ipv4_cidrs:
        LOG.warning('Localport %s on network %s has no IPv4 CIDR in '
                    'external_ids; skipping (link-local VIPs alone '
                    'are not enough — we need an on-subnet IP for the '
                    'guest-side nexthop)',
                    logical_port, network_id)
        return None

    ns = netns_name(network_id)
    root_veth, ns_veth = veth_names(network_id)

    # Create veth + namespace if the namespace-side end doesn't exist.
    # The root-side end can exist on its own from a botched prior run;
    # if so, delete it first so add_veth doesn't trip on EEXIST.
    root_dev = ip_lib.IPDevice(root_veth)
    if ip_lib.device_exists(ns_veth, namespace=ns):
        ns_dev = ip_lib.IPDevice(ns_veth, namespace=ns)
    else:
        if root_dev.exists():
            LOG.debug('Stale root-side veth %s without ns peer; deleting',
                      root_veth)
            root_dev.link.delete()
        LOG.info('Creating veth %s/%s for netns %s', root_veth, ns_veth, ns)
        root_dev, ns_dev = ip_lib.IPWrapper().add_veth(
            root_veth, ns_veth, namespace2=ns)

    # MAC on the namespace side so guests see the LSP's MAC ARP-respond.
    ns_dev.link.set_address(mac)

    # MTU is best-effort — empty external_ids[neutron:mtu] is normal.
    if mtu:
        root_dev.link.set_mtu(mtu)
        ns_dev.link.set_mtu(mtu)

    root_dev.link.set_up()
    ns_dev.link.set_up()

    # IPv4 CIDR reconciliation — on-subnet only. The /32 VIPs that
    # reconcile_vips() manages must be left strictly alone here, or the
    # two reconcilers will fight (provision deletes a "stale" VIP →
    # reconcile_vips re-adds it next pass → loop). Filter both `current`
    # and `desired` to non-/32 IPv4 CIDRs.
    current = {dev['cidr'] for dev in ns_dev.addr.list()
               if _is_on_subnet_cidr(dev['cidr'])}
    desired = {c for c in ipv4_cidrs if _is_on_subnet_cidr(c)}
    to_add = desired - current
    to_del = current - desired
    if to_del:
        ns_dev.addr.delete_multiple(list(to_del))
    if to_add:
        ns_dev.addr.add_multiple(list(to_add))

    # Plumb the root end into br-int with iface-id == logical_port.
    # ovn-controller binds the LSP to whichever local OVS interface
    # has matching iface-id.
    ovs_idl.add_port(ovn_bridge, root_veth).execute()
    ovs_idl.db_set(
        'Interface', root_veth,
        ('external_ids', {'iface-id': logical_port})).execute()

    LOG.info('Provisioned netns %s (veth %s↔%s, mac=%s, cidrs=%s)',
             ns, root_veth, ns_veth, mac, sorted(desired))
    return ns


def reconcile_vips(network_id, desired_vips):
    """Reconcile link-local /32 VIPs on the namespace-side veth.

    ``desired_vips`` is an iterable of CIDR strings (``'<vip>/32'``)
    sourced from the registry by the caller. We diff against the
    /32 IPv4 addresses already on the ns-side veth and apply the
    minimum set of add/delete operations.

    Returns ``(added, removed)`` for caller logging. ``(set(), set())``
    means a clean no-op — the steady state.

    Soft-fails (returns empty tuples) when the namespace doesn't exist
    yet: the reconciler runs on a 10s timer and may fire before
    provision() has come back from the SB Port_Binding event. The next
    pass will reconcile correctly.
    """
    ns = netns_name(network_id)
    _, ns_veth = veth_names(network_id)

    if not ip_lib.network_namespace_exists(ns):
        LOG.debug('reconcile_vips: namespace %s not present yet; skipping',
                  ns)
        return set(), set()
    if not ip_lib.device_exists(ns_veth, namespace=ns):
        LOG.debug('reconcile_vips: ns-side veth %s not present yet; '
                  'skipping (provision pending?)', ns_veth)
        return set(), set()

    ns_dev = ip_lib.IPDevice(ns_veth, namespace=ns)
    desired = {c for c in desired_vips if _is_vip_cidr(c)}
    current = {dev['cidr'] for dev in ns_dev.addr.list()
               if _is_vip_cidr(dev['cidr'])}

    to_add = desired - current
    to_del = current - desired

    if to_del:
        ns_dev.addr.delete_multiple(list(to_del))
    if to_add:
        ns_dev.addr.add_multiple(list(to_add))

    if to_add or to_del:
        LOG.info('reconcile_vips on %s: +%s -%s',
                 ns, sorted(to_add), sorted(to_del))
    return to_add, to_del


def teardown(ovs_idl, network_id):
    """Tear down the netns + veth for a given network.

    Idempotent — missing interfaces / namespaces are no-ops, so this
    is safe to call from a delete event after the OVS port has already
    gone (e.g. on agent restart racing with ovn-controller cleanup).
    """
    ns = netns_name(network_id)
    root_veth, _ = veth_names(network_id)

    ip = ip_lib.IPWrapper(namespace=ns)
    if not ip.netns.exists(ns):
        # Nothing to do, but still clear any orphan root-side state.
        ovs_idl.del_port(root_veth).execute()
        if ip_lib.device_exists(root_veth):
            ip_lib.IPWrapper().del_veth(root_veth)
        return False

    LOG.info('Tearing down netns %s', ns)

    ovs_idl.del_port(root_veth).execute()
    if ip_lib.device_exists(root_veth):
        ip_lib.IPWrapper().del_veth(root_veth)
    ip.garbage_collect_namespace()
    return True


def sync(sb_idl, ovs_idl, ovn_bridge):
    """Reconcile the netns set against current SB Port_Binding state.

    Called on agent start. For each Port_Binding row in SB that is
    one of our localports (type=localport AND localsvc- marker),
    provision; for any leftover ``localsvc-`` namespace that doesn't
    map to a current localport row, tear it down.

    No multi-chassis filtering yet (see module docstring).
    """
    rows = sb_idl.db_list_rows('Port_Binding').execute(check_error=True)
    desired_networks = set()
    for row in rows:
        if getattr(row, 'type', None) != ovn_const.LSP_TYPE_LOCALPORT:
            continue
        ext_ids = getattr(row, 'external_ids', None) or {}
        device_id = ext_ids.get(ovn_const.OVN_DEVID_EXT_ID_KEY, '')
        if lsc.DEVICE_ID_MARKER not in device_id:
            continue
        try:
            ns = provision(ovs_idl, ovn_bridge, row)
            if ns:
                desired_networks.add(
                    device_id.split(lsc.DEVICE_ID_MARKER, 1)[1])
        except Exception:
            LOG.exception('Sync provision failed for localport %s',
                          row.logical_port)

    # Garbage-collect orphan namespaces from previous runs.
    for ns in ip_lib.list_network_namespaces():
        if not ns.startswith(lsc.NETNS_PREFIX):
            continue
        net_id = ns[len(lsc.NETNS_PREFIX):]
        if net_id not in desired_networks:
            LOG.info('Sync: removing orphan netns %s', ns)
            try:
                teardown(ovs_idl, net_id)
            except Exception:
                LOG.exception('Sync teardown failed for orphan netns %s', ns)
