"""Column ordering and value formatting for OSC output.

Single source of truth so Show and List don't drift. cliff's
ShowOne/Lister both want ``(columns, data)`` tuples, so these
helpers return paired sequences.
"""

import json


_LOCAL_SERVICE_COLUMNS = (
    'id', 'name', 'description', 'project_id',
    'local_ipv4', 'local_ipv6', 'protocol', 'port',
    'attachment_policy', 'distribution_policy', 'exposure_plugin',
    'health_check_type', 'health_check_config',
    'enabled',
)

_LOCAL_SERVICE_BACKEND_COLUMNS = (
    'id', 'service_id', 'name', 'availability_zone', 'weight',
    'address', 'port',
    'health_check_address', 'health_check_port',
    'enabled',
)

_LOCAL_SERVICE_BINDING_COLUMNS = (
    'id', 'project_id', 'service_id', 'network_id', 'enabled',
)


def _fmt(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return value


def _row(columns, obj):
    return tuple(_fmt(obj.get(c)) for c in columns)


def service_show(obj):
    return _LOCAL_SERVICE_COLUMNS, _row(_LOCAL_SERVICE_COLUMNS, obj)


def service_list(objs):
    return (_LOCAL_SERVICE_COLUMNS,
            [_row(_LOCAL_SERVICE_COLUMNS, o) for o in objs])


def backend_show(obj):
    return (_LOCAL_SERVICE_BACKEND_COLUMNS,
            _row(_LOCAL_SERVICE_BACKEND_COLUMNS, obj))


def backend_list(objs):
    return (_LOCAL_SERVICE_BACKEND_COLUMNS,
            [_row(_LOCAL_SERVICE_BACKEND_COLUMNS, o) for o in objs])


def binding_show(obj):
    return (_LOCAL_SERVICE_BINDING_COLUMNS,
            _row(_LOCAL_SERVICE_BINDING_COLUMNS, obj))


def binding_list(objs):
    return (_LOCAL_SERVICE_BINDING_COLUMNS,
            [_row(_LOCAL_SERVICE_BINDING_COLUMNS, o) for o in objs])
