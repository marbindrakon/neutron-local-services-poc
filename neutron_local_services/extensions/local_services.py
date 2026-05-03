"""Neutron API extension descriptor for the local-services plugin.

Neutron's `api-paste.ini` discovers extension classes by name. The
class lives in this module so the Neutron extension manager can find
it under `neutron_local_services.extensions.local_services` once we
register the path in `neutron.conf`'s `api_extensions_path`.
"""

import abc

from neutron_lib.api import extensions as api_extensions
from neutron_lib.plugins import directory
from neutron_lib.services import base as service_base

from neutron.api import extensions
from neutron.api.v2 import base

from neutron_local_services.api import local_services as ls_apidef
from neutron_local_services import constants as lsc


PLUGIN_TYPE = 'LOCAL_SERVICES'


class Local_services(api_extensions.APIExtensionDescriptor):
    """Extension class supporting Local Services."""

    api_definition = ls_apidef

    @classmethod
    def get_resources(cls):
        plugin = directory.get_plugin(PLUGIN_TYPE)
        resources = []
        for collection_name, resource_name in (
                (ls_apidef.LOCAL_SERVICES, ls_apidef.LOCAL_SERVICE),
                (ls_apidef.LOCAL_SERVICE_BACKENDS,
                 ls_apidef.LOCAL_SERVICE_BACKEND),
                (ls_apidef.LOCAL_SERVICE_BINDINGS,
                 ls_apidef.LOCAL_SERVICE_BINDING)):
            params = ls_apidef.RESOURCE_ATTRIBUTE_MAP.get(
                collection_name, {})
            url_collection = collection_name.replace('_', '-')
            controller = base.create_resource(
                url_collection, resource_name, plugin, params,
                allow_bulk=True, allow_pagination=True, allow_sorting=True)
            resources.append(extensions.ResourceExtension(
                url_collection, controller, attr_map=params))
        return resources


class LocalServicesPluginBase(service_base.ServicePluginBase,
                              metaclass=abc.ABCMeta):

    @classmethod
    def get_plugin_type(cls):
        return PLUGIN_TYPE

    def get_plugin_description(self):
        return 'Local Services'

    # local_services CRUD
    @abc.abstractmethod
    def create_local_service(self, context, local_service):
        pass

    @abc.abstractmethod
    def update_local_service(self, context, id_, local_service):
        pass

    @abc.abstractmethod
    def delete_local_service(self, context, id_):
        pass

    @abc.abstractmethod
    def get_local_service(self, context, id_, fields=None):
        pass

    @abc.abstractmethod
    def get_local_services(self, context, filters=None, fields=None,
                           sorts=None, limit=None, marker=None,
                           page_reverse=False):
        pass

    # local_service_backends CRUD
    @abc.abstractmethod
    def create_local_service_backend(self, context, local_service_backend):
        pass

    @abc.abstractmethod
    def update_local_service_backend(self, context, id_,
                                     local_service_backend):
        pass

    @abc.abstractmethod
    def delete_local_service_backend(self, context, id_):
        pass

    @abc.abstractmethod
    def get_local_service_backend(self, context, id_, fields=None):
        pass

    @abc.abstractmethod
    def get_local_service_backends(self, context, filters=None,
                                   fields=None, sorts=None, limit=None,
                                   marker=None, page_reverse=False):
        pass

    # local_service_bindings CRUD
    @abc.abstractmethod
    def create_local_service_binding(self, context, local_service_binding):
        pass

    @abc.abstractmethod
    def update_local_service_binding(self, context, id_,
                                     local_service_binding):
        pass

    @abc.abstractmethod
    def delete_local_service_binding(self, context, id_):
        pass

    @abc.abstractmethod
    def get_local_service_binding(self, context, id_, fields=None):
        pass

    @abc.abstractmethod
    def get_local_service_bindings(self, context, filters=None, fields=None,
                                   sorts=None, limit=None, marker=None,
                                   page_reverse=False):
        pass


# Re-export the alias so callers can import a single canonical name.
ALIAS = lsc.ALIAS
