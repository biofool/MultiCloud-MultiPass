"""Offline unit tests for cloud_management_client.

Unlike tests/test_cloud_management_client.py these require no running
CloudManagement instance and no report token — they exercise the durable
spool and client_seq bookkeeping added in v0.12.0 against a temp directory
and an unreachable base_url.
"""
import json
import glob
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cloud_management_client import CloudManagementClient, __version__

# Port 9 is the reserved discard port — connections are refused immediately,
# so every report fails fast and stays in the spool.
UNREACHABLE = "http://127.0.0.1:9"


def _client(spool_dir, token="test-token"):
    return CloudManagementClient(
        project_id="test-project",
        report_token=token,
        base_url=UNREACHABLE,
        spool_dir=str(spool_dir),
    )


def _spooled_seqs(spool_dir):
    seqs = []
    for path in glob.glob(os.path.join(str(spool_dir), "*.json")):
        with open(path, encoding="utf-8") as f:
            seqs.append(json.load(f)["payload"]["client_seq"])
    return sorted(seqs)


def test_version_is_compared_numerically():
    """__version__ must parse as a numeric tuple >= 0.2.0.

    Guards the lexicographic-comparison trap: "0.12.0" >= "0.2.0" is False
    as a string compare.
    """
    parsed = tuple(int(part) for part in __version__.split(".")[:3])
    assert parsed >= (0, 2, 0)


def test_client_seq_continues_above_spool_after_restart(tmp_path):
    """A restarted process must not reuse client_seq values that are still
    sitting in the spool — the hub treats the highest sequence as the
    authoritative cumulative actual, so a reused/lower sequence makes the
    newer report look stale and silently under-reports spend.
    """
    first = _client(tmp_path)
    for i in range(3):
        first.report_actual(
            intent_id="INT-1", job_id="job", actual_calls=(i + 1) * 10,
            actual_cost_usd=(i + 1) * 1.0, status="running", sync=True,
        )
    spooled = _spooled_seqs(tmp_path)
    assert spooled == [1, 2, 3]

    # Simulate a crash + restart: a brand-new client over the same spool dir.
    second = _client(tmp_path)
    second.report_actual(
        intent_id="INT-1", job_id="job", actual_calls=40,
        actual_cost_usd=4.0, status="completed", sync=True,
    )
    assert max(_spooled_seqs(tmp_path)) > max(spooled)


def test_client_seq_is_per_intent(tmp_path):
    """Sequences are tracked per intent_id, not globally."""
    client = _client(tmp_path)
    client.report_actual(intent_id="A", job_id="j", actual_calls=1, sync=True)
    client.report_actual(intent_id="B", job_id="j", actual_calls=1, sync=True)
    by_intent = {}
    for path in glob.glob(os.path.join(str(tmp_path), "*.json")):
        with open(path, encoding="utf-8") as f:
            entry = json.load(f)
        by_intent[entry["payload"]["intent_id"]] = entry["payload"]["client_seq"]
    assert by_intent == {"A": 1, "B": 1}


def test_disabled_client_does_not_spool(tmp_path):
    """A client with no credentials can never deliver, so it must not fill
    the spool with entries that will only ever be evicted by the cap."""
    disabled = _client(tmp_path, token="")
    assert not disabled.enabled
    for i in range(5):
        disabled.report_actual(intent_id="X", job_id="job", actual_calls=i)
    assert glob.glob(os.path.join(str(tmp_path), "*.json")) == []


def test_corrupt_spool_entry_does_not_break_construction(tmp_path):
    """Seeding client_seq happens in __init__, so an unreadable leftover file
    must not stop the client from being built.

    In strict mode the spool's read/list helpers raise, which would otherwise
    turn a stale cache file into a startup crash for the calling application.
    """
    first = _client(tmp_path)
    first.report_actual(intent_id="INT-1", job_id="job", actual_calls=1, sync=True)
    (tmp_path / "9999999999.000000_1_1.json").write_text("{not json", encoding="utf-8")

    for strict in (False, True):
        client = CloudManagementClient(
            project_id="test-project", report_token="test-token",
            base_url=UNREACHABLE, spool_dir=str(tmp_path), strict=strict,
        )
        # The good entry still raised the high-water mark despite the bad one.
        assert client._client_seq.get("INT-1") == 1


