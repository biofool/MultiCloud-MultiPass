"""Exception types, the client-polled kill order model, and the shared
best-effort failure helper for cloud_management_client.

Split out of the original monolithic ``__init__.py`` (see git history) —
pure structural move, no behavior change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("cloud_management_client")


class CloudManagementError(Exception):
    """Raised when CLOUDMANAGEMENT_STRICT=true and a request fails."""


class JobKilledError(Exception):
    """Raised by IntentContext when the hub returns a kill directive.

    The host application should catch this in its job loop to perform
    cleanup and exit gracefully. The ``kill_order`` attribute carries the
    kill directive from the hub (a dict with ``kill_id``, ``intent_id``,
    ``job_id``, ``reason``, ``rule``, etc.).
    """

    def __init__(self, message: str, kill_order: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.kill_order = kill_order or {}


@dataclass
class KillOrder:
    """A kill order from the hub (client-polled kill channel, issue #13)."""
    kill_id: str = ""
    intent_id: str = ""
    project_id: str = ""
    job_id: str = ""
    reason: str = ""
    rule: str = ""
    kill_type: str = ""
    killed: bool = False
    detail: str = ""
    error: str = ""
    timestamp: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def _fail(msg: str, exc: Exception | None = None, strict: bool = False) -> None:
    """Log an error and optionally raise in strict mode."""
    detail = f"{msg}: {exc}" if exc else msg
    log.warning("cloud_management: %s", detail)
    if strict:
        raise CloudManagementError(detail) from exc
