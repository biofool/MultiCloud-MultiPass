"""Provider registry — routes kill descriptors to the right CostProvider.

Maps the ``type`` field in a kill descriptor (or the ``cloud`` field in an
Account) to a CostProvider instance.  The ``http_callback`` type is handled
by HttpCallbackProvider and works for any cloud.
"""

from __future__ import annotations

import logging
from typing import Any

from providers.base import CostProvider, KillResult

log = logging.getLogger("killswitch.providers.registry")

# Lazy-instantiated singletons
_instances: dict[str, CostProvider] = {}


def _get_provider(cloud: str) -> CostProvider:
    """Get or create a CostProvider instance for a cloud key."""
    if cloud in _instances:
        return _instances[cloud]

    if cloud == "gcp":
        from providers.gcp import GcpProvider
        _instances[cloud] = GcpProvider()
    elif cloud == "openstack":
        from providers.openstack import OpenStackProvider
        _instances[cloud] = OpenStackProvider()
    elif cloud == "cloudflare":
        from providers.cloudflare import CloudflareProvider
        _instances[cloud] = CloudflareProvider()
    elif cloud in ("generic", "http_callback"):
        from providers.http_callback import HttpCallbackProvider
        _instances[cloud] = HttpCallbackProvider()
    else:
        log.warning(f"Unknown cloud provider: {cloud}, falling back to http_callback")
        from providers.http_callback import HttpCallbackProvider
        _instances[cloud] = HttpCallbackProvider()

    return _instances[cloud]


def kill_job(kill_descriptor: dict[str, Any], reason: str) -> KillResult:
    """Route a kill descriptor to the appropriate provider and execute.

    The ``type`` field in the descriptor selects the provider:
    - "http_callback" → HttpCallbackProvider (portable, any cloud)
    - "cloud_run" / "cloud_scheduler" / "gce" / "gke" → GcpProvider
    - "openstack" → OpenStackProvider
    - "cloudflare" → CloudflareProvider
    """
    kill_type = kill_descriptor.get("type", "http_callback")

    # GCP-native types map to the gcp provider
    gcp_types = {"cloud_run", "cloud_scheduler", "gce", "gke"}
    cloud_key = "gcp" if kill_type in gcp_types else kill_type

    provider = _get_provider(cloud_key)
    return provider.kill_job(kill_descriptor, reason)


def fetch_billed_costs(cloud: str, project_id: str, since, until) -> list:
    """Fetch billed costs from a cloud provider's billing API."""
    provider = _get_provider(cloud)
    return provider.fetch_billed_costs(project_id, since, until)
