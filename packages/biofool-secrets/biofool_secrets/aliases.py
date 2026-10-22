"""KEY_ALIASES registry — canonical replacement for 5 diverged copies.

Loads ``key_aliases.json`` bundled with the package and merges an optional
operator override file (``BIOFOOL_SECRETS_ALIASES_FILE`` — same JSON shape;
its entries win per-key). Global ``aliases`` apply to every consumer;
``project_aliases`` apply only when the caller selects a project via
``resolve_secret(project=...)`` or the ``BIOFOOL_SECRETS_PROJECT`` env var.

Project scoping exists because some names are semantically unsafe to alias
globally — e.g. ``GOOGLE_API_KEY`` means "the Gemini key" in story_graph but
"any interchangeable Google key" in WorldStudioFinder. See the ``notes``
section of key_aliases.json for documented gotchas.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from importlib.resources import files as _pkg_files
from pathlib import Path
from typing import Any

log = logging.getLogger("biofool_secrets.aliases")

_lock = threading.Lock()
_registry: dict[str, Any] | None = None


def _load_bundled() -> dict[str, Any]:
    data = _pkg_files("biofool_secrets").joinpath("key_aliases.json").read_text()
    return json.loads(data)


def load_registry(override_path: str | Path | None = None) -> dict[str, Any]:
    """Load the alias registry (bundled + optional override file).

    The override file uses the same ``{"aliases": {...},
    "project_aliases": {...}}`` shape and is merged per-key on top of the
    bundled registry. Missing override files are a WARNING, never silent.
    """
    global _registry
    reg = _load_bundled()
    path = override_path or os.environ.get("BIOFOOL_SECRETS_ALIASES_FILE")
    if path:
        try:
            extra = json.loads(Path(path).read_text())
            for section in ("aliases", "project_aliases"):
                for key, val in extra.get(section, {}).items():
                    if section == "project_aliases" and isinstance(val, dict):
                        reg.setdefault("project_aliases", {}).setdefault(key, {}).update(val)
                    else:
                        reg.setdefault(section, {})[key] = val
        except FileNotFoundError:
            log.warning("BIOFOOL_SECRETS_ALIASES_FILE not found: %s", path)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("failed to load aliases override %s: %s", path, exc)
    with _lock:
        _registry = reg
    return reg


def registry() -> dict[str, Any]:
    """Return the loaded registry, loading on first access."""
    with _lock:
        if _registry is not None:
            return _registry
    return load_registry()


def aliases_for(name: str, project: str | None = None) -> list[str]:
    """Return ordered alias candidates for *name* (global + project-scoped).

    Order: global aliases first (same-credential spellings), then
    project-scoped aliases (GSM ids / repo-specific names), deduplicated.
    """
    reg = registry()
    project = project or os.environ.get("BIOFOOL_SECRETS_PROJECT", "")
    out: list[str] = list(reg.get("aliases", {}).get(name, []))
    if project:
        proj = reg.get("project_aliases", {}).get(project, {})
        out.extend(a for a in proj.get(name, []) if a not in out)
    return out


def all_projects() -> list[str]:
    """Return the list of projects with scoped alias sections."""
    return sorted(registry().get("project_aliases", {}).keys())


def reset() -> None:
    """Drop the cached registry — for tests that monkeypatch files/env."""
    global _registry
    with _lock:
        _registry = None
