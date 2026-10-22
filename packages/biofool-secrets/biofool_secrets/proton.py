"""Proton Pass read path — optional last-remote fallback in the resolution
chain. Ported from CloudManagement ``proton_bootstrap.py`` (read side only;
vault→GSM sync stays with the hub).

Enabled only when ``PROTON_PASS_ENABLED`` is truthy AND
``PROTON_PASS_AGENT_TOKEN`` is set. ``pass-cli`` is invoked as a subprocess
with the token injected via ``PASS_CLI_AGENT_TOKEN``; values are cached for
``PROTON_PASS_CACHE_TTL`` seconds (default 300) including negative results.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time

log = logging.getLogger("biofool_secrets.proton")

_CACHE: dict[str, tuple[str, float]] = {}


def _truthy(val: str | None) -> bool:
    return val is not None and val.lower() in ("true", "1", "yes", "on")


def is_enabled() -> bool:
    """True when the Proton path is configured and the CLI is usable."""
    if not _truthy(os.environ.get("PROTON_PASS_ENABLED")):
        return False
    if not os.environ.get("PROTON_PASS_AGENT_TOKEN"):
        return False
    cli = os.environ.get("PROTON_PASS_CLI_PATH", "pass-cli")
    if os.path.isabs(cli) and not os.path.isfile(cli):
        return False
    return True


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    token = os.environ.get("PROTON_PASS_AGENT_TOKEN")
    if token:
        env["PASS_CLI_AGENT_TOKEN"] = token
    return subprocess.run(
        [os.environ.get("PROTON_PASS_CLI_PATH", "pass-cli"), *args],
        capture_output=True, text=True, timeout=30, env=env,
    )


def get_secret(key_name: str, vault: str | None = None) -> str:
    """Resolve one secret from Proton Pass. "" when disabled or not found."""
    if not is_enabled():
        return ""
    cached = _CACHE.get(key_name)
    if cached is not None:
        value, ts = cached
        if time.time() - ts < int(os.environ.get("PROTON_PASS_CACHE_TTL", "300")):
            return value
        _CACHE.pop(key_name, None)

    vault_name = vault or os.environ.get("PROTON_PASS_VAULT", "CloudManagement")
    try:
        result = _run_cli(
            "item", "get", "--vault-name", vault_name,
            "--name", key_name, "--field", "password",
        )
    except FileNotFoundError:
        log.warning(json.dumps({
            "event": "proton_cli_not_found",
            "key": key_name,
            "cli_path": os.environ.get("PROTON_PASS_CLI_PATH", "pass-cli"),
        }))
        return ""
    except subprocess.TimeoutExpired:
        log.warning(json.dumps({
            "event": "proton_cli_timeout", "key": key_name, "vault": vault_name,
        }))
        return ""

    value = result.stdout.strip() if result.returncode == 0 else ""
    _CACHE[key_name] = (value, time.time())
    return value


def reset() -> None:
    """Clear the Proton result cache — for tests."""
    _CACHE.clear()
