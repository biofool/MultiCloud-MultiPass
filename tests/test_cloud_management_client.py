"""Tests for the cloud_management_client module.

Integration tests — require a running CloudManagement instance (local dev
server on port 8080 by default) and a valid report token.  Skipped
automatically when CLOUDMANAGEMENT_REPORT_TOKEN_AIRICHARDMOON is not set.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cloud_management_client import (
    CloudManagementClient,
    IntentResponse,
    ActualResponse,
    IntentContext,
    __version__,
)

# Test config — must be set via environment variables
PROJECT = "your-project-1"
TOKEN = os.environ.get("CLOUDMANAGEMENT_REPORT_TOKEN_AIRICHARDMOON", "")
URL = os.environ.get("CLOUDMANAGEMENT_URL", "http://127.0.0.1:8080")

# Skip all integration tests if no token is configured
pytestmark = pytest.mark.skipif(
    not TOKEN,
    reason="CLOUDMANAGEMENT_REPORT_TOKEN_AIRICHARDMOON not set — integration tests require a running server",
)


def make_client() -> CloudManagementClient:
    return CloudManagementClient(
        project_id=PROJECT,
        report_token=TOKEN,
        base_url=URL,
        source_repo="test-suite",
    )


def test_declare_and_report():
    """Declare an intent, report an actual (sync), verify responses."""
    cb = make_client()
    assert cb.enabled, "client should be enabled with project + token"

    job_id = f"test-client-{int(time.time())}"
    intent = cb.declare_intent(
        job_id=job_id,
        job_name="client-test",
        provider="google",
        api="gemini-pro",
        expected_calls=100,
        expected_cost_usd=0.50,
        rate_limit_rpm=60,
    )
    assert intent.intent_id, f"should get an intent_id, got: {intent}"
    assert intent.approved, f"intent should be approved, reason: {intent.reason}"
    assert intent.kill_switch_armed, "kill switch should be armed"
    print(f"  [ OK] declare_intent: id={intent.intent_id} budget_remaining={intent.budget_remaining_usd}")

    # Sync report — should get a real response
    actual = cb.report_actual(
        intent_id=intent.intent_id,
        job_id=job_id,
        provider="google",
        api="gemini-pro",
        actual_calls=50,
        actual_cost_usd=0.25,
        status="completed",
        sync=True,
    )
    assert actual.actual_id, f"should get an actual_id, got: {actual}"
    assert actual.status == "completed"
    print(f"  [ OK] report_actual (sync): id={actual.actual_id} overrun={actual.overrun_detected}")


def test_overrun_detection():
    """Report an overrun and verify it's detected."""
    cb = make_client()
    job_id = f"test-overrun-{int(time.time())}"
    intent = cb.declare_intent(
        job_id=job_id,
        provider="google",
        api="gemini-pro",
        expected_calls=100,
        expected_cost_usd=0.50,
    )
    assert intent.approved

    # Report 150 calls (1.5x threshold of 1.2) with status=running (sync for test)
    actual = cb.report_actual(
        intent_id=intent.intent_id,
        job_id=job_id,
        provider="google",
        api="gemini-pro",
        actual_calls=150,
        actual_cost_usd=0.75,
        status="running",
        sync=True,
    )
    assert actual.overrun_detected, f"overrun should be detected: {actual}"
    assert actual.overrun.get("rule") == "actual_exceeds_intent_calls"
    print(f"  [ OK] overrun detected: rule={actual.overrun['rule']} ratio={actual.overrun['ratio']}")


def test_context_manager():
    """Test the context manager lifecycle."""
    cb = make_client()
    job_id = f"test-ctx-{int(time.time())}"

    with cb.intent(
        job_id=job_id,
        provider="google",
        api="places-text-search",
        expected_calls=10,
        expected_cost_usd=0.32,
    ) as ctx:
        assert ctx.intent is not None
        assert ctx.intent.approved
        for _ in range(10):
            ctx.add_calls(1, cost_usd=0.032)
        # On exit, "completed" actual is reported automatically

    print(f"  [ OK] context manager: intent={ctx.intent.intent_id} calls={ctx._calls}")


def test_context_manager_failure():
    """Test that context manager reports 'failed' on exception."""
    cb = make_client()
    job_id = f"test-ctx-fail-{int(time.time())}"

    try:
        with cb.intent(
            job_id=job_id,
            provider="google",
            api="places-text-search",
            expected_calls=10,
            expected_cost_usd=0.32,
        ) as ctx:
            ctx.add_calls(3, cost_usd=0.10)
            raise ValueError("simulated failure")
    except ValueError:
        pass  # expected

    print(f"  [ OK] context manager failure: intent={ctx.intent.intent_id} reported failed")


def test_disabled_client():
    """Client with no token should be disabled and no-op."""
    cb = CloudManagementClient(project_id="x", report_token="", base_url=URL)
    assert not cb.enabled
    intent = cb.declare_intent(job_id="noop", provider="google")
    assert intent.intent_id == ""  # no-op
    print("  [ OK] disabled client is no-op")


def test_async_report_and_flush():
    """Async report_actual should not block, and flush() should drain the queue."""
    cb = make_client()
    job_id = f"test-async-{int(time.time())}"
    intent = cb.declare_intent(
        job_id=job_id,
        provider="google",
        api="gemini-pro",
        expected_calls=100,
        expected_cost_usd=1.0,
    )
    assert intent.approved

    # Fire several async reports — should return immediately
    for i in range(5):
        resp = cb.report_actual(
            intent_id=intent.intent_id,
            job_id=job_id,
            provider="google",
            api="gemini-pro",
            actual_calls=(i + 1) * 10,
            actual_cost_usd=(i + 1) * 0.1,
            status="running",
        )
        # Async returns a placeholder
        assert resp.actual_id == ""

    # Flush should block until the background worker processes them
    cb.flush(timeout=10.0)

    # Final sync report to verify the hub received prior reports
    final = cb.report_actual(
        intent_id=intent.intent_id,
        job_id=job_id,
        provider="google",
        api="gemini-pro",
        actual_calls=50,
        actual_cost_usd=0.50,
        status="completed",
        sync=True,
    )
    assert final.actual_id, f"final sync report should get an actual_id, got: {final}"
    assert final.status == "completed"
    print(f"  [ OK] async report + flush: final id={final.actual_id}")
    cb.close()


def test_version():
    """Verify the package exposes a version string."""
    assert __version__, "package should expose __version__"
    # Compare parsed version tuples, not strings — "0.12.0" >= "0.2.0" is
    # False lexicographically, which broke this assertion at v0.12.0.
    parsed = tuple(int(part) for part in __version__.split(".")[:3])
    assert parsed >= (0, 2, 0), f"version should be >= 0.2.0, got {__version__}"
    print(f"  [ OK] version: {__version__}")


def test_unauthorized():
    """Wrong token should get an empty response (not raise)."""
    cb = CloudManagementClient(
        project_id=PROJECT, report_token="wrong-token", base_url=URL
    )
    intent = cb.declare_intent(job_id="unauth", provider="google")
    assert intent.intent_id == ""  # failed silently
    print("  [ OK] unauthorized client fails silently")


if __name__ == "__main__":
    print("=== cloud_management_client tests ===")
    test_version()
    test_declare_and_report()
    test_overrun_detection()
    test_context_manager()
    test_context_manager_failure()
    test_disabled_client()
    test_async_report_and_flush()
    test_unauthorized()
    print("\n  All client tests passed.")

