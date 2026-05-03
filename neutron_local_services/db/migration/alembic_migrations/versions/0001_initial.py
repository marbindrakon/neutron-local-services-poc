"""Initial schema for neutron-local-services.

Revision ID: 0001
Revises: -
Create Date: 2026-05-02

Note: this is a Neutron *subproject* alembic chain. Neutron's
neutron-db-manage runs each subproject through its own alembic
config and tracks heads in a separate version table — there is no
cross-project depends_on. The ordering "apply Neutron schema
before this one" is operational: neutron-db-manage upgrade head
must be run for the neutron core before our subproject so the
referenced standardattributes / networks tables exist.
"""

from alembic import op
import sqlalchemy as sa

from neutron_local_services import constants as lsc
from neutron_local_services.db.migration.alembic_migrations import (
    branch_label)


revision = '0001'
down_revision = None
branch_labels = (branch_label.LOCAL_SERVICES_EXPAND_BRANCH,)


def upgrade():
    op.create_table(
        'local_services',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('project_id', sa.String(255), nullable=True, index=True),
        sa.Column('standard_attr_id', sa.BigInteger,
                  sa.ForeignKey('standardattributes.id', ondelete='CASCADE'),
                  nullable=False, unique=True),
        sa.Column('name', sa.String(64), nullable=False),
        sa.Column('description', sa.String(255), nullable=True),
        sa.Column('local_ipv4', sa.String(64), nullable=True),
        sa.Column('local_ipv6', sa.String(64), nullable=True),
        sa.Column('protocol',
                  sa.Enum(*lsc.PROTOCOLS, name='local_service_protocol'),
                  nullable=False),
        sa.Column('port', sa.Integer, nullable=False),
        sa.Column('attachment_policy',
                  sa.Enum(*lsc.ATTACH_POLICIES,
                          name='local_service_attachment_policy'),
                  nullable=False),
        sa.Column('distribution_policy',
                  sa.Enum(*lsc.DISTRIBUTION_POLICIES,
                          name='local_service_distribution_policy'),
                  nullable=False),
        sa.Column('exposure_plugin',
                  sa.Enum(*lsc.EXPOSURE_PLUGINS,
                          name='local_service_exposure_plugin'),
                  nullable=False),
        sa.Column('health_check_type',
                  sa.Enum(*lsc.HC_TYPES, name='local_service_hc_type'),
                  nullable=False),
        sa.Column('health_check_config', sa.Text, nullable=True),
        sa.Column('enabled', sa.Boolean, nullable=False,
                  server_default=sa.true()),
        sa.UniqueConstraint('project_id', 'name',
                            name='uniq_local_services_project_name'),
        sa.CheckConstraint('port > 0 AND port <= 65535',
                           name='ck_local_services_port_range'),
    )

    op.create_table(
        'local_service_backends',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('standard_attr_id', sa.BigInteger,
                  sa.ForeignKey('standardattributes.id', ondelete='CASCADE'),
                  nullable=False, unique=True),
        sa.Column('service_id', sa.String(36),
                  sa.ForeignKey('local_services.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('name', sa.String(64), nullable=False),
        sa.Column('availability_zone', sa.String(255), nullable=True),
        sa.Column('weight', sa.Integer, nullable=True),
        sa.Column('address', sa.String(64), nullable=False),
        sa.Column('port', sa.Integer, nullable=False),
        sa.Column('health_check_address', sa.String(64), nullable=True),
        sa.Column('health_check_port', sa.Integer, nullable=True),
        sa.Column('enabled', sa.Boolean, nullable=False,
                  server_default=sa.true()),
        sa.UniqueConstraint('service_id', 'name',
                            name='uniq_local_service_backends_svc_name'),
        sa.CheckConstraint('port > 0 AND port <= 65535',
                           name='ck_local_service_backends_port_range'),
    )

    op.create_table(
        'local_service_bindings',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('project_id', sa.String(255), nullable=True, index=True),
        sa.Column('standard_attr_id', sa.BigInteger,
                  sa.ForeignKey('standardattributes.id', ondelete='CASCADE'),
                  nullable=False, unique=True),
        sa.Column('service_id', sa.String(36),
                  sa.ForeignKey('local_services.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('network_id', sa.String(36),
                  sa.ForeignKey('networks.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('enabled', sa.Boolean, nullable=False,
                  server_default=sa.true()),
        sa.UniqueConstraint('service_id', 'network_id',
                            name='uniq_local_service_bindings_svc_net'),
    )
