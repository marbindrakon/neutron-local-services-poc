from alembic import context
from neutron_lib.db import model_base
import sqlalchemy as sa
from sqlalchemy import event  # noqa: N346

from neutron.db.migration.connection import DBConnection

from neutron_local_services.db.migration import alembic_migrations


MYSQL_ENGINE = None

config = context.config
neutron_config = config.neutron_config  # type:ignore[attr-defined]
target_metadata = model_base.BASEV2.metadata


def set_mysql_engine():
    global MYSQL_ENGINE
    MYSQL_ENGINE = model_base.BASEV2.__table_args__['mysql_engine']


def run_migrations_offline():
    set_mysql_engine()
    kwargs = {'version_table': alembic_migrations.LOCAL_SERVICES_VERSION_TABLE}
    if neutron_config.database.connection:
        kwargs['url'] = neutron_config.database.connection
    else:
        kwargs['dialect_name'] = neutron_config.database.engine
    context.configure(**kwargs)
    with context.begin_transaction():
        context.run_migrations()


@event.listens_for(sa.Table, 'after_parent_attach')
def set_storage_engine(target, parent):
    if MYSQL_ENGINE:
        target.kwargs['mysql_engine'] = MYSQL_ENGINE


def run_migrations_online():
    set_mysql_engine()
    connection = config.attributes.get('connection')
    with DBConnection(neutron_config.database.connection, connection) as conn:
        context.configure(
            connection=conn,
            target_metadata=target_metadata,
            version_table=alembic_migrations.LOCAL_SERVICES_VERSION_TABLE,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
