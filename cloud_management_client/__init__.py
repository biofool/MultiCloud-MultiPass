"""CloudManagement intent/actual reporting client.

A lightweight, stdlib-only client that sub-projects use to declare
expected API usage before making calls and report actuals after (or
incrementally during long jobs).  CloudManagement validates actual vs
intent, detects overruns, and can kill the specific job that is
accumulating cost.

Typical usage:

    from cloud_management_client import CloudManagementClient

    cb = CloudManagementClient(
        project_id="aisuppportvigilent",
        report_token=os.environ["CLOUDMANAGEMENT_REPORT_TOKEN_AISUPPPORTVIGILENT"],
        # base_url defaults to http://127.0.0.1:8080 for local dev;
        # set to the Cloud Run URL in production.
    )

    # Declare intent before a batch of API calls (synchronous — needs
    # the response to check approval)
    intent = cb.declare_intent(
        job_id="gemini-session-abc123",
        job_name="coaching-chat-batch",
        provider="google",
        api="gemini-3.1-flash-lite",
        expected_calls=500,
        expected_cost_usd=2.50,
        rate_limit_rpm=10,
        source_repo="AISuppportVigilent",
    )
    if not intent.approved:
        raise RuntimeError(f"intent denied: {intent.reason}")

    # ... make API calls ...

    # Report actual (incremental — fire-and-forget via background thread
    # so it never blocks the caller)
    cb.report_actual(
        intent_id=intent.intent_id,
        job_id="gemini-session-abc123",
        provider="google",
        api="gemini-3.1-flash-lite",
        actual_calls=150,
        actual_cost_usd=0.75,
        status="running",   # "running" | "completed" | "failed"
    )

    # Final report when done (synchronous to ensure delivery)
    cb.report_actual(
        intent_id=intent.intent_id,
        job_id="gemini-session-abc123",
        provider="google",
        api="gemini-3.1-flash-lite",
        actual_calls=500,
        actual_cost_usd=2.50,
        status="completed",
        sync=True,           # wait for the HTTP response
    )
    cb.flush()  # wait for any pending async reports to drain

The client is also usable as a context manager for automatic final
actual reporting:

    with cb.intent(
        job_id="scrape-phase1-la",
        provider="google",
        api="places-text-search",
        expected_calls=312,
        expected_cost_usd=10.0,
    ) as ctx:
        for query in queries:
            result = call_api(query)
            ctx.add_calls(1, cost_usd=0.032)
        # ctx reports "completed" on exit (or "failed" on exception)

Configuration via environment variables (all optional — constructor
args take precedence):
    CLOUDMANAGEMENT_URL          Base URL of the CloudManagement service
                                 (use the stable hostname, e.g.
                                 https://cloud.magicsolutions.biz)
    CLOUDMANAGEMENT_PROJECT_ID   Default project_id for this repo
    CLOUDMANAGEMENT_REPORT_TOKEN Default report token
    CLOUDMANAGEMENT_GATE_TOKEN   Shared secret for the Cloudflare Worker auth
                                 gate (sent as X-Gate-Token header). Required
                                 when base_url is the stable hostname
                                 cloud.magicsolutions.biz and the Worker has
                                 GATE_TOKEN configured. Stored in GCP Secret
                                 Manager as CLOUDMANAGEMENT_GATE_TOKEN.
    CLOUDMANAGEMENT_APPLICATION  Human-readable name of the calling app
                                 (e.g. "OSenseiArchiver"); recorded on
                                 every intent/actual report for attribution.
                                 Distinct from source_repo (the GitHub repo).
    CLOUDMANAGEMENT_TIMEOUT      HTTP timeout in seconds (default 5)
    CLOUDMANAGEMENT_INTENT_TIMEOUT  Timeout for declare_intent (default 3)
    CLOUDMANAGEMENT_STRICT       If "true", raise on errors instead of logging
    CLOUDMANAGEMENT_SPOOL_DIR    Directory for the durable on-disk spool
                                 (default: ~/.cache/cloud_management_client/spool).
                                 Set to empty string to disable spooling
                                 (read-only filesystems). See issue #12.
    CLOUDMANAGEMENT_SPOOL_CAP    Max spool entries before oldest are dropped
                                 with an ERROR log (default: 1000).
    CLOUDMANAGEMENT_SPOOL_MAX_ATTEMPTS  Max delivery attempts per entry
                                 before it is dropped with an ERROR (default: 10).
    CLOUDMANAGEMENT_SPOOL_MAX_AGE_SECONDS  Max age in seconds before an entry
                                 is dropped even if attempts remain (default: 86400).
    CLOUDMANAGEMENT_USE_IDENTITY If "true", fetch a GCP OIDC ID token from the
                                 metadata server and use it as the bearer
                                 credential instead of a shared report_token
                                 (issue #10). Falls back to report_token when
                                 not on GCP (local dev, OpenStack).

Errors are logged at WARNING and never raised — billing reporting is
best-effort and must not break the host application.  Set
CLOUDMANAGEMENT_STRICT=true to raise on errors instead.
"""

from __future__ import annotations

from .client import CloudManagementClient
from .context import IntentContext
from .errors import CloudManagementError, JobKilledError, KillOrder
from .errors import log as log  # re-exported as a module attribute, as before the split
from .models import ActualResponse, BudgetCheck, IntentResponse

__all__ = [
    "CloudManagementClient",
    "CloudManagementError",
    "IntentResponse",
    "ActualResponse",
    "BudgetCheck",
    "IntentContext",
    "JobKilledError",
    "KillOrder",
]

__version__ = "0.12.0"
