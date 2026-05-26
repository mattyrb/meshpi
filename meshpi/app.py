"""meshpi entry point. Wires interface, logger, watchdog, automations, GUI.

Run with:
    python -m meshpi.app

On the Pi this is invoked by systemd (see systemd/meshpi.service). The
watchdog will call os._exit on terminal failure so systemd can restart us
cleanly; SIGTERM triggers an orderly shutdown.
"""

from __future__ import annotations

import importlib
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any

from . import config as cfg_mod
from .automations.base import Automation
from .gui import BacklightController, MessagingGui
from .interface import (
    EVENT_CONNECTED,
    EVENT_DISCONNECTED,
    EVENT_NODE,
    EVENT_PACKET,
    InterfaceManager,
)
from .logger import SqliteLogger
from .watchdog import Watchdog

log = logging.getLogger("meshpi")


def _setup_logging() -> None:
    level = os.environ.get("MESHPI_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def _load_automations(
    enabled: list[str],
    params_by_name: dict[str, dict[str, Any]],
) -> list[Automation]:
    out: list[Automation] = []
    for name in enabled:
        try:
            mod = importlib.import_module(f"meshpi.automations.{name}")
        except ImportError:
            log.exception("automation module not found: %s", name)
            continue
        # Convention: each module exposes a single Automation subclass.
        cls: type[Automation] | None = None
        for value in vars(mod).values():
            if (
                isinstance(value, type)
                and issubclass(value, Automation)
                and value is not Automation
            ):
                cls = value
                break
        if cls is None:
            log.warning("automation %s has no Automation subclass", name)
            continue
        params = params_by_name.get(name, {})
        try:
            out.append(cls(params))
            log.info("loaded automation: %s", name)
        except Exception:  # noqa: BLE001
            log.exception("failed to construct automation %s", name)
    return out


