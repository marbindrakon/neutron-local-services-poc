"""Unit tests for the REST helper.

Drives the helper against a `requests_mock` fixture rather than
spinning up a real Neutron — fast and dependency-free, matches what
the OSC commands actually do at runtime.
"""

import requests
import requests_mock
import testtools

from neutron_local_services.cli import _client


_ENDPOINT = 'http://neutron.example.test:9696'


def _session():
    # keystoneauth Session would normally inject auth headers; for
    # unit tests we just need something with the requests-style
    # verb methods.
    return requests.Session()


class ClientTestCase(testtools.TestCase):

    def setUp(self):
        super().setUp()
        self.client = _client.Client(_session(), _ENDPOINT)

    def test_list_strips_empty_filters(self):
        with requests_mock.Mocker() as m:
            m.get(f'{_ENDPOINT}/v2.0/local_services',
                  json={'local_services': [{'id': 'a'}]})
            result = self.client.list('local_services',
                                      protocol=None, enabled='')
            self.assertEqual([{'id': 'a'}], result)
            # Empty filters omitted from the URL entirely.
            self.assertNotIn('?', m.last_request.url)

    def test_list_passes_filters(self):
        with requests_mock.Mocker() as m:
            m.get(f'{_ENDPOINT}/v2.0/local_services',
                  json={'local_services': []})
            self.client.list('local_services', protocol='udp',
                             enabled=True)
            qs = m.last_request.qs
            self.assertEqual(['udp'], qs['protocol'])
            self.assertEqual(['true'], qs['enabled'])

    def test_show_404_raises_notfound(self):
        with requests_mock.Mocker() as m:
            m.get(f'{_ENDPOINT}/v2.0/local_services/bogus',
                  status_code=404, json={})
            self.assertRaises(_client.NotFound,
                              self.client.show, 'local_services',
                              'local_service', 'bogus')

    def test_create_wraps_envelope(self):
        with requests_mock.Mocker() as m:
            m.post(f'{_ENDPOINT}/v2.0/local_services',
                   json={'local_service': {'id': 'new', 'name': 'x'}})
            result = self.client.create('local_services',
                                        'local_service',
                                        {'name': 'x'})
            self.assertEqual({'id': 'new', 'name': 'x'}, result)
            self.assertEqual({'local_service': {'name': 'x'}},
                             m.last_request.json())

    def test_find_by_name_falls_back(self):
        with requests_mock.Mocker() as m:
            m.get(f'{_ENDPOINT}/v2.0/local_services/dns', status_code=404,
                  json={})
            m.get(f'{_ENDPOINT}/v2.0/local_services',
                  json={'local_services': [{'id': 'svc-1', 'name': 'dns'}]})
            result = self.client.find_by_name_or_id(
                'local_services', 'local_service', 'dns')
            self.assertEqual('svc-1', result['id'])

    def test_find_by_name_ambiguous(self):
        with requests_mock.Mocker() as m:
            m.get(f'{_ENDPOINT}/v2.0/local_services/dns', status_code=404,
                  json={})
            m.get(f'{_ENDPOINT}/v2.0/local_services',
                  json={'local_services': [
                      {'id': 'a', 'name': 'dns'},
                      {'id': 'b', 'name': 'dns'}]})
            self.assertRaises(_client.LocalServicesClientError,
                              self.client.find_by_name_or_id,
                              'local_services', 'local_service', 'dns')

    def test_delete_404_raises_notfound(self):
        with requests_mock.Mocker() as m:
            m.delete(f'{_ENDPOINT}/v2.0/local_services/x',
                     status_code=404, json={})
            self.assertRaises(_client.NotFound,
                              self.client.delete, 'local_services',
                              'local_service', 'x')
