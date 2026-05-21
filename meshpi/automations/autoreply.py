"""Auto-reply on keywords like 'ping' or 'status'.

Replies on the same channel the message came in on, or a fixed channel if
reply_channel is set in config. The reply includes node uptime and battery
when we can read them from our own node info.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .base import Automation, SendCallable

log = logging.getLogger(__name__)

_START_MONOTONIC = time.monotonic()


def _format_uptime() -> str:
    secs = int(time.monotonic() - _START_MONOTONIC)
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d{hours:02d}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


class AutoReply(Automation):
    name = "autoreply"

    def __init__(self, params: dict[str, Any]):
        super().__init__(params)
        kws = params.get("keywords") or ["ping", "status"]
        self.keywords = {str(k).strip().lower() for k in kws}
        self.reply_channel = params.get("reply_channel")
        # app.py sets this so we can pull our own battery for the reply.
        self.my_node_info_cb = None

    def on_text(self, packet: dict[str, Any], send: SendCallable) -> None:
        decoded = packet.get("decoded") or {}
        text = (decoded.get("text") or "").strip().lower()
        if not text or text not in self.keywords:
            return
        from_id = packet.get("fromId") or packet.get("from")
        in_channel = packet.get("channel", 0)
        channel = self.reply_channel if self.reply_channel is not None else in_channel

        battery = "?"
        if callable(self.my_node_info_cb):
            try:
                info = self.my_node_info_cb() or {}
                dm = info.get("deviceMetrics") or {}
                if "batteryLevel" in dm:
                    battery = f"{dm['batteryLevel']}%"
            except Exception:  # noqa: BLE001
                log.debug("could not read battery for autoreply", exc_info=True)

        reply = f"pong: up {_format_uptime()}, batt {battery}"
        log.info("autoreply -> %s ch=%s: %s", from_id, channel, reply)
        try:
            send(reply, destination=from_id, channel=int(channel))
        except Exception:  # noqa: BLE001
            log.exception("autoreply send failed")
