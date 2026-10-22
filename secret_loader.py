"""Parallel secret loader — thin shim over the canonical ``biofool-secrets``
package (CloudManagement#130 P5 / MultiCloud-MultiPass convergence).

All resolution machinery — GSM access, caching, alias registry, Proton Pass
fallback — now lives in the vendored ``packages/biofool-secrets`` snapshot
so the diverged repo copies converge on one implementation. This module
keeps this repo's established API (``get``/``is_enabled``/``invalidate_cache``)
and its **opt-in** gate:

* ``USE_SECRET_MANAGER`` not ``"true"`` (default): pure env-var lookup —
  identical to pre-migration behaviour. No remote calls, no new failure modes.
* ``USE_SECRET_MANAGER=true``: delegates to
  ``biofool_secrets.resolve_secret(..., prefer_remote=True)`` — GSM
  (SDK, then gcloud CLI) → Proton Pass → env → default.

Environment variables:
  USE_SECRET_MANAGER            Set to "true" to enable the remote path.
  SECRET_PROJECT_ID             GCP project hosting Secret Manager
                               (default: falls back to PROJECT_ID).
  PROTON_PASS_ENABLED           "true" to enable the Proton Pass fallback.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

try:
    from biofool_secrets import resolver as _resolver
    from biofool_secrets import gsm as _gsm
except ImportError:
    # Bare checkout without the package installed — fall back to the
    # vendored snapshot under packages/.
    _pkg = Path(__file__).resolve().parent / "packages" / "biofool-secrets"
    if _pkg.is_dir():
        sys.path.insert(0, str(_pkg))
    from biofool_secrets import resolver as _resolver
    from biofool_secrets import gsm as _gsm

log = logging.getLogger("killswitch.secret_loader")

_ENABLED = os.environ.get("USE_SECRET_MANAGER", "").lower() == "true"

_no_project_warned = False


def is_enabled() -> bool:
    """Return True if the Secret Manager path is active."""
    return _ENABLED


def invalidate_cache(name: str | None = None) -> None:
    """Clear the cache for a specific key, or all entries if ``name`` is None."""
    _resolver.invalidate_cache(name)


def _warn_no_project_once() -> None:
    """USE_SECRET_MANAGER=true with no GCP project env is a misconfig —
    the GSM pass can never resolve. Warn once (never fail silently)."""
    global _no_project_warned
    if _no_project_warned or _gsm.secret_project():
        return
    _no_project_warned = True
    log.warning(json.dumps({
        "event": "secret_loader_no_gcp_project",
        "detail": "USE_SECRET_MANAGER=true but no SECRET_PROJECT_ID / "
                  "PROJECT_ID / GCP_* project var is set — GSM disabled",
    }))


def get(name: str, default: str = "") -> str:
    """Resolve a secret by name.

    When ``USE_SECRET_MANAGER`` is not ``"true"`` this is equivalent to
    ``os.environ.get(name, default)`` — no remote call, no behaviour change.

    When ``USE_SECRET_MANAGER=true`` the resolution chain is:
    GSM (cached) -> Proton Pass -> env var -> default.
    Full ``projects/...`` resource names pass through to GSM unchanged.
    """
    if _ENABLED:
        _warn_no_project_once()
        return _resolver.resolve_secret(name, default, prefer_remote=True)

    # Disabled path — env-only, identical to pre-migration behaviour.
    return os.environ.get(name, default)
