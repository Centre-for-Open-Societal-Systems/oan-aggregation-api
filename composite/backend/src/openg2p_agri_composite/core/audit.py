"""Fire-and-forget CloudEvents to the OpenG2P Audit Manager.

Off when the URL is empty. Never blocks a request and never raises: events are
posted from background tasks and failures are logged at WARNING. Events carry
outcomes and identifiers of the request, never data or subject identifiers.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

_logger = logging.getLogger("agri_composite.audit")

EVENTS_PATH = "/v1/auditmanager/events"
MAX_IN_FLIGHT = 1000


class AuditEmitter:
    def __init__(self, url: str, client_getter: Callable, timeout: float = 2.0, source: str = "/openg2p/agri-composite"):
        self.url = (url or "").rstrip("/")
        self.enabled = bool(self.url)
        self._client_getter = client_getter
        self.timeout = timeout
        self.source = source
        self._tasks: set = set()

    def emit(
        self,
        kind: str,  # request | source_call | response
        *,
        request_id: str,
        partner_id: Optional[str],
        use_case: Optional[str],
        outcome: str,  # success | failure | denied
        reason: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.enabled:
            return
        try:
            if len(self._tasks) >= MAX_IN_FLIGHT:
                _logger.warning("Audit backlog full (%d in flight); dropping a %s event", len(self._tasks), kind)
                return
            event = {
                "specversion": "1.0",
                "id": str(uuid.uuid4()),
                "source": self.source,
                "type": f"org.openg2p.agri-composite.{kind}",
                "subject": f"request/{request_id}",
                "time": datetime.now(tz=timezone.utc).isoformat(),
                "datacontenttype": "application/json",
                "data": {
                    "actor": {"type": "service", "id": partner_id or "unknown"},
                    "action": kind,
                    "outcome": outcome,
                    "resource": {"type": "use_case", "id": use_case or "unknown"},
                    "reason": reason,
                    "context": {"request_id": request_id, **(context or {})},
                },
            }
            task = asyncio.get_running_loop().create_task(self._send(event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except Exception:
            _logger.warning("Failed to queue an audit event", exc_info=True)

    async def _send(self, event: Dict[str, Any]) -> None:
        try:
            client = self._client_getter()
            await client.post(f"{self.url}{EVENTS_PATH}", json=event, timeout=self.timeout)
        except Exception as e:
            _logger.warning("Audit Manager emit failed: %r", e)

    async def drain(self) -> None:
        """Wait for queued events (shutdown and tests)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
