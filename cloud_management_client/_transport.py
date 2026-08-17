"""Low-level HTTP transport for CloudManagementClient: identity/bearer
auth and the synchronous GET/POST helpers used by every API method.

One of several mixins that ``client.CloudManagementClient`` combines —
split out of the original monolithic class for readability. Pure
structural move: every method here behaves identically to before, just
relocated. See ``client.py`` for how the mixins are assembled.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .errors import _fail, log


class _TransportMixin:
    """Identity token / bearer auth plus synchronous GET and POST."""

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------

    def _get_identity_token(self) -> str | None:
        """Fetch a GCP OIDC ID token scoped to base_url (issue #10).

        Uses the metadata server's identity endpoint with the hub's URL as
        the audience. The token is cached until 60s before expiry. Returns
        None if not on GCP or the metadata server is unreachable — the
        caller falls back to the shared report_token in that case.
        """
        # Return cached token if still valid (with a 60s safety margin).
        if self._id_token and time.time() < self._id_token_expiry - 60:
            return self._id_token
        try:
            audience = urllib.parse.quote(self.base_url, safe="")
            url = (
                "http://metadata.google.internal/computeMetadata/v1/instance/"
                f"service-accounts/default/identity?audience={audience}"
            )
            req = urllib.request.Request(url, headers={"Metadata-Flavor": "Google"})
            with urllib.request.urlopen(req, timeout=2) as resp:
                token = resp.read().decode("utf-8").strip()
                if not token:
                    return None
                # Decode the JWT payload to get the expiry (no verification —
                # the metadata server is trusted in this context).
                try:
                    payload_b64 = token.split(".")[1]
                    # Add padding for base64 decode
                    payload_b64 += "=" * (4 - len(payload_b64) % 4)
                    payload = json.loads(
                        __import__("base64").urlsafe_b64decode(payload_b64)
                    )
                    self._id_token_expiry = float(payload.get("exp", 0))
                except Exception as e:
                    # If we can't decode the expiry, assume a short lifetime.
                    log.warning(
                        "cloud_management: could not decode identity token expiry, "
                        "assuming 300s: %s", e
                    )
                    self._id_token_expiry = time.time() + 300
                self._id_token = token
                return token
        except Exception as e:
            log.warning("cloud_management: identity token fetch failed: %s", e)
            return None

    def _auth_token(self) -> str:
        """Return the bearer token for the current request.

        In identity mode, fetches an ID token from the metadata server; if
        that fails (not on GCP), falls back to the shared report_token.
        In shared-token mode, returns the report_token directly.
        """
        if self.use_identity:
            token = self._get_identity_token()
            if token:
                return token
            # Fall back to shared token (local dev, OpenStack).
            log.warning("cloud_management: identity token unavailable, falling back to report_token")
        return self.report_token

    def _gate_headers(self) -> dict[str, str]:
        """Return extra headers for the Cloudflare Worker auth gate.

        When ``gate_token`` is set, the Worker in front of
        cloud.magicsolutions.biz requires ``X-Gate-Token`` on every request
        (except GET /health). This header is ignored by the hub's
        application layer — it's only consumed by the Worker.

        Returns an empty dict when no gate token is configured (local dev,
        direct Cloud Run access, etc.).
        """
        if self.gate_token:
            return {"X-Gate-Token": self.gate_token}
        return {}

    def _post_sync(self, path: str, payload: dict[str, Any], timeout: int | None = None) -> dict[str, Any] | None:
        """Synchronous HTTP POST. Returns parsed JSON or None on error."""
        if not self.enabled:
            return None
        url = f"{self.base_url}{path}"
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._auth_token()}",
        }
        headers.update(self._gate_headers())
        req = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method="POST",
        )
        t = timeout if timeout is not None else self.timeout
        try:
            with urllib.request.urlopen(req, timeout=t) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8")[:200]
            except Exception:
                pass
            _fail(f"POST {path} failed (HTTP {e.code}): {err_body}", strict=self.strict)
            return None
        except urllib.error.URLError as e:
            _fail(f"POST {path} connection error", e, strict=self.strict)
            return None
        except Exception as e:
            _fail(f"POST {path} unexpected error", e, strict=self.strict)
            return None

    def _get_sync(self, path: str, timeout: int | None = None) -> dict[str, Any] | None:
        """Synchronous HTTP GET. Returns parsed JSON or None on error."""
        if not self.enabled:
            return None
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._auth_token()}",
        }
        headers.update(self._gate_headers())
        req = urllib.request.Request(
            url,
            headers=headers,
            method="GET",
        )
        t = timeout if timeout is not None else self.timeout
        try:
            with urllib.request.urlopen(req, timeout=t) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8")[:200]
            except Exception:
                pass
            _fail(f"GET {path} failed (HTTP {e.code}): {err_body}", strict=self.strict)
            return None
        except urllib.error.URLError as e:
            _fail(f"GET {path} connection error", e, strict=self.strict)
            return None
        except Exception as e:
            _fail(f"GET {path} unexpected error", e, strict=self.strict)
            return None
