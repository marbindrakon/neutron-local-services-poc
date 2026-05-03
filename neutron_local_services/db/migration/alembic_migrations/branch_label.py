"""Branch label for our alembic branch.

Subproject migrations are an independent revision chain — Neutron's
neutron-db-manage builds a separate alembic config per subproject
and tracks heads in a separate version table. We do not encode a
cross-project depends_on; ordering is operational (apply core
Neutron schema first).
"""

LOCAL_SERVICES_EXPAND_BRANCH = 'local_services_expand'
LOCAL_SERVICES_CONTRACT_BRANCH = 'local_services_contract'
