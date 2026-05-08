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


class RegistryFetchError(Exception):
    """Raised when desired-state fetch can't be completed.

    Distinct from "the API answered with an empty result" — that is
    *authoritative* state and reconciles to nothing. ``RegistryFetchError``
    means the agent could not learn the desired state at all (Keystone
    catalog miss, transport error, non-2xx response, partial fetch
    where one of the dependent calls failed). Callers in the agent
    extension catch this and preserve last-known-good state rather
    than withdraw VIPs / listeners on transient blips.
    """


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

    On any fetch failure (Keystone catalog miss, transport exception,
    non-2xx response, partial fan-out failure) the public methods raise
    ``RegistryFetchError``. Callers must distinguish this from an
    authoritative empty response and preserve last-known-good state on
    the exception path.
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
            except ks_exc.EndpointNotFound as e:
                LOG.exception(
                    'No %s endpoint in keystone catalog; agent cannot '
                    'reach the local-services API.',
                    SERVICE_TYPE)
                raise RegistryFetchError(
                    'No %s endpoint in keystone catalog' % SERVICE_TYPE
                ) from e
        return self._endpoint

    def _get_json(self, path):
        """Issue a GET and return the decoded JSON body.

        Raises ``RegistryFetchError`` on Keystone-catalog miss,
        transport exception, or non-200 response. Callers above
        ``desired_state_for_network`` translate that into "preserve
        last-known-good" behavior.
        """
        endpoint = self._get_endpoint()
        try:
            resp = self._get_session().get(endpoint + path,
                                           raise_exc=False)
        except Exception as e:
            LOG.exception('Local-services API GET %s failed', path)
            raise RegistryFetchError(
                'GET %s raised: %s' % (path, e)) from e
        if resp.status_code != 200:
            LOG.warning('Local-services API GET %s → %s: %s',
                        path, resp.status_code, resp.text[:200])
            raise RegistryFetchError(
                'GET %s returned HTTP %s' % (path, resp.status_code))
        return resp.json()

    def desired_vips_for_network(self, network_id):
        """Return the set of VIP /32 CIDRs scoped to ``network_id``.

        Both the binding and the service must be ``enabled=True`` to
        contribute. Services without a ``local_ipv4`` are skipped (the
        PoC is IPv4-only — see ``docs/limitations.md`` §1).

        Returns ``set[str]`` of CIDRs (e.g. ``{'169.254.169.5/32'}``).
        Raises ``RegistryFetchError`` on any fetch failure — the agent
        extension catches that and reuses last-known-good rather than
        withdraw VIPs.

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

        Raises ``RegistryFetchError`` if any of the fetches needed to
        compose the answer fail (bindings list, opt-out catalog query,
        per-service GET, or backends GET). The agent extension treats
        that as "transient — reuse last-known-good" rather than
        synthesizing a partial answer that would withdraw VIPs the
        plugin still considers desired.
        """
        # Fetch ALL bindings for this network. Both states matter: enabled
        # rows are inclusions; disabled rows are opt-out markers.
        bindings_resp = self._get_json(
            '/v2.0/local_service_bindings?network_id=%s' % network_id)
        bindings = bindings_resp.get('local_service_bindings') or []

        enabled_svc_ids = {b['service_id'] for b in bindings
                           if b.get('service_id')
                           and b.get('enabled', True)}
        excluded_svc_ids = {b['service_id'] for b in bindings
                            if b.get('service_id')
                            and not b.get('enabled', True)}

        services = []
        seen = set()

        # Explicit attachments: fetch each by id. A failure here raises
        # — we don't want a transient 500 on one service to look like a
        # "withdraw it" signal.
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
        """Fetch a single service + its enabled backends.

        Returns ``None`` only if the service is operator-disabled.
        Raises ``RegistryFetchError`` on transport/HTTP failure — a
        404 / 500 here is *not* the same as "the service was deleted":
        the binding list said the service was attached, so an empty
        answer would be inconsistent with what we just read. The next
        reconcile pass will see a consistent (binding-list, service)
        view either way.
        """
        svc_resp = self._get_json('/v2.0/local_services/%s' % service_id)
        svc = svc_resp.get('local_service') or {}
        if not svc.get('enabled', True):
            return None
        svc['backends'] = self._fetch_backends(service_id)
        return svc

    def _fetch_backends(self, service_id):
        """List enabled backends for one service.

        Raises ``RegistryFetchError`` on transport/HTTP failure. An
        authoritative empty list still returns ``[]``.
        """
        resp = self._get_json(
            '/v2.0/local_service_backends?service_id=%s&enabled=True'
            % service_id)
        return [b for b in (resp.get('local_service_backends') or [])
                if b.get('enabled', True)]
