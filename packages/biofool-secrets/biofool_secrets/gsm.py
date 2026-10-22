"""GCP Secret Manager access — google-cloud-secretmanager SDK first,
``gcloud`` CLI fallback. Never logs or returns secret values in logs.

The project id is resolved per call (never hardcoded): env var order is
``GCP_SECRET_PROJECT`` → ``SECRET_PROJECT_ID`` → ``GCP_PROJECT_ID`` →
``GOOGLE_CLOUD_PROJECT`` → ``PROJECT_ID``. Callers may also pass a project
explicitly.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import Any

log = logging.getLogger("biofool_secrets.gsm")

_GCLOUD_TIMEOUT = 15

# Substrings that mark a gcloud stderr message as a genuine "no such
# secret" — everything else (auth, permission, config) looks identical
# on a bare non-zero exit code and must WARN, not silently miss.
_NOT_FOUND_MARKERS = ("NOT_FOUND", "not found", "404")

_sdk_client: Any = None
_sdk_checked: bool = False
_gcloud_available: bool | None = None


def secret_project(explicit: str | None = None) -> str:
    """GCP project id to read secrets from ("" when unset)."""
    return (
        explicit
        or os.environ.get("GCP_SECRET_PROJECT")
        or os.environ.get("SECRET_PROJECT_ID")
        or os.environ.get("GCP_PROJECT_ID")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("PROJECT_ID")
        or ""
    )


def _sdk():
    """Return an initialized SecretManagerServiceClient or None."""
    global _sdk_client, _sdk_checked
    if _sdk_client is not None:
        return _sdk_client
    if _sdk_checked:
        return None
    _sdk_checked = True
    try:
        from google.cloud import secretmanager  # type: ignore
        _sdk_client = secretmanager.SecretManagerServiceClient()
    except Exception as exc:  # ImportError or ADC failure — degrade to gcloud
        log.debug("secretmanager SDK unavailable, falling back to gcloud: %s", exc)
        _sdk_client = None
    return _sdk_client


def _gcloud() -> str | None:
    """Path to the gcloud binary, or None (detected once per process)."""
    global _gcloud_available
    if _gcloud_available is None:
        _gcloud_available = shutil.which("gcloud")
        if _gcloud_available is None:
            log.debug("gcloud not on PATH — CLI Secret Manager path disabled")
    return _gcloud_available


def _access_sdk(name: str, project: str) -> str | None:
    resource = name if name.startswith("projects/") else (
        f"projects/{project}/secrets/{name}/versions/latest"
    )
    resp = _sdk().access_secret_version(name=resource)
    value = resp.payload.data.decode("utf-8")
    # Strip only one trailing newline — a full .strip() corrupts secrets
    # that legitimately carry surrounding whitespace (e.g. PEM blocks).
    return value[:-1] if value.endswith("\n") else value


def _access_gcloud(name: str, project: str) -> str | None:
    cli = _gcloud()
    if cli is None:
        return None
    args = [cli, "secrets", "versions", "access", "latest", f"--secret={name}"]
    if project:
        args.append(f"--project={project}")
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, check=False,
            timeout=_GCLOUD_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        log.warning("Timed out fetching secret %s", name)
        return None
    except OSError as exc:
        log.warning("gcloud lookup failed for %s: %s", name, exc)
        return None
    if proc.returncode == 0:
        return proc.stdout.rstrip("\n") or None
    stderr = (proc.stderr or "").strip()
    if any(m in stderr for m in _NOT_FOUND_MARKERS):
        log.debug("secret %s not available via gcloud (project %s)", name, project)
    else:
        log.warning(
            "gcloud lookup for %s failed (project %s, not a plain not-found "
            "— check gcloud auth / project / IAM): %s",
            name, project, stderr[:200] or "no error output",
        )
    return None


def access(name: str, project: str | None = None) -> str | None:
    """Fetch one secret version value. SDK first, gcloud CLI fallback.

    Returns the value, or None on any failure (missing, unreachable,
    unauthenticated). Never logs the value.
    """
    proj = secret_project(project)
    if not name.startswith("projects/") and not proj:
        log.debug("no GCP project configured for secret %s — skipping GSM", name)
        return None
    if _sdk() is not None:
        try:
            return _access_sdk(name, proj)
        except Exception as exc:
            log.warning("Secret Manager SDK fetch failed for %s: %s", name, exc)
            return None
    return _access_gcloud(name, proj)


def list_names(project: str | None = None) -> list[str]:
    """List secret NAMES (never values). SDK first, gcloud CLI fallback."""
    proj = secret_project(project)
    if _sdk() is not None:
        try:
            parent = f"projects/{proj}"
            return [
                s.name.rsplit("/", 1)[-1]
                for s in _sdk().list_secrets(request={"parent": parent})
            ]
        except Exception as exc:
            log.warning("Secret Manager SDK list failed in %s: %s", proj, exc)
            return []
    cli = _gcloud()
    if cli is None:
        return []
    args = [cli, "secrets", "list", "--format=value(name)"]
    if proj:
        args.append(f"--project={proj}")
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, check=False,
            timeout=_GCLOUD_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        log.warning("Timed out listing secrets in project %s", proj)
        return []
    except OSError as exc:
        log.warning("gcloud secrets list failed: %s", exc)
        return []
    if proc.returncode != 0:
        log.warning(
            "gcloud secrets list failed in project %s: %s",
            proj, proc.stderr.strip()[:200],
        )
        return []
    return [n.strip() for n in proc.stdout.splitlines() if n.strip()]


def _push_sdk(project: str, name: str, value: str) -> tuple[bool, str]:
    """Create *name* (if needed) and add a version via the Python SDK.

    Does not log ``value``. Returns (ok, detail) for the caller's report.
    """
    client = _sdk()
    if client is None:
        return False, "gcloud not found and google-cloud-secretmanager not installed"
    parent = f"projects/{project}"
    try:
        from google.api_core.exceptions import AlreadyExists
    except ImportError:
        AlreadyExists = type("AlreadyExists", (Exception,), {})  # noqa: N806
    try:
        client.create_secret(
            request={
                "parent": parent,
                "secret_id": name,
                "secret": {"replication": {"automatic": {}}},
            }
        )
    except AlreadyExists:
        pass
    except Exception as exc:
        if "already exists" not in str(exc).lower():
            err = str(exc)[:200]
            log.warning("Failed to create secret %s via SDK: %s", name, err)
            return False, err
    try:
        client.add_secret_version(
            request={
                "parent": f"{parent}/secrets/{name}",
                "payload": {"data": value.encode("utf-8")},
            }
        )
    except Exception as exc:
        err = str(exc)[:200]
        log.warning("Failed to add version to secret %s via SDK: %s", name, err)
        return False, err
    return True, "imported"


def push_secret(name: str, value: str, project: str | None = None) -> tuple[bool, str]:
    """Write *value* as a new version of *name*, creating the secret first.

    gcloud CLI first (streams the value on stdin, never argv), SDK fallback
    when gcloud is missing or unexecutable. Returns (ok, detail) — the
    contract shared by the three bootstrap_env.py push paths this replaces.
    Explicitly invoked by operators (--push-secrets), so it is NOT gated
    by ``SECRETS_BACKEND`` — that switch guards ambient resolution only.
    Never logs ``value``.
    """
    proj = secret_project(project)
    cli = _gcloud()
    if cli is not None:
        create = [
            cli, "secrets", "create", name,
            f"--project={proj}",
            "--replication-policy=automatic",
            "--quiet",
        ]
        try:
            # ALREADY_EXISTS is expected for existing secrets — ignored.
            subprocess.run(
                create, capture_output=True, text=True, check=False,
                timeout=_GCLOUD_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            log.warning("Timed out creating secret %s in %s", name, proj)
            return False, "create timed out"
        except OSError as exc:
            log.info("gcloud unavailable (%s) — falling back to SDK", exc)
            return _push_sdk(proj, name, value)
        add = [
            cli, "secrets", "versions", "add", name,
            f"--project={proj}",
            "--data-file=-",
            "--quiet",
        ]
        try:
            proc = subprocess.run(
                add, input=value, capture_output=True, text=True,
                check=False, timeout=_GCLOUD_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            log.warning(
                "Timed out adding version to %s — gcloud may be waiting "
                "for interactive auth. Run 'gcloud auth login' and retry.",
                name,
            )
            return False, "add version timed out"
        except OSError as exc:
            log.info("gcloud unavailable (%s) — falling back to SDK", exc)
            return _push_sdk(proj, name, value)
        if proc.returncode == 0:
            return True, "imported"
        err = (proc.stderr or proc.stdout or "unknown error").strip()
        log.warning("gcloud push failed for %s: %s", name, err[:200])
        return False, err
    return _push_sdk(proj, name, value)


def reset() -> None:
    """Re-probe SDK/CLI availability — for tests."""
    global _sdk_client, _sdk_checked, _gcloud_available
    _sdk_client = None
    _sdk_checked = False
    _gcloud_available = None
