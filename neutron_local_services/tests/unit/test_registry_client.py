"""Unit tests for the agent-side REST client.

The session and HTTP machinery are mocked at the keystoneauth1
boundary — we don't open real sockets. What matters here is the
fan-out from one bindings list call to per-service GETs and the
filtering rules (disabled service, missing local_ipv4, dedup).
"""

from unittest import mock

import testtools

from neutron_local_services.agent import registry_client


NET_ID = '11111111-1111-1111-1111-111111111111'


class _Resp:
    """Minimal stand-in for keystoneauth1's HTTP response object."""

    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = '' if isinstance(body, dict) else str(body)

    def json(self):
        return self._body


class TestRegistryClient(testtools.TestCase):

    def setUp(self):
        super().setUp()
        self.client = registry_client.RegistryClient()
        self.session = mock.Mock()
        self.session.get_endpoint.return_value = 'http://neutron/networking'
        # Bypass lazy session construction.
        self.client._session = self.session
        self.client._endpoint = 'http://neutron/networking'

    def _set_responses(self, mapping):
        """Stage HTTP responses keyed by URL substring.

        Tests call _set_responses({'local_service_bindings': resp_a,
                                   '<svc-id>': resp_b}) and the mock
        ``session.get`` looks the substring up.
        """
        def _get(url, raise_exc=False):
            for key, resp in mapping.items():
                if key in url:
                    return resp
            # Default fallback for URLs the test didn't stage. We
            # return an empty-body 200 (rather than a 404) because
            # ``desired_state_for_network`` always fetches both the
            # binding list and the opt-out catalog; tests that only
            # care about one of those would otherwise need to stage
            # the other just to avoid spurious RegistryFetchError.
            return _Resp(200, {})
        self.session.get.side_effect = _get

    def test_returns_set_of_vip_cidrs(self):
        svc_id = 'svc-aaaa'
        self._set_responses({
            'local_service_bindings': _Resp(200, {
                'local_service_bindings': [
                    {'service_id': svc_id, 'network_id': NET_ID,
                     'enabled': True}],
            }),
            svc_id: _Resp(200, {
                'local_service': {
                    'id': svc_id, 'local_ipv4': '169.254.169.5',
                    'enabled': True}
            }),
        })
        self.assertEqual({'169.254.169.5/32'},
                         self.client.desired_vips_for_network(NET_ID))

    def test_dedups_multiple_bindings_to_same_service(self):
        # Same service bound twice (unusual but legal). One svc fetch
        # (via /local_services/<id>); the backends GET is a separate
        # path so the assertion targets the per-service URL pattern,
        # not "any URL containing the svc_id".
        svc_id = 'svc-bb'
        self._set_responses({
            'local_service_bindings': _Resp(200, {
                'local_service_bindings': [
                    {'service_id': svc_id, 'network_id': NET_ID,
                     'enabled': True},
                    {'service_id': svc_id, 'network_id': NET_ID,
                     'enabled': True}],
            }),
            '/local_services/' + svc_id: _Resp(200, {
                'local_service': {'local_ipv4': '169.254.10.10',
                                  'enabled': True}
            }),
            'local_service_backends': _Resp(200, {
                'local_service_backends': []}),
        })
        self.client.desired_vips_for_network(NET_ID)
        # Two bindings, but only one fetch for the (single) service.
        svc_calls = [c for c in self.session.get.call_args_list
                     if ('/local_services/' + svc_id) in c.args[0]]
        self.assertEqual(1, len(svc_calls))

    def test_filters_disabled_service(self):
        # Even with an enabled binding, a disabled service contributes
        # nothing — operator may have temporarily quiesced it.
        svc_id = 'svc-disabled'
        self._set_responses({
            'local_service_bindings': _Resp(200, {
                'local_service_bindings': [
                    {'service_id': svc_id, 'network_id': NET_ID,
                     'enabled': True}],
            }),
            svc_id: _Resp(200, {
                'local_service': {'local_ipv4': '169.254.1.1',
                                  'enabled': False}
            }),
        })
        self.assertEqual(set(),
                         self.client.desired_vips_for_network(NET_ID))

    def test_skips_service_without_local_ipv4(self):
        # Per docs/limitations.md §1, IPv6 is post-PoC; an IPv6-only service
        # contributes no v4 VIP.
        svc_id = 'svc-v6'
        self._set_responses({
            'local_service_bindings': _Resp(200, {
                'local_service_bindings': [
                    {'service_id': svc_id, 'network_id': NET_ID,
                     'enabled': True}],
            }),
            svc_id: _Resp(200, {
                'local_service': {'local_ipv4': None,
                                  'local_ipv6': 'fe80::1',
                                  'enabled': True}
            }),
        })
        self.assertEqual(set(),
                         self.client.desired_vips_for_network(NET_ID))

    def test_returns_empty_when_no_bindings(self):
        self._set_responses({
            'local_service_bindings': _Resp(200, {
                'local_service_bindings': []}),
        })
        self.assertEqual(set(),
                         self.client.desired_vips_for_network(NET_ID))

    def test_raises_on_bindings_api_error(self):
        # 500 on the bindings list must raise RegistryFetchError so the
        # caller (agent extension) can fall back to last-known-good
        # state instead of withdrawing every VIP on a transient blip.
        self._set_responses({
            'local_service_bindings': _Resp(500, 'oops'),
        })
        self.assertRaises(registry_client.RegistryFetchError,
                          self.client.desired_vips_for_network, NET_ID)

    def test_raises_on_per_service_error(self):
        # If any per-service fetch fails we must signal the failure up.
        # Returning a partial answer would falsely tell the caller "the
        # other service is gone, withdraw it" — exactly what LKG
        # caching was added to prevent.
        svc_a, svc_b = 'svc-aa', 'svc-bb'
        self._set_responses({
            'local_service_bindings': _Resp(200, {
                'local_service_bindings': [
                    {'service_id': svc_a, 'network_id': NET_ID,
                     'enabled': True},
                    {'service_id': svc_b, 'network_id': NET_ID,
                     'enabled': True}],
            }),
            svc_a: _Resp(500, 'down'),
            svc_b: _Resp(200, {
                'local_service': {'local_ipv4': '169.254.2.2',
                                  'enabled': True}}),
        })
        self.assertRaises(registry_client.RegistryFetchError,
                          self.client.desired_vips_for_network, NET_ID)

    def test_filters_disabled_binding(self):
        # Server-side filter (``?enabled=True``) carries the load, but
        # the client also defends against an older server / TOCTOU /
        # test mock that lets a disabled binding leak through. Stage
        # one anyway and confirm the client still skips it without
        # fetching the service.
        svc_id = 'svc-disabled-binding'
        self._set_responses({
            'local_service_bindings': _Resp(200, {
                'local_service_bindings': [
                    {'service_id': svc_id, 'network_id': NET_ID,
                     'enabled': False}],
            }),
            svc_id: _Resp(200, {
                'local_service': {'local_ipv4': '169.254.99.1',
                                  'enabled': True}
            }),
        })
        self.assertEqual(set(),
                         self.client.desired_vips_for_network(NET_ID))
        # And the service GET should NOT have been issued — disabled
        # bindings short-circuit the per-service fetch.
        svc_calls = [c for c in self.session.get.call_args_list
                     if svc_id in c.args[0]]
        self.assertEqual(0, len(svc_calls))

    def test_raises_when_endpoint_unavailable(self):
        # Catalog has no `network` service — agent can't reach the API.
        # That's a fetch failure, not "no VIPs desired"; signal it up
        # so the caller preserves last-known-good rather than thrashing
        # the tap to empty on every periodic tick during a Keystone
        # outage.
        self.client._endpoint = None
        from keystoneauth1 import exceptions as ks_exc
        self.session.get_endpoint.side_effect = ks_exc.EndpointNotFound()
        self.assertRaises(registry_client.RegistryFetchError,
                          self.client.desired_vips_for_network, NET_ID)

    def test_raises_on_request_exception(self):
        # Transport-level failure (DNS, TCP RST, TLS hiccup): the
        # session.get call itself raises. Translate into our typed
        # exception so callers don't have to special-case requests
        # internals.
        self.session.get.side_effect = RuntimeError('connection reset')
        self.assertRaises(registry_client.RegistryFetchError,
                          self.client.desired_vips_for_network, NET_ID)


