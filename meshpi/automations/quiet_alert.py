"""Alert when a named node goes silent for too long.

Tracks the last-heard timestamp for the configured node by name and pushes
a notice to the GUI when the silence threshold is crossed. Only alerts
once per silence event; resets when the node is heard again.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .base import Automation, SendCallable

log = logging.getLogger(__name__)


class QuietAlert(Automation):
    name = "quiet_alert"

    def __init__(self, params: dict[str, Any]):
        super().__init__(params)
        self.node_name = str(params.get("node_name", "")).strip()
        self.silence_seconds = int(params.get("silence_seconds", 7200))
        self._last_heard: datetime | None = None
        self._alerted = False

    def _matches(self, node: dict[str, Any]) -> bool:
        user = node.get("user") or {}
        names = {
            (user.get("longName") or "").strip(),
            (user.get("shortName") or "").strip(),
        }
        return self.node_name in names and bool(self.node_name)

    def on_node_update(self, node: dict[str, Any], send: SendCallable) -> None:
        if not self._matches(node):
            return
        self._last_heard = datetime.now(timezone.utc)
        if self._alerted:
            log.info("quiet_alert: %s is back", self.node_name)
            self.gui_notice(f"{self.node_name} is back online")
            self._alerted = False

    def tick(self, now: datetime, send: SendCallable) -> None:
        if not self.node_name or self._last_heard is None or self._alerted:
            return
        silence = (now - self._last_heard).total_seconds()
        if silence >= self.silence_seconds:
            log.warning(
                "quiet_alert: %s silent for %.0fs", self.node_name, silence
            )
            self.gui_notice(
                f"{self.node_name} silent for {int(silence // 60)} min"
            )
            self._alerted = True
