"""oslo.policy defaults for the local-services plugin.

Default posture: admin-only for every resource action. The catalog
exposes operator infra (VIPs, backend addresses, AZs) that tenants
must not enumerate. A tenant-safe read API distinct from this
operator catalog is a productization item — see docs/limitations.md
§3 for the RBAC depth that needs to land first.
"""

from oslo_policy import policy

from neutron_local_services import constants as lsc


_ADMIN = 'rule:admin_only'

_RULES = []

for resource in (lsc.RESOURCE_LOCAL_SERVICE,
                 lsc.RESOURCE_LOCAL_SERVICE_BACKEND,
                 lsc.RESOURCE_LOCAL_SERVICE_BINDING):
    _RULES.extend([
        policy.DocumentedRuleDefault(
            name=f'create_{resource}',
            check_str=_ADMIN,
            description=f'Create a {resource}',
            operations=[{'method': 'POST',
                         'path': f'/{resource.replace("_", "-")}s'}]),
        policy.DocumentedRuleDefault(
            name=f'update_{resource}',
            check_str=_ADMIN,
            description=f'Update a {resource}',
            operations=[{'method': 'PUT',
                         'path': f'/{resource.replace("_", "-")}s/'
                                 '{id}'}]),
        policy.DocumentedRuleDefault(
            name=f'delete_{resource}',
            check_str=_ADMIN,
            description=f'Delete a {resource}',
            operations=[{'method': 'DELETE',
                         'path': f'/{resource.replace("_", "-")}s/'
                                 '{id}'}]),
        policy.DocumentedRuleDefault(
            name=f'get_{resource}',
            check_str=_ADMIN,
            description=f'Get a {resource}',
            operations=[{'method': 'GET',
                         'path': f'/{resource.replace("_", "-")}s'},
                        {'method': 'GET',
                         'path': f'/{resource.replace("_", "-")}s/'
                                 '{id}'}]),
    ])


def list_rules():
    return _RULES
