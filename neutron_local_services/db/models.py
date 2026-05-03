"""SQLAlchemy ORM models for the local-services service plugin.

Three tables:

* `local_services`            — operator-defined service definitions
* `local_service_backends`    — pool of backends per service
* `local_service_bindings`    — (service, network) pairings; tenant
                                 opt-in/opt-out lives here

The schema is intentionally simple. Most validation happens in the API
layer; constraints here are the ones the DB can enforce cheaply.
"""

import sqlalchemy as sa
from sqlalchemy import orm

from neutron_lib.db import model_base
from neutron_lib.db import standard_attr

from neutron_local_services import constants as lsc


class LocalService(standard_attr.HasStandardAttributes,
                   model_base.BASEV2,
                   model_base.HasId,
                   model_base.HasProject):
    """Operator-defined local service catalog entry."""

    __tablename__ = 'local_services'

    name = sa.Column(sa.String(64), nullable=False)
    description = sa.Column(sa.String(255), nullable=True)

    local_ipv4 = sa.Column(sa.String(64), nullable=True)
    local_ipv6 = sa.Column(sa.String(64), nullable=True)

    protocol = sa.Column(
        sa.Enum(*lsc.PROTOCOLS, name='local_service_protocol'),
        nullable=False)
    port = sa.Column(sa.Integer, nullable=False)

    attachment_policy = sa.Column(
        sa.Enum(*lsc.ATTACH_POLICIES,
                name='local_service_attachment_policy'),
        nullable=False, default=lsc.ATTACH_OPT_IN)

    distribution_policy = sa.Column(
        sa.Enum(*lsc.DISTRIBUTION_POLICIES,
                name='local_service_distribution_policy'),
        nullable=False, default=lsc.DIST_ROUND_ROBIN)

    exposure_plugin = sa.Column(
        sa.Enum(*lsc.EXPOSURE_PLUGINS,
                name='local_service_exposure_plugin'),
        nullable=False, default=lsc.EXPOSURE_NAT)

    health_check_type = sa.Column(
        sa.Enum(*lsc.HC_TYPES,
                name='local_service_hc_type'),
        nullable=False, default=lsc.HC_NONE)
    health_check_config = sa.Column(sa.Text, nullable=True)

    enabled = sa.Column(sa.Boolean, nullable=False, default=True)

    api_collections = [lsc.COLLECTION_LOCAL_SERVICE]
    api_sub_resources = []
    collection_resource_map = {
        lsc.COLLECTION_LOCAL_SERVICE: lsc.RESOURCE_LOCAL_SERVICE,
    }
    tag_support = False

    __table_args__ = (
        sa.UniqueConstraint('project_id', 'name',
                            name='uniq_local_services_project_name'),
        sa.CheckConstraint('port > 0 AND port <= 65535',
                           name='ck_local_services_port_range'),
    )


class LocalServiceBackend(standard_attr.HasStandardAttributes,
                          model_base.BASEV2,
                          model_base.HasId):
    """Backend endpoint for a local service."""

    __tablename__ = 'local_service_backends'

    service_id = sa.Column(sa.String(36),
                           sa.ForeignKey('local_services.id',
                                         ondelete='CASCADE'),
                           nullable=False, index=True)

    name = sa.Column(sa.String(64), nullable=False)
    availability_zone = sa.Column(sa.String(255), nullable=True)
    weight = sa.Column(sa.Integer, nullable=True)

    address = sa.Column(sa.String(64), nullable=False)
    port = sa.Column(sa.Integer, nullable=False)

    health_check_address = sa.Column(sa.String(64), nullable=True)
    health_check_port = sa.Column(sa.Integer, nullable=True)

    enabled = sa.Column(sa.Boolean, nullable=False, default=True)

    service = orm.relationship(
        LocalService,
        backref=orm.backref('backends', cascade='all, delete-orphan',
                            lazy='joined'))

    api_collections = [lsc.COLLECTION_LOCAL_SERVICE_BACKEND]
    api_sub_resources = []
    collection_resource_map = {
        lsc.COLLECTION_LOCAL_SERVICE_BACKEND:
            lsc.RESOURCE_LOCAL_SERVICE_BACKEND,
    }
    tag_support = False

    __table_args__ = (
        sa.UniqueConstraint('service_id', 'name',
                            name='uniq_local_service_backends_svc_name'),
        sa.CheckConstraint('port > 0 AND port <= 65535',
                           name='ck_local_service_backends_port_range'),
    )


class LocalServiceBinding(standard_attr.HasStandardAttributes,
                          model_base.BASEV2,
                          model_base.HasId,
                          model_base.HasProject):
    """Attachment of a service to a tenant network.

    `enabled=False` lets a tenant opt out of an opt-out service or
    represents the "not yet opted in" state for an opt-in service.
    """

    __tablename__ = 'local_service_bindings'

    service_id = sa.Column(sa.String(36),
                           sa.ForeignKey('local_services.id',
                                         ondelete='CASCADE'),
                           nullable=False, index=True)

    network_id = sa.Column(sa.String(36),
                           sa.ForeignKey('networks.id',
                                         ondelete='CASCADE'),
                           nullable=False, index=True)

    enabled = sa.Column(sa.Boolean, nullable=False, default=True)

    service = orm.relationship(
        LocalService,
        backref=orm.backref('bindings', cascade='all, delete-orphan'))

    api_collections = [lsc.COLLECTION_LOCAL_SERVICE_BINDING]
    api_sub_resources = []
    collection_resource_map = {
        lsc.COLLECTION_LOCAL_SERVICE_BINDING:
            lsc.RESOURCE_LOCAL_SERVICE_BINDING,
    }
    tag_support = False

    __table_args__ = (
        sa.UniqueConstraint('service_id', 'network_id',
                            name='uniq_local_service_bindings_svc_net'),
    )
