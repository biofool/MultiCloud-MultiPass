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


def test_spool_can_be_disabled(tmp_path):
    """spool_dir="" disables spooling entirely (read-only filesystems)."""
    client = CloudManagementClient(
        project_id="test-project", report_token="t",
        base_url=UNREACHABLE, spool_dir="",
    )
    client.report_actual(intent_id="Y", job_id="job", actual_calls=1, sync=True)
    assert glob.glob(os.path.join(str(tmp_path), "*.json")) == []
