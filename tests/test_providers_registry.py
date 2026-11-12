"""Tests for the registry.Account multi-cloud extension fields.

Split out of ``test_providers.py`` during the ≤250-line refactor — pure
structural move, no behavior change.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestRegistryExtension:
    def test_account_multi_cloud_fields(self):
        import registry
        acct = registry.Account(
            project_id="test-os",
            cloud="openstack",
            openstack_project="454-123",
            openstack_regions=["your-region-1"],
            budget_amount_usd=20,
        )
        d = acct.to_dict()
        assert d["cloud"] == "openstack"
        assert d["openstack_project"] == "454-123"
        assert d["openstack_regions"] == ["your-region-1"]

        # Round-trip
        acct2 = registry.Account.from_dict(d)
        assert acct2.cloud == "openstack"
        assert acct2.openstack_project == "454-123"

    def test_account_gcp_with_jobs(self):
        import registry
        acct = registry.Account(
            project_id="test-gcp",
            cloud="gcp",
            gcp_project_id="my-gcp-project",
            jobs=[{"job_id_prefix": "scrape-", "kill": {"type": "http_callback", "url": "http://x"}}],
        )
        d = acct.to_dict()
        assert d["gcp_project_id"] == "my-gcp-project"
        assert len(d["jobs"]) == 1

        acct2 = registry.Account.from_dict(d)
        assert acct2.gcp_project_id == "my-gcp-project"
        assert acct2.jobs[0]["job_id_prefix"] == "scrape-"

    def test_account_defaults_to_gcp(self):
        import registry
        acct = registry.Account(project_id="test")
        assert acct.cloud == "gcp"
        assert acct.gcp_project_id == ""

    def test_account_aws_fields(self):
        import registry
        acct = registry.Account(
            project_id="test-aws",
            cloud="aws",
            aws_account_id="123456789012",
            aws_region="us-east-1",
            aws_access_key_secret="AWS_ACCESS_KEY_ID",
            budget_amount_usd=600,
            jobs=[{"job_id_prefix": "gpu-", "kill": {"type": "ec2", "instance_id": "i-abc", "action": "stop"}}],
        )
        d = acct.to_dict()
        assert d["cloud"] == "aws"
        assert d["aws_account_id"] == "123456789012"
        assert d["aws_region"] == "us-east-1"

        acct2 = registry.Account.from_dict(d)
        assert acct2.cloud == "aws"
        assert acct2.aws_account_id == "123456789012"
        assert acct2.aws_access_key_secret == "AWS_ACCESS_KEY_ID"
        assert acct2.jobs[0]["kill"]["type"] == "ec2"
