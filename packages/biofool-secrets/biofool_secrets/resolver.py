"""Unified secret resolution — the canonical implementation replacing the
5 diverged copies tracked in CloudManagement#129.

Resolution chain for ``resolve_secret(name)``:

  1. Deprecation canonicalization — a deprecated alias logs a structured
     WARNING once per process and resolves as its canonical name.
  2. Environment variables — every candidate (canonical + aliases) is
     checked before any remote call; an exported key always wins.
  3. GCP Secret Manager — google-cloud-secretmanager SDK when installed,
     ``gcloud`` CLI otherwise (per candidate, TTL + negative cached).
  4. Proton Pass — optional, only when PROTON_PASS_ENABLED + token.
  5. ``default``.

Ordering knobs:
  ``SECRETS_PREFER_REMOTE=true`` puts the GSM pass *before* the env pass
  (CloudManagement's enabled-mode semantics — remote values can rotate
  beneath a stale env var). Default is env-first (bootstrap semantics:
  what the operator exported is what runs).

Backend selection — ``SECRETS_BACKEND``:
  ``auto``   (default) SDK if importable, else gcloud CLI, else env-only
  ``env``    never touch remote sources — CI/test kill-switch (story_graph#151)
  ``sdk``    SDK only (fail closed if missing)
  ``gcloud`` gcloud CLI only
For CloudManagement-style callers, ``USE_SECRET_MANAGER=false`` forces
``env`` and ``USE_SECRET_MANAGER=true`` forces ``auto`` — but an explicit
``SECRETS_BACKEND`` always wins.

Caching: positive hits TTL ``SECRET_LOADER_CACHE_TTL`` (300 s), misses
``SECRET_LOADER_NEG_CACHE_TTL`` (30 s) — the established names from the
three secret_loader copies, kept so existing deploy configs still work.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

from biofool_secrets import aliases as _aliases
from biofool_secrets import deprecation as _deprecation
from biofool_secrets import gsm as _gsm
from biofool_secrets import proton as _proton

log = logging.getLogger("biofool_secrets")

_cache: dict[str, tuple[str, float]] = {}
_neg_cache: dict[str, float] = {}
_lock = threading.Lock()


def _ttl() -> int:
    return int(os.environ.get("SECRET_LOADER_CACHE_TTL", "300"))


def _neg_ttl() -> int:
    return int(os.environ.get("SECRET_LOADER_NEG_CACHE_TTL", "30"))


def backend() -> str:
    """Effective remote backend: auto|env|sdk|gcloud.

    Unrecognized values fail CLOSED (``env``) with a WARNING — a typo'd
    kill-switch must never silently enable remote lookups.
    """
    explicit = os.environ.get("SECRETS_BACKEND", "").lower()
    if explicit in ("env", "sdk", "gcloud", "auto"):
        return explicit
    if explicit:
        log.warning(json.dumps({
            "event": "secrets_backend_unrecognized",
            "value": explicit, "effective": "env",
        }))
        return "env"
    usm = os.environ.get("USE_SECRET_MANAGER", "").lower()
    if usm in ("false", "0", "no"):
        return "env"
    if usm in ("true", "1", "yes"):
        return "auto"
    if usm:
        log.warning(json.dumps({
            "event": "use_secret_manager_unrecognized",
            "value": usm, "effective": "env",
        }))
        return "env"
    return "auto"


def _remote_allowed(kind: str) -> bool:
    """Whether backend *kind* (sdk/gcloud) may be used under the current backend."""
    be = backend()
    if be == "env":
        return False
    if be == "auto":
        return True
    return be == kind


def _cache_get(name: str) -> str | None:
    with _lock:
        entry = _cache.get(name)
        if entry and entry[1] > time.time():
            return entry[0]
    return None


def _cache_set(name: str, value: str) -> None:
    with _lock:
        _cache[name] = (value, time.time() + _ttl())


def _neg_get(name: str) -> bool:
    with _lock:
        exp = _neg_cache.get(name)
        return exp is not None and exp > time.time()


def _neg_set(name: str) -> None:
    with _lock:
        _neg_cache[name] = time.time() + _neg_ttl()


def _gsm_access(candidate: str, project: str | None) -> str | None:
    """GSM fetch honoring the backend switch + caches."""
    if _neg_get(candidate):
        return None
    cached = _cache_get(candidate)
    if cached is not None:
        return cached
    if not (_remote_allowed("sdk") or _remote_allowed("gcloud")):
        return None
    value = _gsm.access(candidate, project)
    if value:
        _cache_set(candidate, value)
        return value
    _neg_set(candidate)
    return None


def resolve_secret(
    name: str,
    default: str = "",
    *,
    project: str | None = None,
    secret_project: str | None = None,
    prefer_remote: bool | None = None,
) -> str:
    """Resolve a secret by name. Never logs the resolved value.

    Args:
        name: canonical or alias key name (e.g. ``BRAVE_SEARCH_API_KEY``)
        default: returned when nothing resolves
        project: alias-registry project scope (default:
            ``BIOFOOL_SECRETS_PROJECT`` env var)
        secret_project: GCP project for the GSM pass (default: resolved
            from ``GCP_SECRET_PROJECT``/``SECRET_PROJECT_ID``/… env vars)
        prefer_remote: GSM before env when True; default None reads
            ``SECRETS_PREFER_REMOTE``. CloudManagement's enabled mode
            passes True — remote values may rotate beneath stale env vars.
    """
    canonical = name
    dep = _deprecation.is_deprecated(name)
    if dep is not None:
        _deprecation.emit_deprecation_warning(name, caller="resolve_secret")
        canonical = dep.canonical

    candidates = [canonical, *_aliases.aliases_for(canonical, project)]
    if canonical != name:  # resolving via a deprecated alias: keep it last
        candidates.append(name)

    if prefer_remote is None:
        prefer_remote = os.environ.get("SECRETS_PREFER_REMOTE", "").lower() in (
            "true", "1", "yes",
        )

    def _env_pass() -> str | None:
        for c in candidates:
            v = os.environ.get(c, "")
            if v:
                log.debug("%s resolved from env var %s", name, c)
                return v
        return None

    def _remote_pass() -> str | None:
        if backend() == "env":
            return None
        for c in candidates:
            v = _gsm_access(c, secret_project)
            if v:
                log.debug("%s resolved from Secret Manager id %s", name, c)
                return v
        v = _proton.get_secret(canonical)
        if v:
            log.debug("%s resolved from Proton Pass", name)
            return v
        return None

    first, second = (_remote_pass, _env_pass) if prefer_remote else (_env_pass, _remote_pass)
    return first() or second() or default


def get(name: str, default: str = "", **kwargs) -> str:
    """Alias for :func:`resolve_secret` (CloudManagement secret_loader API)."""
    return resolve_secret(name, default, **kwargs)


def fetch_secret(name: str, secret_project: str | None = None) -> str | None:
    """Fetch one GSM secret by exact name — backend-gated, TTL/negative
    cached. Returns None under ``SECRETS_BACKEND=env`` or on any failure.

    Used by callers that enumerate ``list_secret_names()`` then fetch a
    subset directly (e.g. story_graph's Gemini key discovery), bypassing
    alias/deprecation resolution deliberately.
    """
    return _gsm_access(name, secret_project)


def list_secret_names(secret_project: str | None = None) -> list[str]:
    """List secret NAMES (never values) in the configured project.

    Returns [] under ``SECRETS_BACKEND=env`` and on any failure — with a
    WARNING already logged by the gsm layer.
    """
    if backend() == "env":
        return []
    return _gsm.list_names(secret_project)


def is_enabled() -> bool:
    """True when remote resolution is active (backend != env)."""
    return backend() != "env"


def invalidate_cache(name: str | None = None) -> None:
    """Clear cached values — one name, or everything when None."""
    with _lock:
        if name:
            _cache.pop(name, None)
            _neg_cache.pop(name, None)
        else:
            _cache.clear()
            _neg_cache.clear()


def reset_caches() -> None:
    """Clear all caches + re-probe backends — for tests."""
    invalidate_cache()
    _aliases.reset()
    _deprecation.reset()
    _gsm.reset()
    _proton.reset()


def resolution_report(name: str, **kwargs) -> dict:
    """Names-only diagnostic: which candidates were tried, what resolved.

    Returns a dict safe to log — contains key NAMES and the winning source
    (env / secret-manager / proton / default), NEVER the value.
    """
    canonical = _deprecation.get_canonical(name)
    candidates = [canonical, *_aliases.aliases_for(canonical, kwargs.get("project"))]
    report: dict = {
        "name": name,
        "canonical": canonical,
        "deprecated": canonical != name,
        "backend": backend(),
        "candidates": candidates,
        "resolved_from": "default",
    }
    for c in candidates:
        if os.environ.get(c):
            report["resolved_from"] = f"env:{c}"
            return report
    if backend() != "env":
        for c in candidates:
            if _gsm_access(c, kwargs.get("secret_project")):
                report["resolved_from"] = f"secret-manager:{c}"
                return report
        if _proton.get_secret(canonical):
            report["resolved_from"] = "proton"
    return report