class TestDesiredStateForNetwork(testtools.TestCase):
    """Full service+backend fetch for the plugin reconciler.

    The function fans out per-binding to ``GET local_services/<id>`` and
    per-service to ``GET local_service_backends?service_id=<id>``. We
    cover the happy path, partial-error paths, the disabled-service /
    disabled-backend filters, and the dedup-on-multiple-bindings case.
    """

    def setUp(self):
        super().setUp()
        self.client = registry_client.RegistryClient()
        self.session = mock.Mock()
        self.session.get_endpoint.return_value = 'http://neutron/networking'
        self.client._session = self.session
        self.client._endpoint = 'http://neutron/networking'

    def _set_responses(self, mapping):
        def _get(url, raise_exc=False):
            for key, resp in mapping.items():
                if key in url:
                    return resp
            # Default fallback for URLs the test didn't stage. We
            # return an empty-body 200 (rather than a 404) because
            # ``desired_state_for_network`` always fetches both the
            # binding list and the opt-out catalog; tests that only
            # care about one of those would otherwise need to stage
            # the other just to avoid spurious RegistryFetchError.
            return _Resp(200, {})
        self.session.get.side_effect = _get

    def test_returns_full_service_with_backends(self):
        svc_id = 'svc-aa'
        be1, be2 = 'be-1', 'be-2'
        self._set_responses({
            'local_service_bindings': _Resp(200, {
                'local_service_bindings': [
                    {'service_id': svc_id, 'network_id': NET_ID,
                     'enabled': True}],
            }),
            '/local_services/' + svc_id: _Resp(200, {
                'local_service': {
                    'id': svc_id, 'name': 'dns', 'local_ipv4': '169.254.169.5',
                    'port': 53, 'protocol': 'udp', 'enabled': True,
                    'exposure_plugin': 'nat'},
            }),
            'local_service_backends': _Resp(200, {
                'local_service_backends': [
                    {'id': be1, 'service_id': svc_id, 'address': '10.0.0.10',
                     'port': 53, 'enabled': True, 'weight': 1},
                    {'id': be2, 'service_id': svc_id, 'address': '10.0.0.11',
                     'port': 53, 'enabled': True, 'weight': 2},
                ]}),
        })
        services = self.client.desired_state_for_network(NET_ID)
        self.assertEqual(1, len(services))
        svc = services[0]
        self.assertEqual('dns', svc['name'])
        self.assertEqual(2, len(svc['backends']))
        self.assertEqual({'10.0.0.10', '10.0.0.11'},
                         {b['address'] for b in svc['backends']})

    def test_filters_disabled_service(self):
        svc_id = 'svc-disabled'
        self._set_responses({
            'local_service_bindings': _Resp(200, {
                'local_service_bindings': [
                    {'service_id': svc_id, 'network_id': NET_ID,
                     'enabled': True}],
            }),
            svc_id: _Resp(200, {
                'local_service': {'id': svc_id, 'enabled': False,
                                  'local_ipv4': '169.254.1.1'}}),
        })
        self.assertEqual([],
                         self.client.desired_state_for_network(NET_ID))

    def test_filters_disabled_backends(self):
        # Server-side filter (``?enabled=True``) is the primary defense;
        # client-side filter is belt-and-braces.
        svc_id = 'svc-mixed'
        self._set_responses({
            'local_service_bindings': _Resp(200, {
                'local_service_bindings': [
                    {'service_id': svc_id, 'network_id': NET_ID,
                     'enabled': True}],
            }),
            '/local_services/' + svc_id: _Resp(200, {
                'local_service': {'id': svc_id, 'enabled': True,
                                  'local_ipv4': '1.1.1.1'},
            }),
            'local_service_backends': _Resp(200, {
                'local_service_backends': [
                    {'id': 'b1', 'service_id': svc_id, 'enabled': True,
                     'address': '10.0.0.1', 'port': 80, 'weight': 1},
                    # An entry that leaked through with enabled=False.
                    {'id': 'b2', 'service_id': svc_id, 'enabled': False,
                     'address': '10.0.0.2', 'port': 80, 'weight': 1}]}),
        })
        services = self.client.desired_state_for_network(NET_ID)
        self.assertEqual(1, len(services[0]['backends']))
        self.assertEqual('10.0.0.1', services[0]['backends'][0]['address'])

    def test_raises_on_bindings_error(self):
        # A bindings-list error means we don't know what's attached.
        # Raise so the caller can keep last-known-good.
        self._set_responses({'local_service_bindings': _Resp(500, 'oops')})
        self.assertRaises(registry_client.RegistryFetchError,
                          self.client.desired_state_for_network, NET_ID)

    def test_raises_on_per_service_error(self):
        # An enabled binding row promises the service exists. If the
        # per-service GET fails we don't synthesize a partial answer —
        # we raise so the agent reuses last-known-good for *all*
        # services on this network. A partial answer would have looked
        # exactly like "svc_a was deleted, withdraw it".
        svc_a, svc_b = 'svc-a', 'svc-b'
        self._set_responses({
            'local_service_bindings': _Resp(200, {
                'local_service_bindings': [
                    {'service_id': svc_a, 'network_id': NET_ID,
                     'enabled': True},
                    {'service_id': svc_b, 'network_id': NET_ID,
                     'enabled': True}],
            }),
            '/local_services/' + svc_a: _Resp(500, 'down'),
            '/local_services/' + svc_b: _Resp(200, {
                'local_service': {'id': svc_b, 'enabled': True,
                                  'local_ipv4': '169.254.2.2'}}),
            'local_service_backends': _Resp(200, {
                'local_service_backends': []}),
        })
        self.assertRaises(registry_client.RegistryFetchError,
                          self.client.desired_state_for_network, NET_ID)

    def test_raises_on_backend_fetch_error(self):
        # Same logic as the per-service case: an empty backend list on
        # a service that *should* have backends would look like "all
        # backends drained". Raise so LKG kicks in.
        svc_id = 'svc-aa'
        self._set_responses({
            'local_service_bindings': _Resp(200, {
                'local_service_bindings': [
                    {'service_id': svc_id, 'network_id': NET_ID,
                     'enabled': True}],
            }),
            '/local_services/' + svc_id: _Resp(200, {
                'local_service': {'id': svc_id, 'enabled': True,
                                  'local_ipv4': '1.1.1.1'},
            }),
            'local_service_backends': _Resp(500, 'down'),
        })
        self.assertRaises(registry_client.RegistryFetchError,
                          self.client.desired_state_for_network, NET_ID)

    def test_vips_path_still_works_via_state(self):
        # desired_vips_for_network now derives from desired_state. Make
        # sure the contract holds: a fully-fledged service produces
        # a single /32 VIP set.
        svc_id = 'svc-aa'
        self._set_responses({
            'local_service_bindings': _Resp(200, {
                'local_service_bindings': [
                    {'service_id': svc_id, 'network_id': NET_ID,
                     'enabled': True}],
            }),
            '/local_services/' + svc_id: _Resp(200, {
                'local_service': {'id': svc_id, 'enabled': True,
                                  'local_ipv4': '169.254.99.99'},
            }),
            'local_service_backends': _Resp(200, {
                'local_service_backends': []}),
        })
        self.assertEqual({'169.254.99.99/32'},
                         self.client.desired_vips_for_network(NET_ID))


