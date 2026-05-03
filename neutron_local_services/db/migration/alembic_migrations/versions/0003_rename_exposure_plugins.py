"""Rename exposure_plugin enum values: lvs -> nat, envoy -> proxy.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-03

Swaps the old (lvs, envoy) plugin names for operator-facing
(nat, proxy) so the exposure_plugin choice reads as "throughput vs
HC fidelity" instead of as an implementation. The migration backfills
existing rows in place via a widen -> UPDATE -> narrow sequence so
re-stacks against a partly-populated DB don't trip MySQL's
truncating column-type change.
"""

from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'


def upgrade():
    bind = op.get_bind()

    # 1) Widen the enum so it can hold both the old and new value sets
    # at once. This lets us run UPDATE without MySQL truncating any
    # row whose value isn't in the target type.
    op.alter_column(
        'local_services', 'exposure_plugin',
        existing_type=sa.Enum('lvs', 'envoy',
                              name='local_service_exposure_plugin'),
        type_=sa.Enum('lvs', 'envoy', 'nat', 'proxy',
                      name='local_service_exposure_plugin'),
        existing_nullable=False)

    # 2) Rewrite the data: lvs -> nat, envoy -> proxy.
    bind.execute(sa.text(
        "UPDATE local_services SET exposure_plugin = 'nat' "
        "WHERE exposure_plugin = 'lvs'"))
    bind.execute(sa.text(
        "UPDATE local_services SET exposure_plugin = 'proxy' "
        "WHERE exposure_plugin = 'envoy'"))

    # 3) Narrow the enum to just the new values now that no row holds
    # an old value.
    op.alter_column(
        'local_services', 'exposure_plugin',
        existing_type=sa.Enum('lvs', 'envoy', 'nat', 'proxy',
                              name='local_service_exposure_plugin'),
        type_=sa.Enum('nat', 'proxy',
                      name='local_service_exposure_plugin'),
        existing_nullable=False)
