"""Constants shared across the local-services PoC.

Kept here so the plugin, agent extension, and any helpers can agree on
the magic strings without depending on each other's modules.
"""

ALIAS = 'local-services'
COLLECTION_LOCAL_SERVICE = 'local_services'
COLLECTION_LOCAL_SERVICE_BACKEND = 'local_service_backends'
COLLECTION_LOCAL_SERVICE_BINDING = 'local_service_bindings'

RESOURCE_LOCAL_SERVICE = 'local_service'
RESOURCE_LOCAL_SERVICE_BACKEND = 'local_service_backend'
RESOURCE_LOCAL_SERVICE_BINDING = 'local_service_binding'

# Distinguishing substring inside the OVN-LB-HM device_id we piggyback
# on. The "localsvc-" infix disambiguates our ports from real Octavia
# LB-HM ports while keeping ovn-northd happy with the leading
# "ovn-lb-hm-" that triggers localport materialization.
DEVICE_ID_MARKER = 'localsvc-'

# device_id template: <lb-hm-prefix><our-marker><network_uuid>
# OVN's is_ovn_lb_hm_port() matches device_id.startswith('ovn-lb-hm'),
# so the prefix has to lead. The marker is in the middle to stay
# disambiguable from real Octavia LB-HM ports.
DEVICE_ID_PREFIX = 'ovn-lb-hm-' + DEVICE_ID_MARKER

# Per-network namespace and OVS port name templates.
NETNS_PREFIX = 'localsvc-'

# Attachment policy values.
ATTACH_OPT_IN = 'opt-in'
ATTACH_OPT_OUT = 'opt-out'
ATTACH_POLICIES = (ATTACH_OPT_IN, ATTACH_OPT_OUT)

# Distribution policy values.
DIST_ACTIVE_BACKUP = 'active-backup'
DIST_ROUND_ROBIN = 'round-robin'
DIST_LEAST_CONNECTION = 'least-connection'
DISTRIBUTION_POLICIES = (DIST_ACTIVE_BACKUP, DIST_ROUND_ROBIN,
                         DIST_LEAST_CONNECTION)

# Health-check types. The nat plugin implements `dns` and `ntp` via
# Keepalived's MISC_CHECK with shipped probe scripts; the proxy
# plugin implements all five via its built-in HC engine plus the
# same shipped probe scripts under the `script` HC type.
HC_NONE = 'none'
HC_HTTP = 'http'
HC_HTTPS = 'https'
HC_TCP = 'tcp'
HC_DNS = 'dns'
HC_NTP = 'ntp'
HC_TYPES = (HC_NONE, HC_HTTP, HC_HTTPS, HC_TCP, HC_DNS, HC_NTP)

# Exposure plugins:
#   nat   — Keepalived/ip_vs in the per-tenant netns. Default;
#           uses only widely-packaged keepalived as its userspace
#           binary.
#   proxy — Userspace Rust L4 proxy daemon (`nls-proxy`) with a
#           privileged helper (`nls-proxy-priv`). Pick this when
#           HC fidelity and per-flow observability matter.
EXPOSURE_NAT = 'nat'
EXPOSURE_PROXY = 'proxy'
EXPOSURE_PLUGINS = (EXPOSURE_NAT, EXPOSURE_PROXY)

# Protocol values.
PROTO_TCP = 'tcp'
PROTO_UDP = 'udp'
PROTO_TCP_UDP = 'tcp-udp'
PROTOCOLS = (PROTO_TCP, PROTO_UDP, PROTO_TCP_UDP)

# Default deny-list of VIPs we refuse to allocate — operators can extend.
DEFAULT_VIP_DENYLIST = (
    '169.254.169.254',  # OpenStack metadata
    'fe80::a9fe:a9fe',  # OpenStack metadata IPv6
)

# Default allow-list of CIDRs from which VIPs MAY be allocated. Tighter
# than the denylist on its own: the host_routes injector writes /32
# routes into tenant subnets, so a VIP outside link-local would let an
# operator-managed service silently override tenant routing for any
# destination. Operators can override; we default to RFC3927/4291
# link-local plus the IPv6 fe80::/10 link-local block. The metadata
# IPs in DEFAULT_VIP_DENYLIST are still excluded after this allowlist
# match.
DEFAULT_ALLOWED_VIP_CIDRS = (
    '169.254.0.0/16',  # IPv4 link-local
    'fe80::/10',       # IPv6 link-local
)

# Underlay-egress veth pair (per network), distinct from the tenant-side
# `tls<net[:10]>X` veth that lives on br-int. Linux IFNAMSIZ-1=15 char
# budget: 3-char prefix + 10 net hex chars + 1 suffix = 14. Matches
# the upstream OVN metadata agent's 10-hex-char budget for `tap<...>`
# (see neutron/agent/ovn/metadata/agent.py:_get_veth_name).
UNDERLAY_VETH_PREFIX = 'nls'
UNDERLAY_VETH_NET_LEN = 10

# Default /30 pool for underlay-egress veth pairs. RFC6598 carrier-grade
# NAT space — virtually never collides with operator private networks.
# /22 gives 1024 networks per chassis. Operators can override via
# ``[local_services_agent] underlay_egress_cidr``.
DEFAULT_UNDERLAY_EGRESS_CIDR = '100.64.0.0/22'

# State directory for the underlay /30 allocator.
UNDERLAY_STATE_DIR = '/var/lib/neutron-local-services/_underlay'

# Iptables chain names. Linux caps chain names at 28 chars.
# UNDERLAY_HOST_CHAIN: chassis-wide jump from FORWARD; holds the
#   ESTABLISHED/RELATED accept, the inter-tenant DROP, and a per-network
#   jump per managed network.
# UNDERLAY_PER_NET_CHAIN_PREFIX: per-network sub-chain that whitelists
#   (proto, dst, dport) tuples for the configured backend set. Reconciled
#   on every catalog change. Suffix is the first ``UNDERLAY_VETH_NET_LEN``
#   hex chars of the network UUID, matching the underlay veth so the
#   chain ↔ veth correspondence is trivially derivable. ``NLS_UND_`` +
#   10 = 18 chars, well under the 28-char limit.
UNDERLAY_HOST_CHAIN = 'NEUTRON_LOCAL_SVC_UNDERLAY'
UNDERLAY_PER_NET_CHAIN_PREFIX = 'NLS_UND_'
