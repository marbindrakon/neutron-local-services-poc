"""oslo.config option registration for the local-services plugin.

Loaded via the `oslo.config.opts` entry point so `oslo-config-generator`
finds our options. Also registered at plugin import time so config
values are available wherever `cfg.CONF` is read.
"""

from keystoneauth1 import loading as ks_loading
from oslo_config import cfg

from neutron_local_services import constants as lsc


GROUP = 'local_services'

# Section the agent reads keystone auth from for REST calls back to
# the local-services API. Distinct from `[local_services]` because
# ks_loading.register_*_conf_options scribbles a fixed set of opt
# names into the section it's given and we don't want those leaking
# into the plugin's own config.
AGENT_AUTH_GROUP = 'local_services_agent'

OPTS = [
    cfg.ListOpt(
        'vip_denylist',
        default=list(lsc.DEFAULT_VIP_DENYLIST),
        help='IPs that may not be used as local-service VIPs. '
             'Defaults include the OpenStack metadata IPs.',
    ),
    cfg.IntOpt(
        'reconciler_interval',
        default=10,
        min=1,
        help='Seconds between agent-side reconciliation passes.',
    ),
    cfg.BoolOpt(
        'allow_az_global_fallback',
        default=False,
        help='If True, agents in an AZ may fall back to global '
             '(no-AZ) backends when no AZ-specific backend is '
             'available. Off by default.',
    ),
]


# Agent-side opts. Distinct from the plugin-side OPTS above because the
# agent reads these only on the chassis (and they don't make sense to
# expose on the API server).
AGENT_OPTS = [
    cfg.StrOpt(
        'underlay_egress_cidr',
        default=lsc.DEFAULT_UNDERLAY_EGRESS_CIDR,
        help='IPv4 CIDR pool the agent carves /30s out of for the '
             'per-tenant underlay-egress veth pairs. Each managed '
             'network consumes one /30. Default '
             '(100.64.0.0/22, RFC6598 carrier-grade NAT) gives 1024 '
             'networks per chassis and is unlikely to collide with '
             'operator private networks.',
    ),
]


def register_opts(conf=cfg.CONF):
    conf.register_opts(OPTS, group=GROUP)


def register_agent_opts(conf=cfg.CONF):
    """Register agent-side opts (e.g. underlay_egress_cidr).

    Lives in the AGENT_AUTH_GROUP section so all agent-side knobs are
    consolidated. Idempotent.
    """
    conf.register_opts(AGENT_OPTS, group=AGENT_AUTH_GROUP)


def register_agent_auth_opts(conf=cfg.CONF):
    """Register keystoneauth1 auth + session opts for agent REST calls.

    Idempotent — keystoneauth1 silently skips re-registration. Called
    from the agent extension at module import time and from unit-test
    setUp.
    """
    ks_loading.register_auth_conf_options(conf, AGENT_AUTH_GROUP)
    ks_loading.register_session_conf_options(conf, AGENT_AUTH_GROUP)


def list_opts():
    return [(GROUP, OPTS),
            (AGENT_AUTH_GROUP, AGENT_OPTS)]
