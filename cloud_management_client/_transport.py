"""Low-level HTTP transport for CloudManagementClient: identity/bearer
auth and the synchronous GET/POST helpers used by every API method.

One of several mixins that ``client.CloudManagementClient`` combines —
split out of the original monolithic class for readability. See
``client.py`` for how the mixins are assembled.

Issue #1 parts 3-4: ``_post_sync`` and ``_get_sync`` now distinguish
permanent failures (HTTP 4xx except 408/429) from transient ones (5xx,
connection errors, 408, 429) via the ``_PERMANENT_HTTP_CODES`` set.
Callers that retry (the spool worker) use ``_is_permanent_error`` to
skip retrying reports that can never succeed.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .errors import _fail, log

# HTTP status codes that indicate a permanent failure — retrying won't
# help. 408 (Request Timeout) and 429 (Too Many Requests) are transient
# despite being 4xx. See issue #1 part 4.
_PERMANENT_HTTP_CODES = frozenset(
    {400, 401, 403, 404, 405, 409, 410, 411, 412, 413, 414, 415, 422}
)


def _is_permanent_error(exc: Exception) -> bool:
    """Return True if ``exc`` is an HTTPError with a permanent status code.

    Used by the spool worker to skip retrying reports that can never
    succeed (e.g. 400 Bad Request, 401 Unauthorized, 403 Forbidden).
    Connection errors and 5xx/408/429 are transient and will be retried.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in _PERMANENT_HTTP_CODES
    return False


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
        """Synchronous HTTP POST. Returns parsed JSON or None on error.

        On failure, the exception is stored in ``self._last_exc`` so the
        spool worker can check ``_is_permanent_error`` to decide whether
        to retry (issue #1 part 4).
        """
        data, exc = self._post_sync_with_error(path, payload, timeout=timeout)
        self._last_exc = exc
        return data

    def _post_sync_with_error(self, path: str, payload: dict[str, Any], timeout: int | None = None) -> tuple[dict[str, Any] | None, Exception | None]:
        """Synchronous HTTP POST. Returns (parsed JSON or None, exception or None).

        On success the exception is None. On failure the data is None and
        the exception is the raised error — the caller can inspect it
        with ``_is_permanent_error`` to decide whether to retry.
        """
        if not self.enabled:
            return None, None
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
                return json.loads(resp.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8")[:200]
            except Exception:
                pass
            _fail(f"POST {path} failed (HTTP {e.code}): {err_body}", strict=self.strict)
            return None, e
        except urllib.error.URLError as e:
            _fail(f"POST {path} connection error", e, strict=self.strict)
            return None, e
        except Exception as e:
            _fail(f"POST {path} unexpected error", e, strict=self.strict)
            return None, e

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
