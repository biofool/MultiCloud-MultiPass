"""Plain-data response objects returned by CloudManagementClient.

Split out of the original monolithic ``__init__.py`` — pure structural
move, no behavior change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class IntentResponse:
    """Response from POST /api/v1/intent."""
    intent_id: str = ""
    approved: bool = False
    deferred: bool = False
    budget_remaining_usd: float = 0.0
    budget_short_usd: float = 0.0
    suggested_retry_at: str = ""
    kill_switch_armed: bool = False
    reason: str = ""
    warnings: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ActualResponse:
    """Response from POST /api/v1/actual."""
    actual_id: str = ""
    overrun_detected: bool = False
    status: str = ""
    overrun: dict[str, Any] = field(default_factory=dict)
    kill_result: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class BudgetCheck:
    """Response from GET/POST /api/v1/budget/<project_id>.

    A side-effect-free budget admission probe (budget-informed runtimes).
    ``admit`` is the bottom-line decision: True iff the projected cost
    fits within the project's remaining monthly budget. ``deferred`` is
    True when the cost would push the project over (vs ``admit=False``
    with ``deferred=False`` when the project is already over budget).
    """
    admit: bool = False
    deferred: bool = False
    reason: str = ""
    budget_configured: bool = False
    budget_amount_usd: float = 0.0
    spent_this_month_usd: float = 0.0
    budget_remaining_usd: float = 0.0
    budget_short_usd: float = 0.0
    suggested_retry_at: str = ""
    over_budget: bool = False
    raw: dict[str, Any] = field(default_factory=dict)
