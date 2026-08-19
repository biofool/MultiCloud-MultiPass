"""Per-project bearer token validation for the intent/actual protocol.

Split out of the original monolithic ``intent.py`` — pure structural
move, no behavior change. ``GLOBAL_REPORT_TOKEN`` is env-derived and
reload-sensitive (see intent_storage.py's docstring), hence the
``_intent_mod`` qualified access.
"""

from __future__ import annotations

import os
import secrets

import flask

import intent as _intent_mod
import registry

# ---------------------------------------------------------------------------
# Auth — per-project report token validation
# ---------------------------------------------------------------------------

def _validate_token(project_id: str) -> bool:
    """Validate the bearer token against the project's configured token.

    Per-project tokens take precedence (from the registry's
    report_token_secret); the global CLOUDMANAGEMENT_REPORT_TOKEN is a
    fallback for dev/test.
    """
    auth_header = flask.request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False
    token = auth_header[7:]

    acct = registry.get_account(project_id)
    if acct and acct.report_token_secret:
        # In production this is a Secret Manager ref; in dev it's an env var name.
        # For now, look it up as an env var.
        expected = os.environ.get(acct.report_token_secret, "")
        if expected and secrets.compare_digest(token, expected):
            return True

    if _intent_mod.GLOBAL_REPORT_TOKEN and secrets.compare_digest(token, _intent_mod.GLOBAL_REPORT_TOKEN):
        return True

    return False
