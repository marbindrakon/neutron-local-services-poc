"""Unit tests for the OSC command classes.

We exercise the cliff Command classes via their argparse parsers and
a stub client_manager — no live Neutron, no full OSC `App`. The goal
is to lock in argument shapes (immutability, mutual exclusion,
required flags) and that the body sent to the REST layer is what the
operator typed.
"""

from unittest import mock

import testtools

from neutron_local_services.cli.osc import backend
from neutron_local_services.cli.osc import binding
from neutron_local_services.cli.osc import service


class _FakeApp:
    def __init__(self, client):
        self.client_manager = mock.Mock()
        self.client_manager.local_services = client


def _run(cls, argv, client=None):
    client = client or mock.Mock()
    cmd = cls(_FakeApp(client), None)
    parser = cmd.get_parser('test')
    parsed = parser.parse_args(argv)
    result = cmd.take_action(parsed)
    return parsed, result, client


class ServiceCommandTestCase(testtools.TestCase):

    def test_create_minimum_requires_an_ip(self):
        with testtools.ExpectedException(SystemExit):
            _run(service.CreateLocalService,
                 ['dns', '--protocol', 'udp', '--port', '53'])

    def test_create_builds_expected_body(self):
        client = mock.Mock()
        client.create.return_value = {
            'id': '1', 'name': 'dns', 'protocol': 'udp', 'port': 53,
        }
        _run(service.CreateLocalService,
             ['dns', '--protocol', 'udp', '--port', '53',
              '--local-ipv4', '169.254.10.53', '--enable'],
             client=client)
        client.create.assert_called_once_with(
            'local_services', 'local_service',
            {'name': 'dns', 'protocol': 'udp', 'port': 53,
             'local_ipv4': '169.254.10.53', 'enabled': True})

    def test_set_omits_immutable_fields(self):
        cmd = service.SetLocalService(_FakeApp(mock.Mock()), None)
        parser = cmd.get_parser('test')
        with testtools.ExpectedException(SystemExit):
            parser.parse_args(['svc', '--protocol', 'tcp'])
        with testtools.ExpectedException(SystemExit):
            parser.parse_args(['svc', '--port', '53'])
        with testtools.ExpectedException(SystemExit):
            parser.parse_args(['svc', '--exposure-plugin', 'proxy'])

    def test_set_no_changes_skips_call(self):
        client = mock.Mock()
        client.find_by_name_or_id.return_value = {'id': 'svc-1'}
        _run(service.SetLocalService, ['svc'], client=client)
        client.update.assert_not_called()

    def test_set_passes_only_provided_fields(self):
        client = mock.Mock()
        client.find_by_name_or_id.return_value = {'id': 'svc-1'}
        _run(service.SetLocalService,
             ['svc', '--description', 'new', '--disable'],
             client=client)
        client.update.assert_called_once_with(
            'local_services', 'local_service', 'svc-1',
            {'description': 'new', 'enabled': False})

    def test_list_filters(self):
        client = mock.Mock()
        client.list.return_value = []
        _run(service.ListLocalService,
             ['--protocol', 'tcp', '--disabled'], client=client)
        client.list.assert_called_once_with('local_services',
                                            protocol='tcp', enabled=False)

    def test_delete_aggregates_errors(self):
        client = mock.Mock()
        client.find_by_name_or_id.side_effect = [
            {'id': 'a'}, Exception('boom')]
        with testtools.ExpectedException(SystemExit):
            _run(service.DeleteLocalService,
                 ['svc-a', 'svc-b'], client=client)
        client.delete.assert_called_once_with(
            'local_services', 'local_service', 'a')


class BackendCommandTestCase(testtools.TestCase):

    def test_create_requires_service_address_port(self):
        cmd = backend.CreateLocalServiceBackend(
            _FakeApp(mock.Mock()), None)
        parser = cmd.get_parser('test')
        with testtools.ExpectedException(SystemExit):
            parser.parse_args(['--address', '1.2.3.4', '--port', '53'])
        with testtools.ExpectedException(SystemExit):
            parser.parse_args(['--service', 's', '--port', '53'])
        with testtools.ExpectedException(SystemExit):
            parser.parse_args(['--service', 's', '--address', '1.2.3.4'])

    def test_create_body(self):
        client = mock.Mock()
        client.create.return_value = {'id': 'b1'}
        _run(backend.CreateLocalServiceBackend,
             ['--service', 'svc', '--address', '1.2.3.4',
              '--port', '53', '--weight', '5'], client=client)
        client.create.assert_called_once_with(
            'local_service_backends', 'local_service_backend',
            {'service_id': 'svc', 'address': '1.2.3.4',
             'port': 53, 'weight': 5})

    def test_set_no_service_id_flag(self):
        cmd = backend.SetLocalServiceBackend(_FakeApp(mock.Mock()), None)
        parser = cmd.get_parser('test')
        with testtools.ExpectedException(SystemExit):
            parser.parse_args(['b1', '--service', 'other'])


class BindingCommandTestCase(testtools.TestCase):

    def test_create_requires_service_and_network(self):
        cmd = binding.CreateLocalServiceBinding(
            _FakeApp(mock.Mock()), None)
        parser = cmd.get_parser('test')
        with testtools.ExpectedException(SystemExit):
            parser.parse_args(['--service', 'svc'])
        with testtools.ExpectedException(SystemExit):
            parser.parse_args(['--network', 'net'])

    def test_set_only_enabled(self):
        cmd = binding.SetLocalServiceBinding(_FakeApp(mock.Mock()), None)
        parser = cmd.get_parser('test')
        # No --service / --network on set; binding is immutable
        # except for enabled.
        with testtools.ExpectedException(SystemExit):
            parser.parse_args(['b1', '--service', 'svc'])
        with testtools.ExpectedException(SystemExit):
            parser.parse_args(['b1', '--network', 'net'])

    def test_set_no_changes_skips_call(self):
        client = mock.Mock()
        _run(binding.SetLocalServiceBinding, ['b1'], client=client)
        client.update.assert_not_called()

    def test_set_enabled(self):
        client = mock.Mock()
        _run(binding.SetLocalServiceBinding,
             ['b1', '--enable'], client=client)
        client.update.assert_called_once_with(
            'local_service_bindings', 'local_service_binding', 'b1',
            {'enabled': True})
