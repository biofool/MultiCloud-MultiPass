"""Parallel secret loader — GCP Secret Manager with env-var fallback.

This module provides a single function :func:`get` that replaces
``os.environ.get`` for sensitive values.  It implements an **opt-in
parallel path** to Google Cloud Secret Manager so that existing
env-var-based deployments keep working unchanged until the operator
flips ``USE_SECRET_MANAGER=true``.

Resolution chain (first non-empty hit wins):

  1. **Environment variable** — always checked first when
     ``USE_SECRET_MANAGER`` is not ``"true"`` (the default).  This
     preserves the existing behaviour exactly: no GSM call, no
     behaviour change, no new failure mode.
  2. **GCP Secret Manager** — when ``USE_SECRET_MANAGER=true`` the
     secret is fetched from Secret Manager via the Google Cloud SDK.
     Results are cached with a TTL (``SECRET_LOADER_CACHE_TTL``,
     default 300 s); failures are negative-cached for
     ``SECRET_LOADER_NEG_CACHE_TTL`` (default 30 s).
  3. **Environment variable** — final fallback when GSM comes up
     empty.  This lets a deploy set ``USE_SECRET_MANAGER`` without
     fear: any secret not yet uploaded to GSM still resolves from the
     env var (or Cloud Run ``--set-env-vars``).

The module is inert (returns ``os.environ.get``) unless
``USE_SECRET_MANAGER=true`` is set, so it is safe to import and call
unconditionally — tests and local dev are unaffected.

Environment variables:
  USE_SECRET_MANAGER            Set to "true" to enable the GSM path.
  SECRET_PROJECT_ID             GCP project hosting Secret Manager
                               (default: falls back to PROJECT_ID).
  SECRET_LOADER_CACHE_TTL       Positive cache TTL in seconds (300).
  SECRET_LOADER_NEG_CACHE_TTL   Negative cache TTL in seconds (30).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

log = logging.getLogger("killswitch.secret_loader")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_ENABLED = os.environ.get("USE_SECRET_MANAGER", "").lower() == "true"
_CACHE_TTL = int(os.environ.get("SECRET_LOADER_CACHE_TTL", "300"))
_NEG_TTL = int(os.environ.get("SECRET_LOADER_NEG_CACHE_TTL", "30"))

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[str, float]] = {}
_neg_cache: dict[str, float] = {}
_cache_lock = threading.Lock()


def _cache_get(name: str) -> str | None:
    with _cache_lock:
        entry = _cache.get(name)
        if entry and entry[1] > time.time():
            return entry[0]
    return None


def _cache_set(name: str, value: str) -> None:
    with _cache_lock:
        _cache[name] = (value, time.time() + _CACHE_TTL)


def _neg_cache_get(name: str) -> bool:
    with _cache_lock:
        expiry = _neg_cache.get(name)
        return expiry is not None and expiry > time.time()


def _neg_cache_set(name: str) -> None:
    with _cache_lock:
        _neg_cache[name] = time.time() + _NEG_TTL


def invalidate_cache(name: str | None = None) -> None:
    with _cache_lock:
        if name:
            _cache.pop(name, None)
            _neg_cache.pop(name, None)
        else:
            _cache.clear()
            _neg_cache.clear()


# ---------------------------------------------------------------------------
# Secret Manager access
# ---------------------------------------------------------------------------

_sm_client: Any = None


def _get_sm_client() -> Any:
    global _sm_client
    if _sm_client is None:
        from google.cloud import secretmanager
        _sm_client = secretmanager.SecretManagerServiceClient()
    return _sm_client


def _sm_access(name: str) -> str:
    client = _get_sm_client()
    resource = name
    if not resource.startswith("projects/"):
        project = (
            os.environ.get("SECRET_PROJECT_ID", "")
            or os.environ.get("PROJECT_ID", "")
        )
        if not project:
            raise ValueError(
                "SECRET_PROJECT_ID or PROJECT_ID must be set when "
                "USE_SECRET_MANAGER=true"
            )
        resource = f"projects/{project}/secrets/{name}/versions/latest"
    resp = client.access_secret_version(name=resource)
    # Only strip a single trailing newline (common from echo piping).
    # Full .strip() would corrupt secrets that legitimately contain
    # leading/trailing whitespace (e.g. private keys with trailing newlines).
    value = resp.payload.data.decode("utf-8")
    if value.endswith("\n"):
        value = value[:-1]
    return value


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_enabled() -> bool:
    return _ENABLED


def get(name: str, default: str = "") -> str:
    """Resolve a secret by name.

    When ``USE_SECRET_MANAGER`` is not ``"true"`` this is equivalent to
    ``os.environ.get(name, default)`` — no GSM call, no behaviour change.

    When ``USE_SECRET_MANAGER=true`` the resolution chain is:
    GSM (cached) -> env var -> default.
    """
    if not _ENABLED:
        return os.environ.get(name, default)

    cached = _cache_get(name)
    if cached is not None:
        return cached

    if not _neg_cache_get(name):
        try:
            value = _sm_access(name)
            if value:
                _cache_set(name, value)
                return value
        except Exception as exc:
            log.error(json.dumps({
                "event": "secret_loader_sm_failed",
                "secret": name,
                "error": str(exc)[:200],
            }))
            _neg_cache_set(name)

    return os.environ.get(name, default)
