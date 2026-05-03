"""Extend `exposure_plugin` enum to add the LVS plugin.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-02

The architecture shift mid-made LVS the default exposure plugin
(see docs/architecture/overview.md), but the 0001 migration was
already applied with the original `('envoy',)` enum. New stacks would
get the right shape from the constants update, but the lab DB has the
old shape baked in — this migration brings it forward.

The enum values here are HARDCODED ('lvs', 'envoy') so the schema
trajectory stays stable across later renames in the constants module.
The plugin rename (lvs → nat, envoy → proxy) lands as a separate 0003
migration.
"""

from alembic import op
import sqlalchemy as sa


revision = '0002'
down_revision = '0001'


def upgrade():
    op.alter_column(
        'local_services', 'exposure_plugin',
        existing_type=sa.Enum('envoy', name='local_service_exposure_plugin'),
        type_=sa.Enum('lvs', 'envoy',
                      name='local_service_exposure_plugin'),
        existing_nullable=False)
