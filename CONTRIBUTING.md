# Contributing to CloudManagement

Thank you for your interest in contributing! CloudManagement is a multi-cloud cost kill switch and intent/actual reporting hub. Contributions are welcome.

## License

By contributing, you agree that your contributions will be licensed under the [AGPL 3.0](LICENSE) license.

## Getting started

```bash
git clone https://github.com/biofool/MultiCloud-MultiPass.git
cd CloudManagement
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/ -v
```

## Development workflow

1. **Fork the repo** and create a feature branch (`git checkout -b feat/my-feature`)
2. **Write tests** for your changes — all GCP clients are mocked in the test suite, no live API calls needed
3. **Run tests**: `pytest tests/ -v`
4. **Run the linter** if you have one configured (ruff/black recommended)
5. **Commit** with a clear message describing what and why
6. **Open a pull request** — describe the change, link any related issues

## Code style

- Follow existing conventions in the codebase
- Compact code — avoid unnecessary nesting and duplicate branches
- Don't add/remove comments unless asked
- Handle errors at the right boundary — not every line needs try/catch
- Never fail silently — log every exception at WARNING/ERROR level

## Architecture overview

Read the [README](README.md) for the full architecture. Key points:

- **`main.py`** — Flask app deployed to Cloud Run; handles budget alerts, quota polling, and intent/actual endpoints
- **`registry.py`** — account registry (Firestore in prod, YAML in dev)
- **`intent.py`** — intent/actual reporting protocol (real-time cost tracking)
- **`poller.py`** — quota-based real-time kill switch
- **`providers/`** — pluggable cloud provider adapters (GCP, OpenStack, Cloudflare, HTTP callback)
- **`paths.py`** — project-root path resolution (all config/data paths resolve via this module)
- **`cloud_management_client/`** — pip-installable stdlib-only client for sub-projects

## Adding a new cloud provider

1. Create `providers/yourcloud.py` implementing `CostProvider` from `providers/base.py`
2. Implement `fetch_billed_costs()` (reconciliation tier) and `kill_job()` (per-job kill)
3. Register it in `providers/registry.py`
4. Add tests in `tests/test_providers.py`
5. Update `docs/PRD.md` with the new provider's free-tier details

## Reporting security issues

**Do not open a public issue for security vulnerabilities.** Instead, email the maintainer directly with details. Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

## Code of conduct

Be respectful, constructive, and inclusive. Disagreements happen — focus on the technical merits, not the person.
