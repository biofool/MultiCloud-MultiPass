"""AWS Secrets Manager access via boto3.

Uses boto3's default credential chain (instance profile, ``AWS_PROFILE``,
or env vars). It never goes through ``aws_session``, so there is no
bootstrap loop. Errors propagate to the caller: this module never
returns a fallback value. Secret values are never logged.
"""

from __future__ import annotations

from typing import Any

_clients: dict[str, Any] = {}


def _client(region: str | None) -> Any:
    key = region or ""
    if key not in _clients:
        import boto3

        _clients[key] = boto3.client("secretsmanager", region_name=region or None)
    return _clients[key]


def access(secret_id: str, region: str | None = None) -> str:
    """Return the ``SecretString`` for *secret_id* (name or ARN).

    Raises on any failure: missing secret, denied access, no credentials,
    or a binary-only secret. Never returns an empty string.
    """
    if not secret_id:
        raise ValueError("secret_id is required")
    resp = _client(region).get_secret_value(SecretId=secret_id)
    value = resp.get("SecretString")
    if not value:
        raise ValueError("secret has no SecretString value (binary secrets unsupported)")
    return value


def reset() -> None:
    """Drop cached clients — for tests."""
    _clients.clear()
