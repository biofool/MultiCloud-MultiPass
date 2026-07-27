"""Project-root path resolution for CloudManagement.

All config and data file paths in this project should be resolved relative
to the project root, not the current working directory. This module
provides ``PROJECT_ROOT`` and ``resolve()`` so that paths work correctly
whether the service runs from the repo root (local dev, Cloud Run build
context) or from a different CWD (tests, scripts invoked from elsewhere).

Usage::

    from paths import resolve
    accounts_file = resolve("config/accounts.yaml")
    audit_dir = resolve("data/audit")

The project root is determined by walking up from this file's location
until a directory containing ``pyproject.toml`` is found. If the env var
``CLOUDMANAGEMENT_ROOT`` is set, it overrides the auto-detected root —
useful for Docker/Cloud Run where the code may be copied to a different
path (e.g. ``/app/``). Only set this in trusted environments (Docker,
Cloud Run, CI); an attacker who can set it can redirect all file reads.

Relative paths passed to ``resolve()`` are interpreted relative to
``PROJECT_ROOT``, not the CWD. Absolute paths are returned unchanged.
This means env-var overrides like ``ACCOUNTS_FILE=config/accounts.yaml``
always resolve to ``<PROJECT_ROOT>/config/accounts.yaml`` regardless of
where the process was invoked from. To use a path outside the project
root, set the env var to an absolute path.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("cloudmanagement.paths")

# Allow override via env var (Docker, Cloud Run, tests)
_ENV_ROOT = os.environ.get("CLOUDMANAGEMENT_ROOT", "").strip()

# Walk up from this file to find the project root (directory with pyproject.toml)
_THIS_FILE = Path(__file__).resolve()
if _ENV_ROOT:
    PROJECT_ROOT: Path = Path(_ENV_ROOT).resolve()
    if not (PROJECT_ROOT / "paths.py").is_file():
        log.warning(
            "CLOUDMANAGEMENT_ROOT=%s does not contain paths.py — "
            "path resolution may be incorrect. Ensure this env var "
            "points to the project root directory.",
            PROJECT_ROOT,
        )
else:
    _candidate = _THIS_FILE.parent
    for _ancestor in [_candidate, *_candidate.parents]:
        if (_ancestor / "pyproject.toml").is_file():
            PROJECT_ROOT = _ancestor
            break
    else:
        # Fallback: the directory containing this file
        PROJECT_ROOT = _candidate


def resolve(*parts: str) -> str:
    """Resolve a path relative to the project root.

    Returns an absolute path string. Relative paths are joined to
    ``PROJECT_ROOT``. Absolute paths are returned unchanged (so
    explicit absolute env-var overrides still work).
    """
    joined = os.path.join(*parts) if parts else ""
    if os.path.isabs(joined):
        return joined
    return str(PROJECT_ROOT / joined)


def resolve_path(*parts: str) -> Path:
    """Like ``resolve()`` but returns a ``Path`` object."""
    return Path(resolve(*parts))
