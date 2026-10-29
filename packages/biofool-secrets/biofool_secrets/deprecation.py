"""Deprecated-secret-name registry — port of CloudManagement
``cloudmanagement/deprecation_registry.py`` (issue CloudManagement#65).

A YAML file maps deprecated alias names to canonical names; resolving a
deprecated alias emits one structured WARNING per process and returns the
canonical name. Names only — never secret values.

Registry file discovery:
  1. ``load_registry(path)`` explicit argument
  2. ``BIOFOOL_SECRETS_DEPRECATED_FILE`` env var
  3. ``config/deprecated_secrets.yaml`` under the current working directory

A missing/unreadable registry is a soft degradation logged at WARNING —
never silently ignored, never fatal.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("biofool_secrets.deprecation")


@dataclass(frozen=True)
class DeprecatedSecret:
    alias: str
    canonical: str
    deprecated_since: str
    sunset_on: str


_lock = threading.Lock()
_by_alias: dict[str, DeprecatedSecret] = {}
_loaded: bool = False
_warned: set[str] = set()


def _default_path() -> Path:
    env = os.environ.get("BIOFOOL_SECRETS_DEPRECATED_FILE")
    if env:
        return Path(env)
    return Path.cwd() / "config" / "deprecated_secrets.yaml"


def load_registry(config_path: str | Path | None = None) -> list[DeprecatedSecret]:
    """Load (or reload) the registry. Returns the entry list."""
    global _by_alias, _loaded
    path = Path(config_path) if config_path else _default_path()
    entries: list[DeprecatedSecret] = []
    try:
        import yaml  # type: ignore
    except ImportError:
        log.warning("pyyaml unavailable — deprecated-secret registry disabled")
        entries = []
    else:
        try:
            data = yaml.safe_load(path.read_text()) or {}
            for row in data.get("aliases", []):
                entries.append(DeprecatedSecret(
                    alias=row["alias"],
                    canonical=row["canonical"],
                    deprecated_since=str(row.get("deprecated_since", "")),
                    sunset_on=str(row.get("sunset_on", "")),
                ))
        except FileNotFoundError:
            log.debug("no deprecated-secret registry at %s", path)
        except Exception as exc:
            log.warning("failed to load deprecated-secret registry %s: %s", path, exc)
    with _lock:
        _by_alias = {e.alias: e for e in entries}
        _loaded = True
    return entries


def _ensure_loaded() -> None:
    if not _loaded:
        load_registry()


def is_deprecated(name: str) -> DeprecatedSecret | None:
    """Return the registry entry for *name*, or None."""
    if not name:
        return None
    _ensure_loaded()
    with _lock:
        return _by_alias.get(name)


def get_canonical(name: str) -> str:
    """Canonical name for *name* (itself when not deprecated)."""
    dep = is_deprecated(name)
    return dep.canonical if dep else name


def emit_deprecation_warning(name: str, caller: str = "unknown") -> None:
    """Emit a structured WARNING once per process per deprecated alias."""
    dep = is_deprecated(name)
    if dep is None or dep.alias in _warned:
        return
    _warned.add(dep.alias)
    log.warning(json.dumps({
        "event": "deprecated_secret_resolved",
        "alias": dep.alias,
        "canonical": dep.canonical,
        "sunset_on": dep.sunset_on,
        "caller": caller,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }))


def reset() -> None:
    """Clear loaded state — for tests."""
    global _by_alias, _loaded, _warned
    with _lock:
        _by_alias = {}
        _loaded = False
        _warned = set()
