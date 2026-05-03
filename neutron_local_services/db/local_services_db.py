"""DB-mixin: CRUD operations for local-services models.

Returns plain dicts shaped for the API layer. The plugin class
composes this with the API descriptor and adds OVN/host_routes
side-effects on top.
"""

import netaddr
from neutron_lib import constants as nl_const
from neutron_lib import exceptions as n_exc
from neutron_lib.db import api as db_api
from neutron_lib.db import model_query
from neutron_lib.db import utils as db_utils
from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import uuidutils

from neutron_local_services import conf as ls_conf
from neutron_local_services import constants as lsc
from neutron_local_services.db import models


LOG = logging.getLogger(__name__)


class LocalServicesDbMixin:
    """Mixin providing DB CRUD for local services / backends / bindings."""

    # ----- helpers -----

    @staticmethod
    def _make_service_dict(svc, fields=None):
        d = {
            'id': svc.id,
            'project_id': svc.project_id,
            'name': svc.name,
            'description': svc.description,
            'local_ipv4': svc.local_ipv4,
            'local_ipv6': svc.local_ipv6,
            'protocol': svc.protocol,
            'port': svc.port,
            'attachment_policy': svc.attachment_policy,
            'distribution_policy': svc.distribution_policy,
            'exposure_plugin': svc.exposure_plugin,
            'health_check_type': svc.health_check_type,
            'health_check_config': svc.health_check_config,
            'enabled': svc.enabled,
        }
        return db_utils.resource_fields(d, fields)

    @staticmethod
    def _make_backend_dict(b, fields=None):
        d = {
            'id': b.id,
            'service_id': b.service_id,
            'name': b.name,
            'availability_zone': b.availability_zone,
            'weight': b.weight,
            'address': b.address,
            'port': b.port,
            'health_check_address': b.health_check_address,
            'health_check_port': b.health_check_port,
            'enabled': b.enabled,
        }
        return db_utils.resource_fields(d, fields)

    @staticmethod
    def _make_binding_dict(s, fields=None):
        d = {
            'id': s.id,
            'project_id': s.project_id,
            'service_id': s.service_id,
            'network_id': s.network_id,
            'enabled': s.enabled,
        }
        return db_utils.resource_fields(d, fields)

    @staticmethod
    def _validate_vip(addr):
        if addr is None:
            return
        denylist = cfg.CONF[ls_conf.GROUP].vip_denylist
        try:
            ip = netaddr.IPAddress(addr)
        except (netaddr.AddrFormatError, ValueError):
            raise n_exc.InvalidInput(
                error_message=f'invalid IP address: {addr}')
        if str(ip) in denylist:
            raise n_exc.InvalidInput(
                error_message=f'VIP {addr} is in the configured denylist')

    @classmethod
    def _validate_service_payload(cls, body):
        if not body.get('local_ipv4') and not body.get('local_ipv6'):
            raise n_exc.InvalidInput(
                error_message='at least one of local_ipv4 or local_ipv6 '
                              'must be provided')
        cls._validate_vip(body.get('local_ipv4'))
        cls._validate_vip(body.get('local_ipv6'))

    # ----- local_services -----

    @db_api.CONTEXT_WRITER
    def create_local_service(self, context, local_service):
        body = local_service[lsc.RESOURCE_LOCAL_SERVICE]
        self._validate_service_payload(body)
        svc = models.LocalService(
            id=uuidutils.generate_uuid(),
            project_id=body.get('project_id'),
            name=body['name'],
            description=body.get('description'),
            local_ipv4=body.get('local_ipv4'),
            local_ipv6=body.get('local_ipv6'),
            protocol=body['protocol'],
            port=body['port'],
            attachment_policy=body.get('attachment_policy',
                                       lsc.ATTACH_OPT_IN),
            distribution_policy=body.get('distribution_policy',
                                         lsc.DIST_ROUND_ROBIN),
            exposure_plugin=body.get('exposure_plugin', lsc.EXPOSURE_NAT),
            health_check_type=body.get('health_check_type', lsc.HC_NONE),
            health_check_config=body.get('health_check_config'),
            enabled=body.get('enabled', True),
        )
        context.session.add(svc)
        context.session.flush()
        return self._make_service_dict(svc)

    @db_api.CONTEXT_WRITER
    def update_local_service(self, context, id_, local_service):
        body = local_service[lsc.RESOURCE_LOCAL_SERVICE]
        svc = self._get_service_or_raise(context, id_)
        for key in ('name', 'description', 'local_ipv4', 'local_ipv6',
                    'attachment_policy', 'distribution_policy',
                    'health_check_type', 'health_check_config', 'enabled'):
            if key in body:
                setattr(svc, key, body[key])
        if 'local_ipv4' in body or 'local_ipv6' in body:
            self._validate_service_payload({
                'local_ipv4': svc.local_ipv4,
                'local_ipv6': svc.local_ipv6,
            })
        context.session.flush()
        return self._make_service_dict(svc)

    @db_api.CONTEXT_WRITER
    def delete_local_service(self, context, id_):
        svc = self._get_service_or_raise(context, id_)
        context.session.delete(svc)

    @db_api.CONTEXT_READER
    def get_local_service(self, context, id_, fields=None):
        return self._make_service_dict(
            self._get_service_or_raise(context, id_), fields)

    @db_api.CONTEXT_READER
    def get_local_services(self, context, filters=None, fields=None,
                           sorts=None, limit=None, marker=None,
                           page_reverse=False):
        marker_obj = db_utils.get_marker_obj(self, context, 'local_service',
                                             limit, marker)
        return [
            self._make_service_dict(s, fields) for s in
            self._collection_query(
                context, models.LocalService, filters, sorts, limit,
                marker_obj, page_reverse)
        ]

    # ----- local_service_backends -----

    @db_api.CONTEXT_WRITER
    def create_local_service_backend(self, context, local_service_backend):
        body = local_service_backend[lsc.RESOURCE_LOCAL_SERVICE_BACKEND]
        # Make sure the service exists; FK would also catch this but the
        # error message is nicer when we raise NotFound.
        self._get_service_or_raise(context, body['service_id'])
        # The API attr-map default for ``weight`` is ATTR_NOT_SPECIFIED
        # (a sentinel object) — neutron-api passes that through unchanged
        # when the client omits the field, and writing the sentinel into
        # MySQL fails with "Incorrect integer value: '<Sentinel ...>'".
        # Translate to NULL (nullable column; LVS plugin treats NULL as 1).
        weight = body.get('weight')
        if weight is nl_const.ATTR_NOT_SPECIFIED:
            weight = None
        b = models.LocalServiceBackend(
            id=uuidutils.generate_uuid(),
            service_id=body['service_id'],
            name=body['name'],
            availability_zone=body.get('availability_zone'),
            weight=weight,
            address=body['address'],
            port=body['port'],
            health_check_address=body.get('health_check_address'),
            health_check_port=body.get('health_check_port'),
            enabled=body.get('enabled', True),
        )
        context.session.add(b)
        context.session.flush()
        return self._make_backend_dict(b)

    @db_api.CONTEXT_WRITER
    def update_local_service_backend(self, context, id_,
                                     local_service_backend):
        body = local_service_backend[lsc.RESOURCE_LOCAL_SERVICE_BACKEND]
        b = self._get_backend_or_raise(context, id_)
        for key in ('name', 'availability_zone', 'weight', 'address', 'port',
                    'health_check_address', 'health_check_port', 'enabled'):
            if key in body:
                setattr(b, key, body[key])
        context.session.flush()
        return self._make_backend_dict(b)

    @db_api.CONTEXT_WRITER
    def delete_local_service_backend(self, context, id_):
        b = self._get_backend_or_raise(context, id_)
        context.session.delete(b)

    @db_api.CONTEXT_READER
    def get_local_service_backend(self, context, id_, fields=None):
        return self._make_backend_dict(
            self._get_backend_or_raise(context, id_), fields)

    @db_api.CONTEXT_READER
    def get_local_service_backends(self, context, filters=None, fields=None,
                                   sorts=None, limit=None, marker=None,
                                   page_reverse=False):
        marker_obj = db_utils.get_marker_obj(
            self, context, 'local_service_backend', limit, marker)
        return [
            self._make_backend_dict(b, fields) for b in
            self._collection_query(
                context, models.LocalServiceBackend, filters, sorts, limit,
                marker_obj, page_reverse)
        ]

    # ----- local_service_bindings -----

    @db_api.CONTEXT_WRITER
    def create_local_service_binding(self, context, local_service_binding):
        body = local_service_binding[lsc.RESOURCE_LOCAL_SERVICE_BINDING]
        self._get_service_or_raise(context, body['service_id'])
        s = models.LocalServiceBinding(
            id=uuidutils.generate_uuid(),
            project_id=body.get('project_id'),
            service_id=body['service_id'],
            network_id=body['network_id'],
            enabled=body.get('enabled', True),
        )
        context.session.add(s)
        context.session.flush()
        return self._make_binding_dict(s)

    @db_api.CONTEXT_WRITER
    def update_local_service_binding(self, context, id_,
                                     local_service_binding):
        body = local_service_binding[lsc.RESOURCE_LOCAL_SERVICE_BINDING]
        s = self._get_binding_or_raise(context, id_)
        if 'enabled' in body:
            s.enabled = body['enabled']
        context.session.flush()
        return self._make_binding_dict(s)

    @db_api.CONTEXT_WRITER
    def delete_local_service_binding(self, context, id_):
        s = self._get_binding_or_raise(context, id_)
        context.session.delete(s)

    @db_api.CONTEXT_READER
    def get_local_service_binding(self, context, id_, fields=None):
        return self._make_binding_dict(
            self._get_binding_or_raise(context, id_), fields)

    @db_api.CONTEXT_READER
    def get_local_service_bindings(self, context, filters=None, fields=None,
                                   sorts=None, limit=None, marker=None,
                                   page_reverse=False):
        marker_obj = db_utils.get_marker_obj(
            self, context, 'local_service_binding', limit, marker)
        return [
            self._make_binding_dict(s, fields) for s in
            self._collection_query(
                context, models.LocalServiceBinding, filters, sorts, limit,
                marker_obj, page_reverse)
        ]

    # ----- internals -----

    @staticmethod
    def _collection_query(context, model, filters, sorts, limit,
                          marker_obj, page_reverse):
        # NOTE: db_utils.model_query_scope_is_project returns a BOOL
        # (whether scoping is needed), not a Query — confused for the
        # query builder in earlier code. The right helper is
        # model_query.query_with_hooks, which builds the query and
        # applies project scoping in one call.
        q = model_query.query_with_hooks(context, model)
        if filters:
            q = model_query.apply_filters(q, model, filters)
        return q.all()

    @staticmethod
    def _get_service_or_raise(context, id_):
        obj = context.session.query(models.LocalService).filter_by(
            id=id_).first()
        if not obj:
            raise n_exc.ObjectNotFound(id=id_)
        return obj

    @staticmethod
    def _get_backend_or_raise(context, id_):
        obj = context.session.query(models.LocalServiceBackend).filter_by(
            id=id_).first()
        if not obj:
            raise n_exc.ObjectNotFound(id=id_)
        return obj

    @staticmethod
    def _get_binding_or_raise(context, id_):
        obj = context.session.query(models.LocalServiceBinding).filter_by(
            id=id_).first()
        if not obj:
            raise n_exc.ObjectNotFound(id=id_)
        return obj