def test_spool_can_be_disabled(tmp_path):
    """spool_dir="" disables spooling entirely (read-only filesystems)."""
    client = CloudManagementClient(
        project_id="test-project", report_token="t",
        base_url=UNREACHABLE, spool_dir="",
    )
    client.report_actual(intent_id="Y", job_id="job", actual_calls=1, sync=True)
    assert glob.glob(os.path.join(str(tmp_path), "*.json")) == []


# ---------------------------------------------------------------------------
# Issue #1 parts 3-4: permanent failure handling and head-of-line blocking
# ---------------------------------------------------------------------------

def test_is_permanent_error_classifies_http_codes():
    """4xx (except 408/429) are permanent; 5xx and connection errors are not."""
    import urllib.error
    from cloud_management_client._transport import _is_permanent_error

    # Permanent: 400, 401, 403, 404
    for code in (400, 401, 403, 404):
        exc = urllib.error.HTTPError("url", code, "msg", {}, None)
        assert _is_permanent_error(exc) is True, f"{code} should be permanent"

    # Transient: 408, 429, 500, 502, 503
    for code in (408, 429, 500, 502, 503):
        exc = urllib.error.HTTPError("url", code, "msg", {}, None)
        assert _is_permanent_error(exc) is False, f"{code} should be transient"

    # Connection errors are transient
    assert _is_permanent_error(urllib.error.URLError("conn refused")) is False
    assert _is_permanent_error(Exception("random")) is False


def test_permanent_failure_drops_spool_entry(tmp_path):
    """A 4xx failure should drop the spool entry immediately, not retry 10 times."""
    import urllib.error
    from unittest.mock import patch, MagicMock
    from cloud_management_client.client import CloudManagementClient

    client = CloudManagementClient(
        project_id="test-proj",
        report_token="test-token",
        base_url="http://127.0.0.1:9999",
        spool_dir=str(tmp_path / "spool"),
    )
    # Mock _post_sync_with_error to return a 401 permanent failure
    exc = urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)
    with patch.object(client, "_post_sync_with_error", return_value=(None, exc)):
        entry_id = client._spool.write("/api/v1/actual", {"intent_id": "int_1", "project_id": "test-proj"})
        client._process_spool_entry(entry_id)
        # Entry should be dropped (removed from spool)
        assert client._spool.read(entry_id) is None, "permanent failure should drop spool entry"
    client.close()


def test_transient_failure_re_enqueues_with_due_time(tmp_path):
    """A transient failure should re-enqueue the entry with a due-time, not sleep inline."""
    import urllib.error
    from unittest.mock import patch
    from cloud_management_client.client import CloudManagementClient

    client = CloudManagementClient(
        project_id="test-proj",
        report_token="test-token",
        base_url="http://127.0.0.1:9999",
        spool_dir=str(tmp_path / "spool"),
    )
    # Mock _post_sync_with_error to return a 503 transient failure
    exc = urllib.error.HTTPError("url", 503, "Service Unavailable", {}, None)
    with patch.object(client, "_post_sync_with_error", return_value=(None, exc)):
        entry_id = client._spool.write("/api/v1/actual", {"intent_id": "int_2", "project_id": "test-proj"})
        client._process_spool_entry(entry_id)
        # Entry should still be in the spool (not dropped)
        assert client._spool.read(entry_id) is not None, "transient failure should keep spool entry"
        # A retry tuple should be in the queue
        item = client._queue.get_nowait()
        assert isinstance(item, tuple), "retry should be enqueued as (entry_id, due_time) tuple"
        assert item[0] == entry_id
        assert item[1] > 0  # due_time is a monotonic timestamp
    client.close()