class App:
    def __init__(self, cfg: cfg_mod.Config):
        self.cfg = cfg
        self.iface = InterfaceManager(cfg.serial.device, cfg.serial.baud)
        self.sqlite = SqliteLogger(
            cfg.database.path,
            batch_size=cfg.database.batch_size,
            batch_seconds=cfg.database.batch_seconds,
        )
        self.automations = _load_automations(
            cfg.automations.enabled, cfg.automations.params
        )
        self.gui: MessagingGui | None = None
        self.watchdog: Watchdog | None = None
        self._tick_stop = threading.Event()
        self._tick_thread: threading.Thread | None = None
        self._shutting_down = False

    # ----- providers handed to GUI and automations -----

    def _my_position(self) -> tuple[float | None, float | None]:
        info = self.iface.my_node_info() or {}
        pos = info.get("position") or {}
        return pos.get("latitude"), pos.get("longitude")

    def _gui_notice(self, source: str, message: str) -> None:
        if self.gui is not None:
            self.gui.show_notice(source, message)

    def _send_text_logged(
        self,
        text: str,
        destination: str | int | None = None,
        channel: int = 0,
        want_ack: bool = False,
    ) -> None:
        """Send a text packet AND log it locally so it shows up in our own
        message history.

        The meshtastic library does not echo our outbound packets back
        through the receive callback, so the SQLite logger would never see
        them otherwise and the GUI would not display them. This wrapper
        is what we pass to the GUI and automations instead of the raw
        InterfaceManager.send_text.
        """
        # Send first. If this raises, we do NOT log a phantom message.
        self.iface.send_text(
            text, destination=destination, channel=channel, want_ack=want_ack
        )
        try:
            my_id = self.iface.my_node_id() or "us"
            to_id = destination if destination is not None else "^all"
            self.sqlite.log_packet({
                "id": None,  # local message; no mesh packet id
                "fromId": my_id,
                "toId": to_id,
                "channel": int(channel),
                "decoded": {
                    "portnum": "TEXT_MESSAGE_APP",
                    "text": text,
                },
            })
            # Flush so the row is queryable immediately, not in 5 seconds.
            self.sqlite.flush()
            if self.gui is not None:
                # Nudge the GUI to refresh the messages list.
                self.gui.push_event("packet", {})
        except Exception:  # noqa: BLE001
            log.exception("failed to log sent message")

    # ----- event fan-out from interface -----

    def _on_iface_event(self, event_type: str, payload: Any) -> None:
        try:
            if event_type == EVENT_PACKET:
                self._handle_packet(payload)
            elif event_type == EVENT_NODE:
                self._handle_node(payload)
            elif event_type == EVENT_CONNECTED:
                # The meshtastic library populates its in-memory nodes dict
                # during the initial sync but does NOT fire per-node events
                # for the seed. Walk it here so the SQLite nodes table has
                # rows the moment we are connected.
                self._seed_nodes()
                if self.gui is not None:
                    self.gui.push_event("connected", {})
            elif event_type == EVENT_DISCONNECTED:
                if self.gui is not None:
                    self.gui.push_event("disconnected", {})
        except Exception:  # noqa: BLE001
            log.exception("error handling interface event %s", event_type)

    def _seed_nodes(self) -> None:
        """Upsert every node from the interface's in-memory dict into SQLite.

        Called on connect (initial seed) and from the tick loop (cheap
        periodic resync that catches any meshtastic.node.updated events
        we may have missed during a brief stall).
        """
        try:
            nodes = self.iface.nodes() or {}
        except Exception:  # noqa: BLE001
            log.exception("iface.nodes() failed during seed")
            return
        seeded = 0
        for node in nodes.values():
            if not isinstance(node, dict):
                continue
            try:
                self.sqlite.upsert_node(node)
                seeded += 1
            except Exception:  # noqa: BLE001
                log.exception("upsert_node failed during seed")
        if seeded and self.gui is not None:
            # Trigger a GUI refresh on the next drain.
            self.gui.push_event("node", {})
        log.info("seeded %d node(s) from interface dict", seeded)

    def _handle_packet(self, pkt: dict[str, Any]) -> None:
        # Log first; never let downstream handlers swallow the record.
        try:
            self.sqlite.log_packet(pkt)
        except Exception:  # noqa: BLE001
            log.exception("logger.log_packet failed")

        # Notify GUI.
        if self.gui is not None:
            self.gui.push_event("packet", pkt)

        # Dispatch to automations.
        decoded = pkt.get("decoded") or {}
        portnum = decoded.get("portnum") or ""
        for auto in self.automations:
            try:
                if portnum == "TEXT_MESSAGE_APP":
                    auto.on_text(pkt, self._send_text_logged)
                elif portnum == "POSITION_APP":
                    auto.on_position(pkt, self._send_text_logged)
            except Exception:  # noqa: BLE001
                log.exception("automation %s on packet failed", auto.name)

    def _handle_node(self, node: dict[str, Any]) -> None:
        try:
            self.sqlite.upsert_node(node)
        except Exception:  # noqa: BLE001
            log.exception("logger.upsert_node failed")
        if self.gui is not None:
            self.gui.push_event("node", node)
        for auto in self.automations:
            try:
                auto.on_node_update(node, self._send_text_logged)
            except Exception:  # noqa: BLE001
                log.exception("automation %s on node failed", auto.name)

    # ----- periodic tick for automations -----

    def _tick_loop(self) -> None:
        ticks = 0
        while not self._tick_stop.is_set():
            now = datetime.now(timezone.utc)
            for auto in self.automations:
                try:
                    auto.tick(now, self._send_text_logged)
                except Exception:  # noqa: BLE001
                    log.exception("automation %s tick failed", auto.name)
            # Re-seed nodes every ~2 minutes so a missed update event
            # cannot leave the GUI's node list stale forever.
            ticks += 1
            if ticks % 4 == 0:
                self._seed_nodes()
            self._tick_stop.wait(30.0)

    # ----- lifecycle -----

    def start(self) -> None:
        self.sqlite.open()
        self.iface.subscribe(self._on_iface_event)
        self.iface.connect()

        # Wire optional callbacks to automations.
        for auto in self.automations:
            try:
                auto._attach(self._gui_notice)  # noqa: SLF001 (intentional)
            except Exception:  # noqa: BLE001
                log.debug("auto._attach failed", exc_info=True)
            # Special: AutoReply wants our own node info.
            if getattr(auto, "name", "") == "autoreply":
                setattr(auto, "my_node_info_cb", self.iface.my_node_info)

        self.watchdog = Watchdog(self.iface, self.cfg.watchdog, self._on_watchdog_fail)
        self.watchdog.start()

        self._tick_thread = threading.Thread(
            target=self._tick_loop, name="meshpi-tick", daemon=True
        )
        self._tick_thread.start()

        # Backlight controller: tries to write the sysfs file once to
        # detect permissions, then silently no-ops if the meshpi process
        # cannot write to it (no udev rule installed yet).
        bl = BacklightController(
            self.cfg.backlight.path,
            self.cfg.backlight.max_brightness_path,
        )

        # GUI runs on the main thread; this blocks until window closes.
        self.gui = MessagingGui(
            send_text=self._send_text_logged,
            canned_messages=self.cfg.gui.canned_messages,
            recent_messages_provider=self.sqlite.recent_messages,
            nodes_provider=self.sqlite.known_nodes,
            my_position_provider=self._my_position,
            channels_provider=self.iface.channels,
            my_node_id_provider=self.iface.my_node_id,
            my_node_stats_provider=self.iface.my_node_stats,
            send_position=self.iface.send_position,
            channel_counts_provider=self.sqlite.channel_message_counts_today,
            fullscreen=self.cfg.gui.fullscreen,
            display_timezone=self.cfg.gui.display_timezone,
            backlight=bl,
            idle_dim_seconds=self.cfg.backlight.idle_dim_seconds,
            idle_dim_brightness=self.cfg.backlight.idle_dim_brightness,
            wake_brightness=self.cfg.backlight.wake_brightness,
            alert_on_message=self.cfg.backlight.alert_on_message,
            alert_on_dm_only=self.cfg.backlight.alert_on_dm_only,
        )
        self.gui.run()

    def shutdown(self, reason: str = "shutdown") -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        log.info("shutdown: %s", reason)
        self._tick_stop.set()
        if self.watchdog is not None:
            self.watchdog.stop()
        if self.gui is not None:
            self.gui.stop()
        try:
            self.iface.close()
        except Exception:  # noqa: BLE001
            log.exception("iface.close failed")
        try:
            self.sqlite.close()
        except Exception:  # noqa: BLE001
            log.exception("sqlite.close failed")

    def _on_watchdog_fail(self, message: str) -> None:
        log.error("watchdog terminal failure: %s; exiting for systemd restart", message)
        # Try a clean shutdown, then exit non-zero.
        try:
            self.shutdown(reason="watchdog")
        finally:
            # os._exit is intentional: we want to bypass Tk mainloop teardown.
            time.sleep(0.5)
            os._exit(2)


def main() -> int:
    _setup_logging()
    try:
        cfg = cfg_mod.load()
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return 1
    except Exception:  # noqa: BLE001
        log.exception("failed to load config")
        return 1

    app = App(cfg)

    def _term(_signum: int, _frame: Any) -> None:
        log.info("signal received; shutting down")
        app.shutdown(reason="signal")
        # GUI mainloop exit will let main() return normally.

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)

    try:
        app.start()
    except KeyboardInterrupt:
        app.shutdown(reason="KeyboardInterrupt")
    except Exception:  # noqa: BLE001
        log.exception("fatal error in app.start")
        app.shutdown(reason="exception")
        return 1
    finally:
        app.shutdown(reason="exit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
