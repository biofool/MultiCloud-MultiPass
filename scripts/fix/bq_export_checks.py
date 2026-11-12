"""Read-only Google API checks for the BigQuery Cloud Billing export.

Split out of ``check_bq_billing_export.py`` during the ≤250-line
refactor — pure structural move, no behavior change. These are the
individual verification functions; the CLI entry point and report
formatting stay in ``check_bq_billing_export.py``.

No third-party deps required — urllib is used so this runs without the
venv. google-cloud-* libs are only needed by the service itself.
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error
import urllib.parse


def _get_access_token() -> str:
    """Get an access token via gcloud."""
    import subprocess
    result = subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _api_get(url: str, token: str, headers: dict | None = None) -> dict:
    """GET a Google API endpoint and return parsed JSON."""
    if urllib.parse.urlsplit(url).scheme != "https":
        return {"_error": True, "status": 0, "body": "refused non-https url"}
    hdrs = {"Authorization": f"Bearer {token}"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"_error": True, "status": exc.code, "body": exc.read().decode("utf-8", errors="replace")}


def _api_post(url: str, token: str, body: dict, headers: dict | None = None) -> dict:
    """POST a Google API endpoint and return parsed JSON."""
    if urllib.parse.urlsplit(url).scheme != "https":
        return {"_error": True, "status": 0, "body": "refused non-https url"}
    hdrs = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"_error": True, "status": exc.code, "body": exc.read().decode("utf-8", errors="replace")}


def check_apis(project: str, token: str) -> dict:
    """Check required APIs are enabled."""
    required = ["cloudbilling.googleapis.com", "bigquery.googleapis.com", "bigquerydatatransfer.googleapis.com"]
    url = f"https://serviceusage.googleapis.com/v1/projects/{project}/services:batchGet"
    # batchGet takes ?names= repeated; easier to list and filter
    url = f"https://serviceusage.googleapis.com/v1/projects/{project}/services?filter=state:ENABLED&pageSize=200"
    data = _api_get(url, token)
    if "_error" in data:
        return {"ok": False, "error": data, "apis": {}}
    enabled = {s["config"]["name"].split("/")[-1] for s in data.get("services", [])}
    return {
        "ok": True,
        "apis": {api: {"enabled": api in enabled} for api in required},
        "all_enabled": all(api in enabled for api in required),
    }


def check_dataset(project: str, dataset: str, token: str) -> dict:
    """Check the BigQuery dataset exists."""
    url = f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}/datasets/{dataset}"
    data = _api_get(url, token)
    if "_error" in data:
        return {"ok": False, "exists": False, "error": data.get("body", "")[:200]}
    return {
        "ok": True,
        "exists": True,
        "id": data.get("id"),
        "location": data.get("location"),
    }


def check_runtime_sa(project: str, token: str) -> dict:
    """Check the runtime service account exists and has required IAM roles."""
    sa_email = f"killswitch-rt@{project}.iam.gserviceaccount.com"
    sa_member = f"serviceAccount:{sa_email}"
    # Check SA exists
    url = f"https://iam.googleapis.com/v1/projects/{project}/serviceAccounts/{sa_email}"
    data = _api_get(url, token)
    if "_error" in data:
        return {"ok": False, "exists": False, "email": sa_email, "error": data.get("body", "")[:200]}
    # Check IAM roles
    required_roles = ["roles/bigquery.dataViewer", "roles/bigquery.jobUser", "roles/datastore.user", "roles/pubsub.subscriber"]
    iam_url = f"https://cloudresourcemanager.googleapis.com/v1/projects/{project}:getIamPolicy"
    iam_data = _api_post(iam_url, token, {})
    granted = set()
    if not iam_data.get("_error"):
        for binding in iam_data.get("bindings", []):
            if sa_member in binding.get("members", []):
                granted.add(binding["role"])
    return {
        "ok": True,
        "exists": True,
        "email": sa_email,
        "roles": {r: {"granted": r in granted} for r in required_roles},
        "all_roles_granted": all(r in granted for r in required_roles),
    }


def check_export_tables(project: str, dataset: str, billing_accounts: list[str], token: str) -> dict:
    """Check that billing export tables exist in the dataset."""
    url = f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}/datasets/{dataset}/tables"
    data = _api_get(url, token)
    if "_error" in data:
        return {"ok": False, "error": data.get("body", "")[:200], "tables": []}
    tables = data.get("tables", [])
    table_ids = [t["id"] for t in tables]
    # Expected table names: one per billing account
    # gcp_billing_export_resource_v1_<account_id_with_underscores>
    expected = {}
    for ba in billing_accounts:
        suffix = ba.replace("-", "_")
        expected_name = f"gcp_billing_export_resource_v1_{suffix}"
        expected[ba] = {
            "expected_table": expected_name,
            "found": any(expected_name in tid for tid in table_ids),
        }
    return {
        "ok": True,
        "table_count": len(tables),
        "tables": table_ids,
        "expected": expected,
        "any_enabled": any(v["found"] for v in expected.values()),
    }


def check_table_rows(project: str, dataset: str, billing_accounts: list[str], token: str) -> dict:
    """Check if export tables have rows (data flowing). Uses a dry-run query."""
    results = {}
    for ba in billing_accounts:
        suffix = ba.replace("-", "_")
        table = f"gcp_billing_export_resource_v1_{suffix}"
        # Use jobs.query with dryRun to get table metadata without scanning
        # Actually, use tables.get for numRows
        url = f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}/datasets/{dataset}/tables/{table}"
        data = _api_get(url, token)
        if "_error" in data:
            results[ba] = {"exists": False, "num_rows": 0}
        else:
            results[ba] = {
                "exists": True,
                "num_rows": int(data.get("numRows", 0)),
                "creation_time": data.get("creationTime"),
            }
    return {"ok": True, "tables": results}
