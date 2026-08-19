"""Public CloudManagementClient class.

The class itself is assembled from focused mixins living alongside this
file (``_lifecycle.py``, ``_transport.py``, ``_intent_ops.py``,
``_actual_ops.py``) — pure structural move from the original monolithic
implementation, no behavior change. Splitting the mixins out keeps each
file scoped to one cohesive responsibility while ``CloudManagementClient``
itself remains a single class with exactly the same attributes and
methods as before.
"""

from __future__ import annotations

from ._actual_ops import _ActualOpsMixin
from ._intent_ops import _IntentOpsMixin
from ._lifecycle import _LifecycleMixin
from ._transport import _TransportMixin


class CloudManagementClient(
    _LifecycleMixin,
    _TransportMixin,
    _IntentOpsMixin,
    _ActualOpsMixin,
):
    """Client for the CloudManagement intent/actual reporting protocol.

    All methods are best-effort: errors are logged at WARNING and the
    method returns a failure response object rather than raising
    (unless CLOUDMANAGEMENT_STRICT=true).

    ``report_actual`` is asynchronous by default — it enqueues the HTTP
    request to a background daemon thread so it never blocks the caller.
    Use ``sync=True`` for the final "completed"/"failed" report, and call
    ``flush()`` to wait for pending async reports to drain (e.g. at
    shutdown).

    ``declare_intent`` is always synchronous because the caller needs
    the response to check ``.approved``.
    """
