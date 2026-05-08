"""Unit tests for the proxy exposure plugin's HC translation.

The proxy plugin emits HMAC-signed catalog entries the Rust worker
consumes. The catalog wire intentionally has a closed set of native
HC variants — there is no path-execution variant — so the API-side
HC types must all map to one of {tcp_connect, http_get,
https_handshake, udp_dns_query, udp_ntp_query}.
"""

import testtools

from neutron_local_services import constants as lsc
from neutron_local_services.agent.plugins import proxy as proxy_plugin


def _svc(**overrides):
    base = {
        'id': 'svc-aaaa',
        'name': 'dns',
        'local_ipv4': '169.254.169.5',
        'port': 53,
        'protocol': lsc.PROTO_UDP,
        'distribution_policy': lsc.DIST_ROUND_ROBIN,
        'health_check_type': lsc.HC_NONE,
        'enabled': True,
        'backends': [],
    }
    base.update(overrides)
    return base


class HcForServiceTests(testtools.TestCase):

    def test_none_defaults_to_tcp_connect(self):
        self.assertEqual(
            {'type': 'tcp_connect'},
            proxy_plugin._hc_for_service(_svc(health_check_type=lsc.HC_NONE)))

    def test_tcp(self):
        self.assertEqual(
            {'type': 'tcp_connect'},
            proxy_plugin._hc_for_service(_svc(health_check_type=lsc.HC_TCP)))

    def test_http(self):
        self.assertEqual(
            {'type': 'http_get', 'path': '/'},
            proxy_plugin._hc_for_service(_svc(health_check_type=lsc.HC_HTTP)))

    def test_https(self):
        self.assertEqual(
            {'type': 'https_handshake'},
            proxy_plugin._hc_for_service(_svc(health_check_type=lsc.HC_HTTPS)))

    def test_dns_uses_native_udp_dns_query(self):
        self.assertEqual(
            {'type': 'udp_dns_query'},
            proxy_plugin._hc_for_service(_svc(health_check_type=lsc.HC_DNS)))

    def test_ntp_uses_native_udp_ntp_query(self):
        self.assertEqual(
            {'type': 'udp_ntp_query'},
            proxy_plugin._hc_for_service(_svc(health_check_type=lsc.HC_NTP)))

    def test_no_script_variant_emitted_for_any_api_hc_type(self):
        # The wire intentionally has no path-execution variant. Every
        # API-accepted HC type must round-trip to a non-script payload.
        for hc_type in lsc.HC_TYPES:
            hc = proxy_plugin._hc_for_service(
                _svc(health_check_type=hc_type))
            self.assertNotIn('path', {k: hc[k] for k in hc if k == 'path'}
                             if hc.get('type') != 'http_get' else {})
            self.assertNotEqual('script', hc.get('type'))
