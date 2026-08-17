"""Data model for the intent/actual reporting protocol: Intent, Actual,
and ExpectedCost.

Split out of the original monolithic ``intent.py`` — pure structural
move, no behavior change. Self-contained: these are plain dataclasses
with no dependency on intent.py's env-derived config, so this module
needs no back-reference to ``intent``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Intent:
    """A declared API usage intent from a sub-project."""
    intent_id: str
    project_id: str
    source_repo: str = ""
    job_id: str = ""
    job_name: str = ""
    provider: str = ""
    api: str = ""
    expected_calls: int = 0
    expected_cost_usd: float = 0.0
    expected_tokens: int | None = None
    rate_limit_rpm: int = 0
    window_start: str = ""
    window_end: str = ""
    kill: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    approved: bool = True
    status: str = "declared"  # declared | running | completed | failed | killed
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Intent":
        return cls(
            intent_id=d.get("intent_id", ""),
            project_id=d.get("project_id", ""),
            source_repo=d.get("source_repo", ""),
            job_id=d.get("job_id", ""),
            job_name=d.get("job_name", ""),
            provider=d.get("provider", ""),
            api=d.get("api", ""),
            expected_calls=int(d.get("expected_calls", 0)),
            expected_cost_usd=float(d.get("expected_cost_usd", 0)),
            expected_tokens=d.get("expected_tokens"),
            rate_limit_rpm=int(d.get("rate_limit_rpm", 0)),
            window_start=d.get("window_start", ""),
            window_end=d.get("window_end", ""),
            kill=d.get("kill", {}),
            metadata=d.get("metadata", {}),
            approved=bool(d.get("approved", True)),
            status=d.get("status", "declared"),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
        )


@dataclass
class Actual:
    """An actual API usage report (post-call or incremental)."""
    actual_id: str
    intent_id: str
    project_id: str
    job_id: str = ""
    provider: str = ""
    api: str = ""
    actual_calls: int = 0
    actual_cost_usd: float = 0.0
    actual_tokens: int | None = None
    status: str = "completed"  # running | completed | failed | killed
    started_at: str = ""
    ended_at: str = ""
    sequence: int = 0           # for incremental reports
    reconciled_cost_usd: float | None = None
    reconciled_at: str | None = None
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Actual":
        return cls(
            actual_id=d.get("actual_id", ""),
            intent_id=d.get("intent_id", ""),
            project_id=d.get("project_id", ""),
            job_id=d.get("job_id", ""),
            provider=d.get("provider", ""),
            api=d.get("api", ""),
            actual_calls=int(d.get("actual_calls", 0)),
            actual_cost_usd=float(d.get("actual_cost_usd", 0)),
            actual_tokens=d.get("actual_tokens"),
            status=d.get("status", "completed"),
            started_at=d.get("started_at", ""),
            ended_at=d.get("ended_at", ""),
            sequence=int(d.get("sequence", 0)),
            reconciled_cost_usd=d.get("reconciled_cost_usd"),
            reconciled_at=d.get("reconciled_at"),
            created_at=d.get("created_at", ""),
        )


@dataclass
class ExpectedCost:
    """Authoritative expected cost for a (project, provider) pair."""
    project_id: str
    provider: str
    unit_cost_usd: float = 0.0
    free_tier_remaining_calls: int | None = None
    free_tier_reset: str = ""
    expected_remaining_monthly_usd: float = 0.0
    calibration_delta: float = 0.0
    pricing: dict[str, Any] = field(default_factory=dict)  # e.g. {input_cost_per_million, output_cost_per_million}
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ExpectedCost":
        return cls(
            project_id=d.get("project_id", ""),
            provider=d.get("provider", ""),
            unit_cost_usd=float(d.get("unit_cost_usd", 0)),
            free_tier_remaining_calls=d.get("free_tier_remaining_calls"),
            free_tier_reset=d.get("free_tier_reset", ""),
            expected_remaining_monthly_usd=float(d.get("expected_remaining_monthly_usd", 0)),
            calibration_delta=float(d.get("calibration_delta", 0)),
            pricing=d.get("pricing", {}),
            updated_at=d.get("updated_at", ""),
        )
