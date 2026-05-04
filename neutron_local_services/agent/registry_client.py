"""Agent-side client for the local-services REST API.

Pulls "desired VIPs per network" out of the service plugin via two
calls:

  GET /v2.0/local_service_bindings?network_id=<net>&enabled=True
  GET /v2.0/local_services/<service_id>     (per binding)

Why REST poll instead of an OVN-side notification or a NB stash:

* The plugin already exposes the data shape we want.
* OVN NB external_ids would require teaching the mech driver about a
  new key, and writes from the plugin to NB don't have a clean path.
* AMQP notifications would mean a second wire format and a working
  oslo.messaging consumer in the agent — more code than poll for the
  PoC.

Polling cadence is the ``[local_services] reconciler_interval`` opt
(default 10s, see ``conf.py``); the agent extension also calls into
this client on each Port_Binding event so the steady-state latency is
event-driven and the timer is just belt-and-braces.

Auth: keystoneauth1 against the ``[local_services_agent]`` config
section. DevStack's ``plugin.sh`` writes the same admin creds the
neutron-server uses; production deployments would use a service
account scoped to read-only access on these resources.
"""

from keystoneauth1 import exceptions as ks_exc
from keystoneauth1 import loading as ks_loading
from oslo_config import cfg
from oslo_log import log as logging


LOG = logging.getLogger(__name__)

AUTH_GROUP = 'local_services_agent'
SERVICE_TYPE = 'network'


def register_opts(conf=cfg.CONF):
    """Register the keystoneauth1 auth + session opts for the agent.

    Idempotent — safe to call from both the agent extension entry
    point and unit-test setUp.
    """
    ks_loading.register_auth_conf_options(conf, AUTH_GROUP)
    ks_loading.register_session_conf_options(conf, AUTH_GROUP)


class RegistryClient:
    """Thin wrapper around a keystoneauth1 session for our REST endpoints.

    Lazy-builds the session on first call so config registration order
    doesn't matter (the agent extension registers opts at import, but
    the session can't be loaded until ``cfg.CONF`` has been parsed).
    Subsequent calls reuse the cached session.

    Errors are swallowed and logged: a failed poll returns ``[]`` so
    the reconciler can degrade to "no VIPs desired" rather than crash
    the agent thread. The next pass picks up the real desired set.
    """

    def __init__(self, conf=cfg.CONF):
        self._conf = conf
        self._session = None
        self._endpoint = None

    def _get_session(self):
        if self._session is None:
            auth = ks_loading.load_auth_from_conf_options(
                self._conf, AUTH_GROUP)
            self._session = ks_loading.load_session_from_conf_options(
                self._conf, AUTH_GROUP, auth=auth)
        return self._session

    def _get_endpoint(self):
        if self._endpoint is None:
            try:
                self._endpoint = self._get_session().get_endpoint(
                    service_type=SERVICE_TYPE,
                    interface='public')
            except ks_exc.EndpointNotFound:
                LOG.exception(
                    'No %s endpoint in keystone catalog; agent cannot '
                    'reach the local-services API. VIP reconciliation '
                    'will return empty until this is fixed.',
                    SERVICE_TYPE)
                return None
        return self._endpoint

    def _get_json(self, path):
        endpoint = self._get_endpoint()
        if not endpoint:
            return None
        try:
            resp = self._get_session().get(endpoint + path,
                                           raise_exc=False)
        except Exception:
            LOG.exception('Local-services API GET %s failed', path)
            return None
        if resp.status_code != 200:
            LOG.warning('Local-services API GET %s → %s: %s',
                        path, resp.status_code, resp.text[:200])
            return None
        return resp.json()

    def desired_vips_for_network(self, network_id):
        """Return the set of VIP /32 CIDRs scoped to ``network_id``.

        Both the binding and the service must be ``enabled=True`` to
        contribute. Services without a ``local_ipv4`` are skipped (the
        PoC is IPv4-only — see ``docs/limitations.md`` §1).

        Returns ``set[str]`` of CIDRs (e.g. ``{'169.254.169.5/32'}``).
        On any error, returns an empty set and logs.

        Implemented in terms of ``desired_state_for_network`` so the
        VIP path and the plugin path share one fetch.
        """
        return {'%s/32' % svc['local_ipv4']
                for svc in self.desired_state_for_network(network_id)
                if svc.get('local_ipv4')}

    def desired_state_for_network(self, network_id):
        """Return the list of service dicts effectively attached to
        ``network_id``.

        Each service dict carries the full row from
        ``GET /v2.0/local_services/<id>`` plus a ``backends`` list of
        the service's enabled backends from
        ``GET /v2.0/local_service_backends?service_id=...&enabled=True``.

        Effective attachment mirrors the server-side rule (see
        ``host_routes._enabled_services_for_network``):

        * Explicit attachment: an ``enabled=True`` binding row exists
          for the (service, network) pair.
        * Implicit attachment: the service has
          ``attachment_policy='opt-out'`` and ``enabled=True``, and no
          ``enabled=False`` binding row exists for this network.

        Disabled services are filtered out — the operator quiesced them.
        Returns ``[]`` on any error so the agent reconciler can log and
        move on rather than crash. Per-service errors are partial: the
        services that did fetch successfully still appear in the list.
        """
        # Fetch ALL bindings for this network. Both states matter: enabled
        # rows are inclusions; disabled rows are opt-out markers.
        bindings_resp = self._get_json(
            '/v2.0/local_service_bindings?network_id=%s' % network_id)
        bindings = []
        if bindings_resp:
            bindings = bindings_resp.get('local_service_bindings') or []

        enabled_svc_ids = {b['service_id'] for b in bindings
                           if b.get('service_id')
                           and b.get('enabled', True)}
        excluded_svc_ids = {b['service_id'] for b in bindings
                            if b.get('service_id')
                            and not b.get('enabled', True)}

        services = []
        seen = set()

        # Explicit attachments: fetch each by id.
        for sid in enabled_svc_ids:
            svc = self._fetch_service(sid)
            if svc and sid not in seen:
                services.append(svc)
                seen.add(sid)

        # Implicit attachments: every enabled opt-out service applies
        # unless explicitly excluded.
        opt_out_resp = self._get_json(
            '/v2.0/local_services'
            '?attachment_policy=opt-out&enabled=True')
        opt_out = []
        if opt_out_resp:
            opt_out = opt_out_resp.get('local_services') or []
        for svc in opt_out:
            sid = svc.get('id')
            if not sid or sid in seen or sid in excluded_svc_ids:
                continue
            if not svc.get('enabled', True):
                continue
            svc['backends'] = self._fetch_backends(sid)
            services.append(svc)
            seen.add(sid)

        return services

    def _fetch_service(self, service_id):
        """Fetch a single service + its enabled backends. None on error
        or if the service is operator-disabled."""
        svc_resp = self._get_json('/v2.0/local_services/%s' % service_id)
        if not svc_resp:
            return None
        svc = svc_resp.get('local_service') or {}
        if not svc.get('enabled', True):
            return None
        svc['backends'] = self._fetch_backends(service_id)
        return svc

    def _fetch_backends(self, service_id):
        """List enabled backends for one service. Empty list on error."""
        resp = self._get_json(
            '/v2.0/local_service_backends?service_id=%s&enabled=True'
            % service_id)
        if not resp:
            return []
        return [b for b in (resp.get('local_service_backends') or [])
                if b.get('enabled', True)]
