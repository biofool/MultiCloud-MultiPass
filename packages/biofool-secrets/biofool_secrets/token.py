"""Prefixed API-token resolution (CloudManagement#154, biofool-secrets#1).

Naming convention for per-project Cloudflare tokens:

  ``<PREFIX>_CFT``      the project's own token (AIKI, QA, ARM, CM, ...)
  ``<PREFIX>_CFT_dup``  tracked duplicate copy kept in local env files
  ``CLOUDFLARE_API_TOKEN``, ``CFT``   generic fallbacks, in that order

``resolve_token`` returns ``(name, value)`` so callers can log which NAME
won. It never logs or returns anything but the winning value itself.
"""

from __future__ import annotations

import logging
import os
import re

from biofool_secrets import resolver as _resolver

log = logging.getLogger("biofool_secrets")

DEFAULT_GENERIC = ("CLOUDFLARE_API_TOKEN", "CFT")
_PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9]*$")
_ASSIGN_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def clean_value(raw: str | None) -> str:
    """Strip whitespace, CR/LF and one pair of surrounding quotes."""
    v = (raw or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1].strip()
    return v


def parse_env_file(path: str) -> dict[str, str]:
    """Parse ``NAME=value`` / ``export NAME=value`` lines; names to values.

    Comment lines are skipped. Empty values are dropped, and a later
    duplicate never replaces an earlier non-empty value. Unreadable file
    returns ``{}`` with a WARNING (names only).
    """
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        log.warning("cannot read env file %s: %s", path, exc.__class__.__name__)
        return out
    for line in lines:
        if line.lstrip().startswith("#"):
            continue
        m = _ASSIGN_RE.match(line)
        if not m:
            continue
        name, value = m.group(1), clean_value(m.group(2))
        if value and name not in out:
            out[name] = value
    return out


def token_candidates(prefix: str, generic: tuple[str, ...] = DEFAULT_GENERIC) -> list[str]:
    """Names to try, in order: ``<P>_CFT``, ``<P>_CFT_dup``, then *generic*."""
    if not _PREFIX_RE.match(prefix or ""):
        raise ValueError(f"invalid token prefix: {prefix!r} (expected e.g. AIKI)")
    return [f"{prefix}_CFT", f"{prefix}_CFT_dup", *generic]


def resolve_token(
    prefix: str,
    generic: tuple[str, ...] = DEFAULT_GENERIC,
    *,
    env_file: str | None = None,
    secret_project: str | None = None,
) -> tuple[str, str] | None:
    """Resolve a project token; returns ``(name, value)`` or None.

    Order: all names in process env, then all names in *env_file* (when
    given), then all names in Secret Manager (honors ``SECRETS_BACKEND``).
    Values are cleaned; empty counts as unset. The first name in the
    candidate order wins within each source.
    """
    names = token_candidates(prefix, generic)
    for n in names:
        v = clean_value(os.environ.get(n))
        if v:
            log.debug("token resolved from env var %s", n)
            return n, v
    if env_file:
        file_vals = parse_env_file(env_file)
        for n in names:
            if n in file_vals:
                log.debug("token resolved from env file name %s", n)
                return n, file_vals[n]
    if _resolver.backend() != "env":
        for n in names:
            v = clean_value(_resolver._gsm_access(n, secret_project))
            if v:
                log.debug("token resolved from Secret Manager id %s", n)
                return n, v
    return None
