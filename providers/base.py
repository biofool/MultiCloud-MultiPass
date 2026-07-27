"""Provider abstraction for multi-cloud cost control.

Each provider implements two capabilities:
  - ``fetch_billed_costs`` — the reconciliation tier: pull actual billed
    amounts from the cloud's billing/metering API (24-48h lag).
  - ``kill_job`` — execute a per-job kill action described by the intent's
    ``kill`` descriptor (or the registry's ``jobs`` fallback).

The real-time intent/actual protocol (intent.py) is provider-agnostic —
projects self-report usage regardless of cloud.  Providers are only needed
for reconciliation and for cloud-native kill actions (GCP Cloud Run,
OpenStack Nova, etc.).  The portable ``http_callback`` kill type needs no
provider at all.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

log = logging.getLogger("killswitch.providers")


@dataclass
class BilledCost:
    """A single billed cost record from a cloud provider's billing API."""
    project_id: str
    provider: str             # API provider key, e.g. "google_places", "gemini"
    api: str = ""             # specific API endpoint
    cost_usd: float = 0.0
    usage_units: float = 0.0  # calls, tokens, GB-hours, etc.
    usage_unit_type: str = "" # "calls" | "tokens" | "gb_hours" | ...
    period_start: datetime | None = None
    period_end: datetime | None = None
    source: str = ""          # "bigquery_export" | "openstack_metering" | ...
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class KillResult:
    """Result of a kill_job invocation."""
    killed: bool
    job_id: str = ""
    action: str = ""          # "http_callback" | "cloud_run" | "openstack" | ...
    detail: str = ""
    error: str = ""


class CostProvider(ABC):
    """Abstract base for cloud-specific cost control actions."""

    @property
    @abstractmethod
    def cloud(self) -> str:
        """Cloud identifier: "gcp", "openstack", "cloudflare", etc."""

    @abstractmethod
    def kill_job(self, kill_descriptor: dict[str, Any], reason: str) -> KillResult:
        """Execute a per-job kill action.

        ``kill_descriptor`` comes from the intent's ``kill`` field or the
        registry's ``jobs`` fallback.  The ``type`` key selects the action:
        - "cloud_run" / "cloud_scheduler" / "gce" / "gke" (GCP-native)
        - "openstack" (OpenStack Nova)
        - "http_callback" (portable — handled by HttpCallbackProvider)
        """

    def fetch_billed_costs(
        self, project_id: str, since: datetime, until: datetime
    ) -> list[BilledCost]:
        """Pull actual billed costs for reconciliation.

        Default: not implemented (returns empty).  Override in subclasses
        that have a billing/metering API.
        """
        return []
