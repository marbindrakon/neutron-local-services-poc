"""``openstack local-service-backend`` commands."""

from osc_lib.command import command

from neutron_local_services import constants as lsc
from neutron_local_services.cli import _format


COLLECTION = lsc.COLLECTION_LOCAL_SERVICE_BACKEND
RESOURCE = lsc.RESOURCE_LOCAL_SERVICE_BACKEND


_CREATE_FIELDS = (
    'service_id', 'name', 'availability_zone', 'weight',
    'address', 'port',
    'health_check_address', 'health_check_port', 'enabled',
)

_SET_FIELDS = (
    'name', 'availability_zone', 'weight',
    'address', 'port',
    'health_check_address', 'health_check_port', 'enabled',
)


def _body(args, fields):
    return {f: getattr(args, f) for f in fields
            if getattr(args, f) is not None}


def _add_enable_flags(parser):
    enabled = parser.add_mutually_exclusive_group()
    enabled.add_argument('--enable', dest='enabled', action='store_true',
                         default=None)
    enabled.add_argument('--disable', dest='enabled', action='store_false',
                         default=None)


class CreateLocalServiceBackend(command.ShowOne):
    """Create a local service backend."""

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument('--service', dest='service_id', required=True,
                            help='Local service UUID (immutable)')
        parser.add_argument('--address', required=True,
                            help='Backend reachable address (host netns)')
        parser.add_argument('--port', required=True, type=int)
        parser.add_argument('--name', default=None)
        parser.add_argument('--availability-zone',
                            dest='availability_zone', default=None)
        parser.add_argument('--weight', type=int, default=None)
        parser.add_argument('--health-check-address',
                            dest='health_check_address', default=None)
        parser.add_argument('--health-check-port',
                            dest='health_check_port', type=int,
                            default=None)
        _add_enable_flags(parser)
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.local_services
        body = _body(parsed_args, _CREATE_FIELDS)
        return _format.backend_show(
            client.create(COLLECTION, RESOURCE, body))


class ListLocalServiceBackend(command.Lister):
    """List local service backends."""

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument('--service', dest='service_id', default=None,
                            help='Filter by service UUID')
        enabled = parser.add_mutually_exclusive_group()
        enabled.add_argument('--enabled', dest='enabled',
                             action='store_true', default=None)
        enabled.add_argument('--disabled', dest='enabled',
                             action='store_false', default=None)
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.local_services
        filters = _body(parsed_args, ('service_id', 'enabled'))
        return _format.backend_list(client.list(COLLECTION, **filters))


class ShowLocalServiceBackend(command.ShowOne):
    """Show a local service backend."""

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument('backend', help='Backend UUID')
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.local_services
        return _format.backend_show(
            client.show(COLLECTION, RESOURCE, parsed_args.backend))


class SetLocalServiceBackend(command.Command):
    """Update a local service backend.

    `service_id` is immutable; not exposed.
    """

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument('backend', help='Backend UUID')
        parser.add_argument('--name', default=None)
        parser.add_argument('--availability-zone',
                            dest='availability_zone', default=None)
        parser.add_argument('--weight', type=int, default=None)
        parser.add_argument('--address', default=None)
        parser.add_argument('--port', type=int, default=None)
        parser.add_argument('--health-check-address',
                            dest='health_check_address', default=None)
        parser.add_argument('--health-check-port',
                            dest='health_check_port', type=int,
                            default=None)
        _add_enable_flags(parser)
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.local_services
        body = _body(parsed_args, _SET_FIELDS)
        if not body:
            return
        client.update(COLLECTION, RESOURCE, parsed_args.backend, body)


class DeleteLocalServiceBackend(command.Command):
    """Delete one or more local service backends."""

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument('backend', nargs='+',
                            help='Backend UUID (repeatable)')
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.local_services
        errors = []
        for _id in parsed_args.backend:
            try:
                client.delete(COLLECTION, RESOURCE, _id)
            except Exception as exc:
                errors.append(f'{_id}: {exc}')
        if errors:
            raise SystemExit(
                f'Failed to delete {len(errors)} of '
                f'{len(parsed_args.backend)} backends:\n  '
                + '\n  '.join(errors))
