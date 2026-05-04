"""``openstack local-service`` commands."""

from osc_lib.command import command

from neutron_local_services import constants as lsc
from neutron_local_services.cli import _format


COLLECTION = lsc.COLLECTION_LOCAL_SERVICE
RESOURCE = lsc.RESOURCE_LOCAL_SERVICE


def _add_create_args(parser):
    parser.add_argument('name', help='Service name')
    parser.add_argument('--description', default=None)
    parser.add_argument('--local-ipv4', dest='local_ipv4', default=None,
                        help='Link-local VIP (at least one of v4/v6 '
                             'required)')
    parser.add_argument('--local-ipv6', dest='local_ipv6', default=None)
    parser.add_argument('--protocol', required=True,
                        choices=list(lsc.PROTOCOLS))
    parser.add_argument('--port', required=True, type=int)
    parser.add_argument('--attachment-policy', dest='attachment_policy',
                        choices=list(lsc.ATTACH_POLICIES),
                        default=None)
    parser.add_argument('--distribution-policy',
                        dest='distribution_policy',
                        choices=list(lsc.DISTRIBUTION_POLICIES),
                        default=None)
    parser.add_argument('--exposure-plugin', dest='exposure_plugin',
                        choices=list(lsc.EXPOSURE_PLUGINS),
                        default=None,
                        help='Immutable after create')
    parser.add_argument('--health-check-type',
                        dest='health_check_type',
                        choices=list(lsc.HC_TYPES), default=None)
    parser.add_argument('--health-check-config',
                        dest='health_check_config', default=None,
                        help='Plugin-specific JSON blob (passed verbatim)')
    enabled = parser.add_mutually_exclusive_group()
    enabled.add_argument('--enable', dest='enabled', action='store_true',
                         default=None)
    enabled.add_argument('--disable', dest='enabled', action='store_false',
                         default=None)


def _add_set_args(parser):
    parser.add_argument('service', help='Service name or ID')
    parser.add_argument('--name', default=None)
    parser.add_argument('--description', default=None)
    parser.add_argument('--local-ipv4', dest='local_ipv4', default=None)
    parser.add_argument('--local-ipv6', dest='local_ipv6', default=None)
    parser.add_argument('--attachment-policy', dest='attachment_policy',
                        choices=list(lsc.ATTACH_POLICIES), default=None)
    parser.add_argument('--distribution-policy',
                        dest='distribution_policy',
                        choices=list(lsc.DISTRIBUTION_POLICIES),
                        default=None)
    parser.add_argument('--health-check-type',
                        dest='health_check_type',
                        choices=list(lsc.HC_TYPES), default=None)
    parser.add_argument('--health-check-config',
                        dest='health_check_config', default=None)
    enabled = parser.add_mutually_exclusive_group()
    enabled.add_argument('--enable', dest='enabled', action='store_true',
                         default=None)
    enabled.add_argument('--disable', dest='enabled', action='store_false',
                         default=None)


_CREATE_BODY_FIELDS = (
    'name', 'description', 'local_ipv4', 'local_ipv6',
    'protocol', 'port', 'attachment_policy', 'distribution_policy',
    'exposure_plugin', 'health_check_type', 'health_check_config',
    'enabled',
)

_SET_BODY_FIELDS = (
    'name', 'description', 'local_ipv4', 'local_ipv6',
    'attachment_policy', 'distribution_policy',
    'health_check_type', 'health_check_config', 'enabled',
)


def _body(args, fields):
    return {f: getattr(args, f) for f in fields
            if getattr(args, f) is not None}


class CreateLocalService(command.ShowOne):
    """Create a local service."""

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        _add_create_args(parser)
        return parser

    def take_action(self, parsed_args):
        if not (parsed_args.local_ipv4 or parsed_args.local_ipv6):
            raise SystemExit(
                'At least one of --local-ipv4 / --local-ipv6 is required')
        client = self.app.client_manager.local_services
        body = _body(parsed_args, _CREATE_BODY_FIELDS)
        return _format.service_show(
            client.create(COLLECTION, RESOURCE, body))


class ListLocalService(command.Lister):
    """List local services."""

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument('--protocol', choices=list(lsc.PROTOCOLS),
                            default=None)
        parser.add_argument('--exposure-plugin',
                            dest='exposure_plugin',
                            choices=list(lsc.EXPOSURE_PLUGINS),
                            default=None)
        enabled = parser.add_mutually_exclusive_group()
        enabled.add_argument('--enabled', dest='enabled',
                             action='store_true', default=None)
        enabled.add_argument('--disabled', dest='enabled',
                             action='store_false', default=None)
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.local_services
        filters = _body(parsed_args,
                        ('protocol', 'exposure_plugin', 'enabled'))
        return _format.service_list(client.list(COLLECTION, **filters))


class ShowLocalService(command.ShowOne):
    """Show a local service."""

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument('service', help='Service name or ID')
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.local_services
        return _format.service_show(
            client.find_by_name_or_id(COLLECTION, RESOURCE,
                                      parsed_args.service))


class SetLocalService(command.Command):
    """Update a local service.

    `protocol`, `port`, and `exposure_plugin` are immutable after
    create — the API will reject any attempt to change them, so this
    command does not expose flags for them.
    """

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        _add_set_args(parser)
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.local_services
        existing = client.find_by_name_or_id(
            COLLECTION, RESOURCE, parsed_args.service)
        body = _body(parsed_args, _SET_BODY_FIELDS)
        if not body:
            return
        client.update(COLLECTION, RESOURCE, existing['id'], body)


class DeleteLocalService(command.Command):
    """Delete one or more local services."""

    def get_parser(self, prog_name):
        parser = super().get_parser(prog_name)
        parser.add_argument('service', nargs='+',
                            help='Service name or ID (repeatable)')
        return parser

    def take_action(self, parsed_args):
        client = self.app.client_manager.local_services
        errors = []
        for ref in parsed_args.service:
            try:
                obj = client.find_by_name_or_id(COLLECTION, RESOURCE, ref)
                client.delete(COLLECTION, RESOURCE, obj['id'])
            except Exception as exc:
                errors.append(f'{ref}: {exc}')
        if errors:
            raise SystemExit(
                f'Failed to delete {len(errors)} of '
                f'{len(parsed_args.service)} services:\n  '
                + '\n  '.join(errors))
