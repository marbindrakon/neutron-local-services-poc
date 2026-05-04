"""``openstack local-service-binding`` commands."""

from osc_lib.command import command

from neutron_local_services import constants as lsc
from neutron_local_services.cli import _format


COLLECTION = lsc.COLLECTION_LOCAL_SERVICE_BINDING
RESOURCE = lsc.RESOURCE_LOCAL_SERVICE_BINDING


def _body(args, fields):
    return {f: getattr(args, f) for f in fields
            if getattr(args, f) is not None}


def _add_enable_flags(parser):
    enabled = parser.add_mutually_exclusive_group()
    enabled.add_argument('--enable', dest='enabled', action='store_true',
                         default=None)
    enabled.add_argument('--disable', dest='enabled', action='store_false',
                         default=None)


class CreateLocalServiceBinding(command.ShowOne):
    """Create a binding (attach a service to a tenant network)."""

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument('--service', dest='service_id', required=True,
                            help='Local service UUID')
        parser.add_argument('--network', dest='network_id', required=True,
                            help='Target network UUID')
        _add_enable_flags(parser)
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.local_services
        body = _body(parsed_args, ('service_id', 'network_id', 'enabled'))
        return _format.binding_show(
            client.create(COLLECTION, RESOURCE, body))


class ListLocalServiceBinding(command.Lister):
    """List local service bindings."""

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument('--service', dest='service_id', default=None)
        parser.add_argument('--network', dest='network_id', default=None)
        enabled = parser.add_mutually_exclusive_group()
        enabled.add_argument('--enabled', dest='enabled',
                             action='store_true', default=None)
        enabled.add_argument('--disabled', dest='enabled',
                             action='store_false', default=None)
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.local_services
        filters = _body(parsed_args,
                        ('service_id', 'network_id', 'enabled'))
        return _format.binding_list(client.list(COLLECTION, **filters))


class ShowLocalServiceBinding(command.ShowOne):
    """Show a local service binding."""

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument('binding', help='Binding UUID')
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.local_services
        return _format.binding_show(
            client.show(COLLECTION, RESOURCE, parsed_args.binding))


class SetLocalServiceBinding(command.Command):
    """Update a local service binding (enable/disable only)."""

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument('binding', help='Binding UUID')
        _add_enable_flags(parser)
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.local_services
        body = _body(parsed_args, ('enabled',))
        if not body:
            return
        client.update(COLLECTION, RESOURCE, parsed_args.binding, body)


class DeleteLocalServiceBinding(command.Command):
    """Delete one or more local service bindings."""

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument('binding', nargs='+',
                            help='Binding UUID (repeatable)')
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.local_services
        errors = []
        for _id in parsed_args.binding:
            try:
                client.delete(COLLECTION, RESOURCE, _id)
            except Exception as exc:
                errors.append(f'{_id}: {exc}')
        if errors:
            raise SystemExit(
                f'Failed to delete {len(errors)} of '
                f'{len(parsed_args.binding)} bindings:\n  '
                + '\n  '.join(errors))
