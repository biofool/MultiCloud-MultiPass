"""Custom boto3 session backed by AWS Secrets Manager.

Builds an explicit ``boto3.session.Session``. Credentials come from AWS
Secrets Manager via :mod:`biofool_secrets.asm` when the source is set to
``secretsmanager``. Otherwise the session uses boto3's default chain (env
vars, ``AWS_SESSION_PROFILE``, or the instance profile).

Fail closed: when the source is ``secretsmanager`` and credentials cannot
be read, :func:`get_session` raises :class:`AwsCredentialError`. It never
falls back to the default chain, so a Secrets Manager outage cannot change
which identity runs a kill action.

Sessions expire after ``AWS_SESSION_TTL_SECONDS`` (default 300), so a
rotated secret is picked up without a restart. Callers that see an auth
error should call :func:`invalidate_session` before retrying.

Usage::

    from aws_session import get_client
    ec2 = get_client("ec2", region="us-east-1")

Environment variables:
  AWS_CREDENTIAL_SOURCE         "secretsmanager" or "default" (default).
  AWS_ACCESS_KEY_ID_SECRET      Secrets Manager name or ARN holding the
                                access key ID. Required when the source is
                                "secretsmanager".
  AWS_SECRET_ACCESS_KEY_SECRET  Secrets Manager name or ARN holding the
                                secret access key. Required when the source
                                is "secretsmanager".
  AWS_REGION                    Default region (fallback: "us-east-1").
  AWS_SESSION_PROFILE           Optional boto3 profile for the default source.
  AWS_SESSION_TTL_SECONDS       Session lifetime in seconds (default 300).
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

log = logging.getLogger("killswitch.aws_session")

_SOURCE_SECRETSMANAGER = "secretsmanager"
_SOURCE_DEFAULT = "default"

# Error codes that mean the cached credentials are no longer valid.
_AUTH_ERROR_CODES = frozenset({
    "ExpiredToken",
    "ExpiredTokenException",
    "InvalidClientTokenId",
    "SignatureDoesNotMatch",
    "UnrecognizedClientException",
    "AuthFailure",
})

_session = None
_session_at = 0.0


class AwsCredentialError(RuntimeError):
    """Credentials could not be loaded from the configured source."""


def _load_biofool_secrets():
    """Import ``biofool_secrets``, falling back to the vendored package path."""
    try:
        from biofool_secrets import asm
    except ImportError:
        pkg = Path(__file__).resolve().parent / "packages" / "biofool-secrets"
        if pkg.is_dir():
            sys.path.insert(0, str(pkg))
        from biofool_secrets import asm
    return asm


def _source() -> str:
    value = os.environ.get("AWS_CREDENTIAL_SOURCE", _SOURCE_DEFAULT).strip().lower()
    if value in (_SOURCE_SECRETSMANAGER, _SOURCE_DEFAULT):
        return value
    # An unknown value is a config error. Failing closed here means a typo
    # cannot silently select the default chain.
    raise AwsCredentialError(f"unknown AWS_CREDENTIAL_SOURCE: {value!r}")


def _default_region() -> str:
    return os.environ.get("AWS_REGION", "us-east-1")


def _ttl_seconds() -> float:
    return float(os.environ.get("AWS_SESSION_TTL_SECONDS", "300"))


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise AwsCredentialError(f"{name} is required when AWS_CREDENTIAL_SOURCE=secretsmanager")
    return value


def _fetch_credentials(region: str) -> tuple[str, str]:
    """Read both AWS keys from Secrets Manager. Raises AwsCredentialError on any failure."""
    key_id_ref = _required_env("AWS_ACCESS_KEY_ID_SECRET")
    secret_key_ref = _required_env("AWS_SECRET_ACCESS_KEY_SECRET")
    asm = _load_biofool_secrets()
    try:
        access_key = asm.access(key_id_ref, region=region)
        secret_key = asm.access(secret_key_ref, region=region)
    except Exception as exc:
        # Log the exception type only. Messages can echo secret identifiers.
        log.error("aws_session: Secrets Manager read failed (%s)", type(exc).__name__)
        raise AwsCredentialError(
            f"Secrets Manager read failed ({type(exc).__name__})"
        ) from exc
    return access_key, secret_key


def get_session():
    """Return a boto3 Session, creating a new one when the cached one has expired."""
    global _session, _session_at
    if _session is not None and time.monotonic() - _session_at < _ttl_seconds():
        return _session

    import boto3

    region = _default_region()
    if _source() == _SOURCE_SECRETSMANAGER:
        access_key, secret_key = _fetch_credentials(region)
        _session = boto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )
        log.info("aws_session: session from Secrets Manager (region=%s)", region)
    else:
        profile = os.environ.get("AWS_SESSION_PROFILE") or None
        _session = boto3.Session(profile_name=profile, region_name=region)
        log.info("aws_session: default-chain session (region=%s, profile=%s)", region, profile)

    _session_at = time.monotonic()
    return _session


def get_client(service: str, region: str | None = None, **kwargs):
    """Return a boto3 client for *service* from the managed session."""
    session = get_session()
    if region:
        kwargs["region_name"] = region
    return session.client(service, **kwargs)


def get_resource(service: str, region: str | None = None, **kwargs):
    """Return a boto3 resource for *service* from the managed session."""
    session = get_session()
    if region:
        kwargs["region_name"] = region
    return session.resource(service, **kwargs)


def is_auth_error(exc: BaseException) -> bool:
    """True when *exc* means the current credentials were rejected or failed to load."""
    if isinstance(exc, AwsCredentialError):
        return True
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    code = (response.get("Error") or {}).get("Code", "")
    return code in _AUTH_ERROR_CODES


def invalidate_session() -> None:
    """Drop the cached session so the next call re-reads credentials."""
    global _session, _session_at
    _session = None
    _session_at = 0.0
