import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Isolate every test: no inherited env, fresh caches/registry."""
    for var in (
        "SECRETS_BACKEND", "USE_SECRET_MANAGER", "SECRETS_PREFER_REMOTE",
        "GCP_SECRET_PROJECT", "SECRET_PROJECT_ID", "GCP_PROJECT_ID",
        "GOOGLE_CLOUD_PROJECT", "PROJECT_ID", "BIOFOOL_SECRETS_PROJECT",
        "BIOFOOL_SECRETS_ALIASES_FILE", "BIOFOOL_SECRETS_DEPRECATED_FILE",
        "PROTON_PASS_ENABLED", "PROTON_PASS_AGENT_TOKEN",
        "SECRET_LOADER_CACHE_TTL", "SECRET_LOADER_NEG_CACHE_TTL",
    ):
        monkeypatch.delenv(var, raising=False)
    import biofool_secrets
    biofool_secrets.reset_caches()
    yield
    biofool_secrets.reset_caches()
