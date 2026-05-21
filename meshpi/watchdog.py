"""Watchdog for the Meshtastic interface.

USB serial connections can hang while the process keeps running, so
systemd's Restart=always alone does not catch a silent stall. This watchdog
runs in its own thread and:

- Checks time since the last received packet and the last successful
  interface heartbeat.
- On silence past the threshold, attempts a reconnect.
- If the reconnect cannot succeed within reconnect_grace_seconds, calls the
  fail_action callback. app.py wires that to a clean exit so systemd can
  restart the service.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from .config import WatchdogConfig
from .interface import InterfaceManager

log = logging.getLogger(__name__)


class Watchdog:
    def __init__(
        self,
        iface: InterfaceManager,
        cfg: WatchdogConfig,
        fail_action: Callable[[str], None],
    ):
        self.iface = iface
        self.cfg = cfg
        self.fail_action = fail_action
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="meshpi-watchdog", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        log.info(
            "watchdog up: silence=%ds heartbeat=%ds reconnect_grace=%ds",
            self.cfg.silence_seconds,
            self.cfg.heartbeat_seconds,
            self.cfg.reconnect_grace_seconds,
        )
        # Give the interface a moment to settle after first connect.
        self._stop.wait(self.cfg.heartbeat_seconds)

        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                log.exception("watchdog tick error")
            self._stop.wait(self.cfg.heartbeat_seconds)

    def _tick(self) -> None:
        now = time.monotonic()
        last_rx = self.iface.stats.last_rx_time
        idle = (now - last_rx) if last_rx else float("inf")

        # Cheap interface poke. Bumps last_heartbeat_time on success.
        alive = self.iface.heartbeat()

        if alive and idle < self.cfg.silence_seconds:
            return  # healthy

        if not alive:
            log.warning("watchdog: heartbeat failed; will try reconnect")
        else:
            log.warning(
                "watchdog: no packets for %.0fs (threshold %ds); will try reconnect",
                idle,
                self.cfg.silence_seconds,
            )

        if self._try_reconnect():
            return

        msg = f"watchdog: reconnect failed after {self.cfg.reconnect_grace_seconds}s"
        log.error(msg)
        self.fail_action(msg)

    def _try_reconnect(self) -> bool:
        deadline = time.monotonic() + self.cfg.reconnect_grace_seconds
        backoff = 2.0
        while time.monotonic() < deadline and not self._stop.is_set():
            if self.iface.reconnect():
                log.info("watchdog: reconnect succeeded")
                return True
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 15.0)
        return False
