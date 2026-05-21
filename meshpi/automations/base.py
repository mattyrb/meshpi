"""Base class for meshpi automations.

Subclasses override any subset of the on_* methods plus an optional tick().
The dispatcher in app.py calls these methods on background threads, so
implementations should be thread-safe. Use the supplied `send` callable to
transmit; it routes through the single connection owner.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol


class SendCallable(Protocol):
    def __call__(
        self,
        text: str,
        destination: str | int | None = None,
        channel: int = 0,
        want_ack: bool = False,
    ) -> None: ...


class Automation:
    """Override the hooks you need; leave the rest as no-ops."""

    name: str = "automation"

    def __init__(self, params: dict[str, Any]):
        self.params = params

    # ---- event hooks; default to no-op ----

    def on_text(self, packet: dict[str, Any], send: SendCallable) -> None:
        """A text message was received."""

    def on_position(self, packet: dict[str, Any], send: SendCallable) -> None:
        """A position packet was received."""

    def on_node_update(self, node: dict[str, Any], send: SendCallable) -> None:
        """The nodes-db entry for a node changed."""

    def tick(self, now: datetime, send: SendCallable) -> None:
        """Called periodically for time-based logic. now is UTC-aware."""

    # ---- helpers ----

    def gui_notice(self, message: str) -> None:
        """Surface a notice on the GUI. app.py sets this hook after construction."""
        notice = getattr(self, "_gui_notice_cb", None)
        if notice is not None:
            try:
                notice(self.name, message)
            except Exception:  # noqa: BLE001
                pass

    def _attach(
        self,
        gui_notice_cb: Callable[[str, str], None] | None,
    ) -> None:
        """Internal: app.py calls this to wire optional callbacks."""
        self._gui_notice_cb = gui_notice_cb
