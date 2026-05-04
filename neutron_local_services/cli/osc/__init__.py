"""OSC plugin entry point.

`make_client` is the contract osc-lib's PluginManager calls during
command discovery. We hand back a thin REST helper bound to the
authenticated network endpoint — no Resource/Proxy abstraction layer
yet (see the v1 plan in the repo's README).
"""

from osc_lib import utils

from neutron_local_services.cli import _client


DEFAULT_API_VERSION = '2.0'
API_NAME = 'local_services'
API_VERSION_OPTION = 'os_local_services_api_version'
API_VERSIONS = {'2.0': 'neutron_local_services.cli.osc'}


def make_client(instance):
    network = instance.get_endpoint_for_service_type(
        'network', region_name=instance.region_name,
        interface=instance.interface)
    return _client.Client(session=instance.session, endpoint=network)


def build_option_parser(parser):
    parser.add_argument(
        '--os-local-services-api-version',
        metavar='<local-services-api-version>',
        default=utils.env('OS_LOCAL_SERVICES_API_VERSION',
                          default=DEFAULT_API_VERSION),
        help=('Local-services API version (default '
              f'{DEFAULT_API_VERSION}; env '
              'OS_LOCAL_SERVICES_API_VERSION)'))
    return parser
