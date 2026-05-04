"""REST client for the local-services Neutron extension.

Deliberately small: keystoneauth's Session already handles auth,
endpoint discovery, retries, and SSL. This wrapper just adds the
``/v2.0/<collection>`` path and JSON envelope conventions that
neutron uses.
"""

from urllib import parse as urlparse


_API_PREFIX = '/v2.0'


class LocalServicesClientError(Exception):
    pass


class NotFound(LocalServicesClientError):
    pass


class Client:
    def __init__(self, session, endpoint):
        self.session = session
        self.endpoint = endpoint.rstrip('/')

    def _url(self, collection, _id=None, **filters):
        path = f'{self.endpoint}{_API_PREFIX}/{collection}'
        if _id is not None:
            path = f'{path}/{_id}'
        if filters:
            # Drop None and empty values so callers can pass kwargs blindly.
            qs = {k: v for k, v in filters.items() if v not in (None, '')}
            if qs:
                path = f'{path}?{urlparse.urlencode(qs, doseq=True)}'
        return path

    def list(self, collection, **filters):
        resp = self.session.get(self._url(collection, **filters))
        resp.raise_for_status()
        return resp.json().get(collection, [])

    def show(self, collection, resource, _id):
        resp = self.session.get(self._url(collection, _id))
        if resp.status_code == 404:
            raise NotFound(f'{resource} {_id} not found')
        resp.raise_for_status()
        return resp.json()[resource]

    def create(self, collection, resource, body):
        resp = self.session.post(self._url(collection),
                                 json={resource: body})
        resp.raise_for_status()
        return resp.json()[resource]

    def update(self, collection, resource, _id, body):
        resp = self.session.put(self._url(collection, _id),
                                json={resource: body})
        if resp.status_code == 404:
            raise NotFound(f'{resource} {_id} not found')
        resp.raise_for_status()
        return resp.json()[resource]

    def delete(self, collection, resource, _id):
        resp = self.session.delete(self._url(collection, _id))
        if resp.status_code == 404:
            raise NotFound(f'{resource} {_id} not found')
        resp.raise_for_status()

    def find_by_name_or_id(self, collection, resource, ref):
        """Resolve a name-or-id reference to a full resource dict.

        Tries id first via a direct GET, falls back to a name filter.
        Used only by `local-service` commands; backends and bindings
        stay UUID-only in v1.
        """
        try:
            return self.show(collection, resource, ref)
        except NotFound:
            pass
        matches = self.list(collection, name=ref)
        if not matches:
            raise NotFound(f'{resource} {ref!r} not found')
        if len(matches) > 1:
            raise LocalServicesClientError(
                f'{resource} {ref!r} matches {len(matches)} resources; '
                'specify by id')
        return matches[0]