class TestDesiredStateOptOut(testtools.TestCase):
    """Agent-side composition of opt-out semantics.

    The client mirrors the server's effective-attachment rule:
    every enabled opt-out service applies to every network unless an
    ``enabled=False`` binding row excludes it.

    URL key discrimination in the mock matcher: the implicit-attachment
    query lands on ``/local_services?attachment_policy=opt-out&...``
    while per-service GETs land on ``/local_services/<id>``. The keys
    below are picked so each call routes to its own response — see the
    string-substring matcher in ``_set_responses``.
    """

    def setUp(self):
        super().setUp()
        self.client = registry_client.RegistryClient()
        self.session = mock.Mock()
        self.session.get_endpoint.return_value = 'http://neutron/networking'
        self.client._session = self.session
        self.client._endpoint = 'http://neutron/networking'

    def _set_responses(self, mapping):
        def _get(url, raise_exc=False):
            for key, resp in mapping.items():
                if key in url:
                    return resp
            # Default fallback for URLs the test didn't stage. We
            # return an empty-body 200 (rather than a 404) because
            # ``desired_state_for_network`` always fetches both the
            # binding list and the opt-out catalog; tests that only
            # care about one of those would otherwise need to stage
            # the other just to avoid spurious RegistryFetchError.
            return _Resp(200, {})
        self.session.get.side_effect = _get

    def test_opt_out_no_binding_appears_in_state(self):
        # No bindings at all — the opt-out service is implicit on the
        # network.
        out_id = 'svc-out'
        self._set_responses({
            'local_service_bindings': _Resp(200, {
                'local_service_bindings': []}),
            'attachment_policy=opt-out': _Resp(200, {
                'local_services': [{
                    'id': out_id, 'enabled': True,
                    'attachment_policy': 'opt-out',
                    'local_ipv4': '169.254.55.5'}]}),
            'local_service_backends': _Resp(200, {
                'local_service_backends': []}),
        })
        services = self.client.desired_state_for_network(NET_ID)
        self.assertEqual([out_id], [s['id'] for s in services])

    def test_opt_out_marker_excludes_service(self):
        # ``enabled=False`` binding row functions as the opt-out marker.
        out_id = 'svc-out'
        self._set_responses({
            'local_service_bindings': _Resp(200, {
                'local_service_bindings': [
                    {'service_id': out_id, 'network_id': NET_ID,
                     'enabled': False}]}),
            'attachment_policy=opt-out': _Resp(200, {
                'local_services': [{
                    'id': out_id, 'enabled': True,
                    'attachment_policy': 'opt-out',
                    'local_ipv4': '169.254.55.5'}]}),
        })
        services = self.client.desired_state_for_network(NET_ID)
        self.assertEqual([], services)

    def test_redundant_enabled_binding_no_dup(self):
        # Same service appears in both the explicit fetch (via
        # enabled-binding) and the implicit opt-out list — should land
        # in the result exactly once.
        out_id = 'svc-out'
        self._set_responses({
            'local_service_bindings': _Resp(200, {
                'local_service_bindings': [
                    {'service_id': out_id, 'network_id': NET_ID,
                     'enabled': True}]}),
            '/local_services/' + out_id: _Resp(200, {
                'local_service': {
                    'id': out_id, 'enabled': True,
                    'attachment_policy': 'opt-out',
                    'local_ipv4': '169.254.55.5'}}),
            'attachment_policy=opt-out': _Resp(200, {
                'local_services': [{
                    'id': out_id, 'enabled': True,
                    'attachment_policy': 'opt-out',
                    'local_ipv4': '169.254.55.5'}]}),
            'local_service_backends': _Resp(200, {
                'local_service_backends': []}),
        })
        services = self.client.desired_state_for_network(NET_ID)
        self.assertEqual(1, len(services))
        self.assertEqual(out_id, services[0]['id'])

    def test_mixed_opt_in_and_opt_out_both_in_state(self):
        in_id, out_id = 'svc-in', 'svc-out'
        self._set_responses({
            'local_service_bindings': _Resp(200, {
                'local_service_bindings': [
                    {'service_id': in_id, 'network_id': NET_ID,
                     'enabled': True}]}),
            '/local_services/' + in_id: _Resp(200, {
                'local_service': {
                    'id': in_id, 'enabled': True,
                    'attachment_policy': 'opt-in',
                    'local_ipv4': '169.254.10.1'}}),
            'attachment_policy=opt-out': _Resp(200, {
                'local_services': [{
                    'id': out_id, 'enabled': True,
                    'attachment_policy': 'opt-out',
                    'local_ipv4': '169.254.20.2'}]}),
            'local_service_backends': _Resp(200, {
                'local_service_backends': []}),
        })
        services = self.client.desired_state_for_network(NET_ID)
        self.assertEqual({in_id, out_id}, {s['id'] for s in services})

    def test_raises_on_opt_out_query_failure(self):
        # An opt-out catalog failure means we can't enumerate implicit
        # attachments. Returning explicit-only would withdraw every
        # implicit-attached service on every network — exactly the
        # failure mode last-known-good caching exists to prevent.
        # Raise; let the caller fall back.
        in_id = 'svc-in'
        self._set_responses({
            'local_service_bindings': _Resp(200, {
                'local_service_bindings': [
                    {'service_id': in_id, 'network_id': NET_ID,
                     'enabled': True}]}),
            '/local_services/' + in_id: _Resp(200, {
                'local_service': {
                    'id': in_id, 'enabled': True,
                    'attachment_policy': 'opt-in',
                    'local_ipv4': '169.254.10.1'}}),
            'attachment_policy=opt-out': _Resp(500, 'down'),
            'local_service_backends': _Resp(200, {
                'local_service_backends': []}),
        })
        self.assertRaises(registry_client.RegistryFetchError,
                          self.client.desired_state_for_network, NET_ID)
