"""
Tests for the account registry (YAML backend).

Run locally:
  cd ~/projects/CloudManagement
  pytest tests/ -v

These tests use the local YAML backend only — no Firestore calls.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import registry  # noqa: E402


@pytest.fixture
def yaml_registry(tmp_path, monkeypatch):
    accounts_file = tmp_path / "accounts.yaml"
    monkeypatch.setattr(registry, "USE_FIRESTORE", False)
    monkeypatch.setattr(registry, "ACCOUNTS_FILE", str(accounts_file))
    return accounts_file


class TestYamlBackend:
    def test_list_accounts_empty_when_file_missing(self, yaml_registry):
        assert registry.list_accounts() == []

    def test_register_and_list(self, yaml_registry):
        acct = registry.Account(
            project_id="proj-a",
            billing_account_id="01AB-23CD-EF45",
            owner_email="a@example.com",
            budget_amount_usd=10,
        )
        registry.register_account(acct)
        accounts = registry.list_accounts()
        assert len(accounts) == 1
        assert accounts[0].project_id == "proj-a"
        assert accounts[0].budget_amount_usd == 10

    def test_register_upserts_existing(self, yaml_registry):
        registry.register_account(registry.Account(project_id="proj-a", billing_account_id="x", owner_email="a@example.com", budget_amount_usd=10))
        registry.register_account(registry.Account(project_id="proj-a", billing_account_id="x", owner_email="a@example.com", budget_amount_usd=25))
        accounts = registry.list_accounts()
        assert len(accounts) == 1
        assert accounts[0].budget_amount_usd == 25

    def test_get_account_found_and_missing(self, yaml_registry):
        registry.register_account(registry.Account(project_id="proj-a", billing_account_id="x", owner_email="a@example.com"))
        assert registry.get_account("proj-a") is not None
        assert registry.get_account("does-not-exist") is None

    def test_is_allowlisted(self, yaml_registry):
        registry.register_account(registry.Account(project_id="proj-safe", billing_account_id="x", owner_email="a@example.com", allowlist=True))
        registry.register_account(registry.Account(project_id="proj-risky", billing_account_id="x", owner_email="b@example.com", allowlist=False))
        assert registry.is_allowlisted("proj-safe") is True
        assert registry.is_allowlisted("proj-risky") is False
        assert registry.is_allowlisted("unregistered") is False

    def test_multiple_accounts_preserved(self, yaml_registry):
        registry.register_account(registry.Account(project_id="proj-a", billing_account_id="x", owner_email="a@example.com"))
        registry.register_account(registry.Account(project_id="proj-b", billing_account_id="x", owner_email="b@example.com"))
        ids = {a.project_id for a in registry.list_accounts()}
        assert ids == {"proj-a", "proj-b"}
