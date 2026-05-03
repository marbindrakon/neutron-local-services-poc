"""API definition for the local-services extension.

Three resources:

* `local_services`            — service catalog (admin-managed)
* `local_service_backends`    — pool of backends per service (admin)
* `local_service_bindings`    — service-to-network attachment with
                                 tenant opt-in/out (RBAC controlled)

The shape mirrors `neutron_lib.api.definitions.local_ip` for layout
consistency. Validation is the cheap kind here; deeper checks belong
in the plugin.
"""

from neutron_lib import constants as nl_const
from neutron_lib.db import constants as db_const

from neutron_local_services import constants as lsc


ALIAS = lsc.ALIAS
IS_SHIM_EXTENSION = False
IS_STANDARD_ATTR_EXTENSION = True
NAME = 'Local Services'
DESCRIPTION = (
    'Operator-defined network services exposed to tenant networks via '
    'link-local VIPs without requiring routed connectivity.')
UPDATED_TIMESTAMP = '2026-05-02T00:00:00-00:00'

LOCAL_SERVICE = lsc.RESOURCE_LOCAL_SERVICE
LOCAL_SERVICES = lsc.COLLECTION_LOCAL_SERVICE
LOCAL_SERVICE_BACKEND = lsc.RESOURCE_LOCAL_SERVICE_BACKEND
LOCAL_SERVICE_BACKENDS = lsc.COLLECTION_LOCAL_SERVICE_BACKEND
LOCAL_SERVICE_BINDING = lsc.RESOURCE_LOCAL_SERVICE_BINDING
LOCAL_SERVICE_BINDINGS = lsc.COLLECTION_LOCAL_SERVICE_BINDING


_PORT = {
    'allow_post': True,
    'allow_put': True,
    'convert_to': int,
    'validate': {'type:range': [1, 65535]},
    'is_filter': True,
    'is_sort_key': True,
    'is_visible': True,
}

_NAME = {
    'allow_post': True,
    'allow_put': True,
    'default': '',
    'validate': {'type:string': db_const.NAME_FIELD_SIZE},
    'is_filter': True,
    'is_sort_key': True,
    'is_visible': True,
}

_DESCRIPTION = {
    'allow_post': True,
    'allow_put': True,
    'default': '',
    'validate': {'type:string': db_const.LONG_DESCRIPTION_FIELD_SIZE},
    'is_visible': True,
}

_PROJECT_ID = {
    'allow_post': True,
    'allow_put': False,
    'validate': {'type:string': db_const.PROJECT_ID_FIELD_SIZE},
    'required_by_policy': True,
    'is_filter': True,
    'is_sort_key': True,
    'is_visible': True,
}

_ID = {
    'allow_post': False,
    'allow_put': False,
    'validate': {'type:uuid': None},
    'is_filter': True,
    'is_sort_key': True,
    'is_visible': True,
    'primary_key': True,
}


RESOURCE_ATTRIBUTE_MAP = {
    LOCAL_SERVICES: {
        'id': _ID,
        'project_id': _PROJECT_ID,
        'name': _NAME,
        'description': _DESCRIPTION,
        'local_ipv4': {
            'allow_post': True, 'allow_put': True,
            'default': None,
            'validate': {'type:ip_address_or_none': None},
            'is_filter': True,
            'is_visible': True,
        },
        'local_ipv6': {
            'allow_post': True, 'allow_put': True,
            'default': None,
            'validate': {'type:ip_address_or_none': None},
            'is_filter': True,
            'is_visible': True,
        },
        'protocol': {
            'allow_post': True, 'allow_put': False,
            'validate': {'type:values': list(lsc.PROTOCOLS)},
            'is_filter': True, 'is_visible': True,
        },
        'port': dict(_PORT, allow_put=False),
        'attachment_policy': {
            'allow_post': True, 'allow_put': True,
            'default': lsc.ATTACH_OPT_IN,
            'validate': {'type:values': list(lsc.ATTACH_POLICIES)},
            'is_filter': True, 'is_visible': True,
        },
        'distribution_policy': {
            'allow_post': True, 'allow_put': True,
            'default': lsc.DIST_ROUND_ROBIN,
            'validate': {'type:values': list(lsc.DISTRIBUTION_POLICIES)},
            'is_visible': True,
        },
        'exposure_plugin': {
            'allow_post': True, 'allow_put': False,
            'default': lsc.EXPOSURE_NAT,
            'validate': {'type:values': list(lsc.EXPOSURE_PLUGINS)},
            'is_filter': True, 'is_visible': True,
        },
        'health_check_type': {
            'allow_post': True, 'allow_put': True,
            'default': lsc.HC_NONE,
            'validate': {'type:values': list(lsc.HC_TYPES)},
            'is_visible': True,
        },
        'health_check_config': {
            'allow_post': True, 'allow_put': True,
            'default': None,
            'validate': {'type:string_or_none':
                         db_const.LONG_DESCRIPTION_FIELD_SIZE * 4},
            'is_visible': True,
        },
        'enabled': {
            'allow_post': True, 'allow_put': True,
            'default': True, 'convert_to': lambda v: bool(v),
            'is_filter': True, 'is_visible': True,
        },
    },
    LOCAL_SERVICE_BACKENDS: {
        'id': _ID,
        'service_id': {
            'allow_post': True, 'allow_put': False,
            'validate': {'type:uuid': None},
            'is_filter': True, 'is_visible': True,
        },
        'name': _NAME,
        'availability_zone': {
            'allow_post': True, 'allow_put': True,
            'default': None,
            'validate': {'type:string_or_none': db_const.NAME_FIELD_SIZE},
            'is_filter': True, 'is_visible': True,
        },
        'weight': {
            'allow_post': True, 'allow_put': True,
            'default': nl_const.ATTR_NOT_SPECIFIED,
            'convert_to': lambda v: int(v) if v is not None else None,
            'is_visible': True,
        },
        'address': {
            'allow_post': True, 'allow_put': True,
            'validate': {'type:ip_address': None},
            'is_filter': True, 'is_visible': True,
        },
        'port': _PORT,
        'health_check_address': {
            'allow_post': True, 'allow_put': True,
            'default': None,
            'validate': {'type:ip_address_or_none': None},
            'is_visible': True,
        },
        'health_check_port': {
            'allow_post': True, 'allow_put': True,
            'default': None,
            'convert_to': lambda v: int(v) if v is not None else None,
            'is_visible': True,
        },
        'enabled': {
            'allow_post': True, 'allow_put': True,
            'default': True, 'convert_to': lambda v: bool(v),
            'is_filter': True, 'is_visible': True,
        },
    },
    LOCAL_SERVICE_BINDINGS: {
        'id': _ID,
        'project_id': _PROJECT_ID,
        'service_id': {
            'allow_post': True, 'allow_put': False,
            'validate': {'type:uuid': None},
            'is_filter': True, 'is_visible': True,
        },
        'network_id': {
            'allow_post': True, 'allow_put': False,
            'validate': {'type:uuid': None},
            'is_filter': True, 'is_visible': True,
        },
        'enabled': {
            'allow_post': True, 'allow_put': True,
            'default': True, 'convert_to': lambda v: bool(v),
            'is_filter': True, 'is_visible': True,
        },
    },
}

SUB_RESOURCE_ATTRIBUTE_MAP = {}
ACTION_MAP = {}
ACTION_STATUS = {}
REQUIRED_EXTENSIONS = []
OPTIONAL_EXTENSIONS = []
