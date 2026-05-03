"""Exposure-plugin abstraction.

The agent's job ends at "the netns exists, the tap has the right IPs,
and the /32 VIPs are present so the kernel ARP-responds." Everything
beyond that — turning a (vip, port, proto) tuple into something
backed by real backends — is the exposure plugin's job.

Two plugins are planned:

- ``lvs``   — Keepalived in the netns drives ip_vs (kernel L4 LB).
              Default.
- ``envoy`` — Envoy in the netns terminates connections and re-originates
              them.

The agent's reconciler groups desired services by ``exposure_plugin``
and calls each plugin's ``apply_config(netns, services)`` once per
group. Plugins coexist in the same netns (mixed-plugin tests cover this), so each one
must own only its own VIPs / listeners and not stomp on the others'.

Why an abstract base instead of duck typing: the agent has to decide
whether to even *load* a plugin (avoid importing Envoy if no service
asks for it). A registry that maps ``exposure_plugin`` → plugin class
keeps that decision local to this module, and the registry is keyed on
the same strings the API enforces in ``constants.EXPOSURE_PLUGINS``.
"""

import abc

from neutron_local_services import constants as lsc


class ExposurePlugin(abc.ABC):
    """Per-(network, plugin) lifecycle for proxy config + processes.

    Lifetime mirrors the netns: the agent calls ``apply_config`` on
    every reconcile pass for as long as services exist, and ``teardown``
    once the network goes away. Plugins must be idempotent under
    repeated ``apply_config`` calls — the reconciler may fire on every
    Port_Binding event AND every 10s timer tick.

    A plugin instance handles one plugin type but is shared across all
    networks the agent manages — per-network state lives on disk under
    ``<state_dir>/<netns>/<plugin_name>/`` and in the (vip, port, proto)
    tuples passed in.
    """

    #: Matches one of ``constants.EXPOSURE_PLUGINS``. The reconciler
    #: looks the plugin up by this name when grouping services.
    name: str = ''

    @abc.abstractmethod
    def apply_config(self, netns_name, services):
        """Bring the in-netns proxy in line with ``services``.

        ``services`` is a list of dicts (see ``registry_client``). Each
        dict carries the full service config (vip, port, proto,
        distribution_policy, health_check_*) and a ``backends`` list of
        enabled backends. Empty list means "no services for me on this
        network" — the plugin should tear its own state down for that
        netns (NOT the netns itself; the agent owns that).

        Idempotent. Returns nothing on success; raises on hard failure
        (the reconciler logs and moves on so one plugin's blowup
        doesn't sink the others).
        """

    @abc.abstractmethod
    def teardown(self, netns_name):
        """Stop processes and clean up plugin state for ``netns_name``.

        Called when the agent decides a netns is going away. The agent
        will subsequently destroy the namespace itself, which kills any
        in-netns processes the plugin spawned — but on-disk state under
        ``<state_dir>/<netns>/<plugin_name>/`` is the plugin's to clean.
        Idempotent (safe to call when nothing was ever applied).
        """

    def get_backend_health(self, netns_name, service_id, backend_id):
        """Return ``'up' | 'down' | 'unknown'`` for a backend.

        Default: ``'unknown'``. Plugins override to expose health to the
        registry API. Optional in — the LVS plugin reads from
        ``ipvsadm`` lazily; not wired into the API yet.
        """
        return 'unknown'

    def get_stats(self, netns_name):
        """Return a dict of plugin-specific stats for ``netns_name``.

        Default: empty dict. Optional.
        """
        return {}


_REGISTRY = {}


def register(plugin_cls):
    """Register a plugin class against ``plugin_cls.name``.

    Used by ``lvs.py`` / ``envoy.py`` at import time. The reconciler
    asks ``get(name)`` to look up an instance.
    """
    if not plugin_cls.name:
        raise ValueError('Plugin class %s has empty name' % plugin_cls)
    if plugin_cls.name not in lsc.EXPOSURE_PLUGINS:
        raise ValueError(
            'Plugin name %r not in EXPOSURE_PLUGINS %r — keep the API '
            'attr-map and the plugin registry in sync'
            % (plugin_cls.name, lsc.EXPOSURE_PLUGINS))
    _REGISTRY[plugin_cls.name] = plugin_cls()
    return plugin_cls


def get(name):
    """Return the registered plugin instance for ``name``, or None.

    ``None`` for an unregistered plugin lets the caller log-and-skip
    rather than crash — useful when an operator references a plugin
    we haven't built yet.
    """
    return _REGISTRY.get(name)


def all_plugins():
    """All registered plugin instances. Used at teardown to fan out."""
    return list(_REGISTRY.values())


def reset_for_tests():
    """Clear the registry. Unit tests register fakes; setUp calls this."""
    _REGISTRY.clear()
