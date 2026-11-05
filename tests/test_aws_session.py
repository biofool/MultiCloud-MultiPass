"""Tests for aws_session — Secrets Manager source, fail-closed behaviour,
session TTL/invalidation, and the kill path under credential failure.

No real AWS calls: boto3.Session and biofool_secrets.asm.access are patched.
"""

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

import aws_session

_ENV_KEYS = (
    "AWS_CREDENTIAL_SOURCE",
    "AWS_ACCESS_KEY_ID_SECRET",
    "AWS_SECRET_ACCESS_KEY_SECRET",
    "AWS_REGION",
    "AWS_SESSION_PROFILE",
    "AWS_SESSION_TTL_SECONDS",
    "DRY_RUN",
)


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    aws_session.invalidate_session()
    asm = aws_session._load_biofool_secrets()
    asm.reset()
    yield asm
    aws_session.invalidate_session()
    asm.reset()


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "test"}}, "GetSecretValue")


def _use_secretsmanager(monkeypatch):
    monkeypatch.setenv("AWS_CREDENTIAL_SOURCE", "secretsmanager")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID_SECRET", "example/aws-key-id")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY_SECRET", "example/aws-secret")


class TestDefaultSource:
    def test_default_chain_does_not_read_secrets_manager(self, monkeypatch, _clean_state):
        asm = _clean_state
        with patch.object(asm, "access") as access, patch("boto3.Session") as session_cls:
            aws_session.get_session()

        access.assert_not_called()
        session_cls.assert_called_once_with(profile_name=None, region_name="us-east-1")

    def test_profile_is_passed_through(self, monkeypatch):
        monkeypatch.setenv("AWS_SESSION_PROFILE", "dev")
        with patch("boto3.Session") as session_cls:
            aws_session.get_session()
        session_cls.assert_called_once_with(profile_name="dev", region_name="us-east-1")


class TestSecretsManagerSource:
    def test_reads_both_keys_and_builds_session(self, monkeypatch, _clean_state):
        asm = _clean_state
        _use_secretsmanager(monkeypatch)
        with patch.object(asm, "access", side_effect=["AKIAEXAMPLE", "secret-value"]) as access, \
                patch("boto3.Session") as session_cls:
            aws_session.get_session()

        assert access.call_args_list[0].args[0] == "example/aws-key-id"
        assert access.call_args_list[1].args[0] == "example/aws-secret"
        session_cls.assert_called_once_with(
            aws_access_key_id="AKIAEXAMPLE",
            aws_secret_access_key="secret-value",
            region_name="us-east-1",
        )

    def test_missing_secret_ref_raises_without_fallback(self, monkeypatch, _clean_state):
        asm = _clean_state
        monkeypatch.setenv("AWS_CREDENTIAL_SOURCE", "secretsmanager")
        with patch.object(asm, "access") as access, patch("boto3.Session") as session_cls:
            with pytest.raises(aws_session.AwsCredentialError, match="AWS_ACCESS_KEY_ID_SECRET"):
                aws_session.get_session()

        access.assert_not_called()
        session_cls.assert_not_called()

    def test_access_denied_raises_without_fallback(self, monkeypatch, _clean_state):
        asm = _clean_state
        _use_secretsmanager(monkeypatch)
        with patch.object(asm, "access", side_effect=_client_error("AccessDeniedException")), \
                patch("boto3.Session") as session_cls:
            with pytest.raises(aws_session.AwsCredentialError, match="ClientError"):
                aws_session.get_session()

        session_cls.assert_not_called()

    def test_unknown_source_fails_closed(self, monkeypatch):
        monkeypatch.setenv("AWS_CREDENTIAL_SOURCE", "secrets-manager")
        with patch("boto3.Session") as session_cls:
            with pytest.raises(aws_session.AwsCredentialError, match="unknown AWS_CREDENTIAL_SOURCE"):
                aws_session.get_session()

        session_cls.assert_not_called()


class TestSessionLifetime:
    def test_session_is_cached_within_ttl(self, monkeypatch, _clean_state):
        asm = _clean_state
        _use_secretsmanager(monkeypatch)
        with patch.object(asm, "access", side_effect=["k", "s"] * 2) as access, \
                patch("boto3.Session") as session_cls:
            first = aws_session.get_session()
            second = aws_session.get_session()

        assert first is second
        assert access.call_count == 2
        session_cls.assert_called_once()

    def test_expired_session_rereads_secret(self, monkeypatch, _clean_state):
        asm = _clean_state
        _use_secretsmanager(monkeypatch)
        monkeypatch.setenv("AWS_SESSION_TTL_SECONDS", "0")
        with patch.object(asm, "access", side_effect=["k1", "s1", "k2", "s2"]) as access, \
                patch("boto3.Session") as session_cls:
            aws_session.get_session()
            aws_session.get_session()

        assert access.call_count == 4
        assert session_cls.call_count == 2

    def test_invalidate_forces_reread(self, monkeypatch, _clean_state):
        asm = _clean_state
        _use_secretsmanager(monkeypatch)
        with patch.object(asm, "access", side_effect=["k1", "s1", "k2", "s2"]) as access, \
                patch("boto3.Session"):
            aws_session.get_session()
            aws_session.invalidate_session()
            aws_session.get_session()

        assert access.call_count == 4


class TestIsAuthError:
    @pytest.mark.parametrize("code", ["ExpiredToken", "InvalidClientTokenId", "SignatureDoesNotMatch"])
    def test_credential_codes_are_auth_errors(self, code):
        assert aws_session.is_auth_error(_client_error(code)) is True

    def test_permission_error_is_not_auth_error(self):
        assert aws_session.is_auth_error(_client_error("AccessDenied")) is False

    def test_credential_error_is_auth_error(self):
        assert aws_session.is_auth_error(aws_session.AwsCredentialError("x")) is True

    def test_plain_exception_is_not_auth_error(self):
        assert aws_session.is_auth_error(Exception("boom")) is False


class TestKillFailsClosed:
    def test_kill_refused_when_secret_unreadable(self, monkeypatch, _clean_state):
        """A Secrets Manager outage must stop the kill, not run it under another identity."""
        from providers.aws import AwsProvider

        asm = _clean_state
        _use_secretsmanager(monkeypatch)
        monkeypatch.setenv("DRY_RUN", "false")
        with patch.object(asm, "access", side_effect=_client_error("ExpiredToken")), \
                patch("boto3.Session") as session_cls:
            result = AwsProvider().kill_job(
                {"type": "ec2", "instance_id": "i-abc", "region": "us-east-1"},
                reason="overrun",
            )

        assert result.killed is False
        assert "Secrets Manager read failed" in result.error
        session_cls.assert_not_called()
        assert aws_session._session is None  # auth error dropped the cache

    def test_kill_uses_secret_backed_session(self, monkeypatch, _clean_state):
        from providers.aws import AwsProvider

        asm = _clean_state
        _use_secretsmanager(monkeypatch)
        monkeypatch.setenv("DRY_RUN", "false")
        ec2 = MagicMock()
        session = MagicMock()
        session.client.return_value = ec2
        with patch.object(asm, "access", side_effect=["k", "s"]), \
                patch("boto3.Session", return_value=session):
            result = AwsProvider().kill_job(
                {"type": "ec2", "instance_id": "i-abc", "region": "us-east-1", "action": "stop"},
                reason="overrun",
            )

        assert result.killed is True
        ec2.stop_instances.assert_called_once_with(InstanceIds=["i-abc"])
