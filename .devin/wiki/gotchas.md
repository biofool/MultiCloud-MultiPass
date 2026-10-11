# Gotchas — MultiCloud-MultiPass

2026-10 security audit: `providers/http_callback.py` + `providers/gcp.py` fixes, spool/transport/lifecycle hardening in `cloud_management_client` package, workflow pins, non-root Dockerfile.

## Standing rules
- Never commit secrets (`.env`, `*.key`, `credentials*.json` are gitignored).
- Never read or log secret values — key names and lengths only.
- Security fixes land reviewed-but-uncommitted first, then roll dev → main.
