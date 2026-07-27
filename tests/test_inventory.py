"""Tests for the inventory module."""

import json
import os
import importlib
import tempfile
import pytest

import registry
import inventory


@pytest.fixture
def temp_accounts(tmp_path, monkeypatch):
    """Create a temporary accounts.yaml for testing."""
    accounts_yaml = tmp_path / "accounts.yaml"
    accounts_yaml.write_text("""
accounts:
  - project_id: test-hub
    cloud: gcp
    gcp_project_id: test-hub-123
    billing_account_id: "AAAA-BBBB-CCCC"
    owner_email: owner@example.com
    allowlist: true
    budget_amount_usd: 10
    quota_rpm_cap: 6000
  - project_id: test-worker
    cloud: gcp
    gcp_project_id: test-worker-456
    billing_account_id: "AAAA-BBBB-CCCC"
    owner_email: worker@example.com
    allowlist: false
    budget_amount_usd: 5
    quota_rpm_cap: 3000
""")
    monkeypatch.setenv("USE_FIRESTORE", "false")
    monkeypatch.setenv("ACCOUNTS_FILE", str(accounts_yaml))
    # Reload registry so it picks up the new env vars
    importlib.reload(registry)
    importlib.reload(inventory)
    return accounts_yaml


@pytest.fixture
def temp_tfstate(tmp_path, monkeypatch):
    """Create a minimal terraform.tfstate for testing."""
    tfstate = {
        "version": 4,
        "terraform_version": "1.5.0",
        "resources": [
            {
                "type": "google_cloud_run_service",
                "name": "killswitch",
                "instances": [{
                    "attributes": {
                        "name": "cost-killswitch",
                        "location": "us-central1",
                        "project": "test-hub-123",
                    }
                }]
            },
            {
                "type": "google_bigquery_dataset",
                "name": "billing_export",
                "instances": [{
                    "attributes": {
                        "dataset_id": "cloud_billing_export",
                        "location": "US",
                        "project": "test-hub-123",
                    }
                }]
            },
            {
                "type": "google_storage_bucket",
                "name": "corpus",
                "instances": [{
                    "attributes": {
                        "name": "test-corpus-bucket",
                        "location": "us-central1",
                        "project": "test-worker-456",
                    }
                }]
            },
            {
                "type": "google_compute_instance",
                "name": "untracked",
                "instances": [{
                    "attributes": {
                        "name": "vm-1",
                        "project": "test-worker-456",
                    }
                }]
            },
        ],
    }
    tf_dir = tmp_path / "terraform"
    tf_dir.mkdir()
    (tf_dir / "terraform.tfstate").write_text(json.dumps(tfstate))
    monkeypatch.setenv("TERRAFORM_DIR", str(tf_dir))
    return tf_dir


class TestBuildInventory:
    def test_returns_accounts_and_resources(self, temp_accounts, temp_tfstate):
        inv = inventory.build_inventory()
        assert "accounts" in inv
        assert "resources" in inv
        assert "summary" in inv

    def test_account_count(self, temp_accounts, temp_tfstate):
        inv = inventory.build_inventory()
        assert inv["summary"]["account_count"] == 2

    def test_resource_count_excludes_untracked_types(self, temp_accounts, temp_tfstate):
        """google_compute_instance is not in type_map, so it should be excluded."""
        inv = inventory.build_inventory()
        # cloud_run + bigquery_dataset + storage_bucket = 3
        assert inv["summary"]["resource_count"] == 3

    def test_resources_associated_with_accounts(self, temp_accounts, temp_tfstate):
        inv = inventory.build_inventory()
        by_type = {r["type"]: r for r in inv["resources"]}
        # Cloud Run and BigQuery belong to the hub project
        assert by_type["cloud_run"]["account_id"] == "test-hub"
        assert by_type["bigquery_dataset"]["account_id"] == "test-hub"
        # Storage bucket belongs to the worker project
        assert by_type["storage_bucket"]["account_id"] == "test-worker"

    def test_summary_by_cloud(self, temp_accounts, temp_tfstate):
        inv = inventory.build_inventory()
        assert inv["summary"]["by_cloud"].get("gcp") == 3

    def test_summary_by_type(self, temp_accounts, temp_tfstate):
        inv = inventory.build_inventory()
        by_type = inv["summary"]["by_type"]
        assert by_type.get("cloud_run") == 1
        assert by_type.get("bigquery_dataset") == 1
        assert by_type.get("storage_bucket") == 1

    def test_no_tfstate_returns_empty_resources(self, temp_accounts, tmp_path, monkeypatch):
        """When no terraform.tfstate exists, resources should be empty but accounts present."""
        monkeypatch.setenv("TERRAFORM_DIR", str(tmp_path / "nonexistent"))
        inv = inventory.build_inventory()
        assert inv["summary"]["resource_count"] == 0
        assert inv["summary"]["account_count"] == 2

    def test_malformed_tfstate_logged_and_skipped(self, temp_accounts, tmp_path, monkeypatch):
        """Malformed tfstate should not crash — logged and returns empty resources."""
        tf_dir = tmp_path / "terraform"
        tf_dir.mkdir()
        (tf_dir / "terraform.tfstate").write_text("not valid json {{{")
        monkeypatch.setenv("TERRAFORM_DIR", str(tf_dir))
        inv = inventory.build_inventory()
        assert inv["summary"]["resource_count"] == 0
        assert inv["summary"]["account_count"] == 2


class TestInventoryEndpoint:
    def test_get_inventory_returns_200(self, temp_accounts, temp_tfstate):
        import flask
        from inventory import bp as inventory_bp
        app = flask.Flask(__name__)
        app.register_blueprint(inventory_bp)
        with app.test_client() as client:
            resp = client.get("/api/v1/inventory")
            assert resp.status_code == 200
            data = resp.get_json()
            assert "accounts" in data
            assert "resources" in data
            assert "summary" in data
