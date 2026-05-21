"""Single owner of the serial connection to the Meshtastic node.

Only one process can hold the serial port, so this module is the one place
in meshpi that touches the meshtastic library directly. Everything else
(GUI, logger, automations) consumes events through callbacks registered
with InterfaceManager.subscribe(...) and sends through .send_text(...).

Threading notes:
- The meshtastic library dispatches events via pubsub on background threads.
- We re-publish those events to our own callbacks while still on those threads.
- Consumers that are not thread-safe (Tk) must drain via a queue inside their
  own callback. The GUI does this; the logger and automations are written
  to be safe to call from any thread.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pubsub import pub

# Import lazily to keep the module importable on machines without the
# meshtastic package installed (e.g. CI lint).
try:
    import meshtastic
    import meshtastic.serial_interface as msi
except ImportError:  # pragma: no cover
    meshtastic = None  # type: ignore[assignment]
    msi = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

# Pubsub topic names from the meshtastic library.
TOPIC_RECEIVE = "meshtastic.receive"  # all packets
TOPIC_TEXT = "meshtastic.receive.text"
TOPIC_POSITION = "meshtastic.receive.position"
TOPIC_USER = "meshtastic.receive.user"
TOPIC_NODE_UPDATED = "meshtastic.node.updated"
TOPIC_CONNECTION_ESTABLISHED = "meshtastic.connection.established"
TOPIC_CONNECTION_LOST = "meshtastic.connection.lost"

# Event types we expose to consumers.
EVENT_PACKET = "packet"
EVENT_NODE = "node"
EVENT_CONNECTED = "connected"
EVENT_DISCONNECTED = "disconnected"

Callback = Callable[[str, dict[str, Any]], None]


@dataclass
class InterfaceStats:
    last_rx_time: float = 0.0  # monotonic, seconds
    last_heartbeat_time: float = 0.0
    connected: bool = False


class InterfaceManager:
    """Owns the serial interface and re-publishes events to local subscribers."""

    def __init__(self, device: str, baud: int = 115200):
        self.device = device
        self.baud = baud
        self._iface: Any | None = None
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._subscribers: list[Callback] = []
        self.stats = InterfaceStats()
        self._closed = False

    # ----- lifecycle -----

    def connect(self) -> None:
        """Open the serial interface and wire pubsub."""
        if msi is None:
            raise RuntimeError(
                "meshtastic package not installed; run pip install -r requirements.txt"
            )
        with self._lock:
            if self._iface is not None:
                return
            log.info("Opening Meshtastic serial interface at %s", self.device)
            # The library accepts devPath for explicit serial paths.
            self._iface = msi.SerialInterface(devPath=self.device)
            self._wire_pubsub()
            # Consider the open itself a heartbeat.
            self.stats.last_heartbeat_time = time.monotonic()

    def close(self) -> None:
        """Close the interface and unsubscribe."""
        with self._lock:
            self._closed = True
            if self._iface is None:
                return
            try:
                pub.unsubAll(topicName=TOPIC_RECEIVE)
                pub.unsubAll(topicName=TOPIC_NODE_UPDATED)
                pub.unsubAll(topicName=TOPIC_CONNECTION_ESTABLISHED)
                pub.unsubAll(topicName=TOPIC_CONNECTION_LOST)
            except Exception:  # noqa: BLE001
                log.debug("pubsub unsubscribe error", exc_info=True)
            try:
                self._iface.close()
            except Exception:  # noqa: BLE001
                log.debug("interface close error", exc_info=True)
            self._iface = None
            self.stats.connected = False

    def reconnect(self) -> bool:
        """Close and reopen. Returns True on success."""
        log.warning("Reconnecting Meshtastic interface")
        try:
            self.close()
        except Exception:  # noqa: BLE001
            log.exception("error during close-before-reconnect")
        # Brief pause so the kernel releases the tty.
        time.sleep(1.0)
        try:
            self._closed = False
            self.connect()
            return True
        except Exception:  # noqa: BLE001
            log.exception("reconnect failed")
            return False

    # ----- pubsub plumbing -----

    def _wire_pubsub(self) -> None:
        pub.subscribe(self._on_receive, TOPIC_RECEIVE)
        pub.subscribe(self._on_node_updated, TOPIC_NODE_UPDATED)
        pub.subscribe(self._on_connection_established, TOPIC_CONNECTION_ESTABLISHED)
        pub.subscribe(self._on_connection_lost, TOPIC_CONNECTION_LOST)

    def _dispatch(self, event_type: str, payload: dict[str, Any]) -> None:
        for cb in list(self._subscribers):
            try:
                cb(event_type, payload)
            except Exception:  # noqa: BLE001
                log.exception("subscriber error in %s", cb)

    def _on_receive(self, packet: dict[str, Any], interface: Any) -> None:
        self.stats.last_rx_time = time.monotonic()
        self._dispatch(EVENT_PACKET, packet)

    def _on_node_updated(self, node: dict[str, Any], interface: Any) -> None:
        self._dispatch(EVENT_NODE, node)

    def _on_connection_established(self, interface: Any, topic: Any = None) -> None:
        log.info("Meshtastic connection established")
        self.stats.connected = True
        self.stats.last_heartbeat_time = time.monotonic()
        self._dispatch(EVENT_CONNECTED, {})

    def _on_connection_lost(self, interface: Any, topic: Any = None) -> None:
        log.warning("Meshtastic connection lost")
        self.stats.connected = False
        self._dispatch(EVENT_DISCONNECTED, {})

    # ----- consumer API -----

    def subscribe(self, callback: Callback) -> None:
        """Register a (event_type, payload) callback. Called on bg threads."""
        self._subscribers.append(callback)

    def send_text(
        self,
        text: str,
        destination: str | int | None = None,
        channel: int = 0,
        want_ack: bool = False,
    ) -> None:
        """Thread-safe text send through the single interface."""
        with self._send_lock:
            if self._iface is None:
                raise RuntimeError("interface not connected")
            kwargs: dict[str, Any] = {"text": text, "channelIndex": channel}
            if destination is not None:
                kwargs["destinationId"] = destination
            if want_ack:
                kwargs["wantAck"] = True
            log.info("send_text dest=%s ch=%s text=%r", destination, channel, text)
            self._iface.sendText(**kwargs)

    def send_position(self) -> None:
        """Broadcast our current position now.

        Uses the position the node already knows (fixed_position config, or
        a GPS fix). Useful as a manual 'I'm here' broadcast after moving
        the node or changing the fixed position.
        """
        with self._send_lock:
            if self._iface is None:
                raise RuntimeError("interface not connected")
            send_pos = getattr(self._iface, "sendPosition", None)
            if send_pos is None:
                raise RuntimeError(
                    "meshtastic library does not expose sendPosition; "
                    "upgrade or rebroadcast via the CLI"
                )
            log.info("send_position requested")
            send_pos()

    # ----- introspection -----

    def my_node_info(self) -> dict[str, Any] | None:
        """Return our own node dict, or None if not connected yet."""
        with self._lock:
            if self._iface is None:
                return None
            try:
                return self._iface.getMyNodeInfo()
            except Exception:  # noqa: BLE001
                log.debug("getMyNodeInfo failed", exc_info=True)
                return None

    def my_node_stats(self) -> dict[str, Any]:
        """Compact summary of our own node for the Glance tab.

        Keys (all optional; missing fields are None):
          long_name, short_name: strings as set in the Meshtastic config
          node_id: '!xxxxxxxx' format
          battery_level: int (0..100)
          uptime_seconds: int
          position_age_seconds: float  -- seconds since our last position update
          latitude, longitude: floats
        """
        out: dict[str, Any] = {
            "long_name": None,
            "short_name": None,
            "node_id": None,
            "battery_level": None,
            "uptime_seconds": None,
            "position_age_seconds": None,
            "latitude": None,
            "longitude": None,
        }
        info = self.my_node_info() or {}
        user = info.get("user") or {}
        dm = info.get("deviceMetrics") or {}
        pos = info.get("position") or {}
        out["long_name"] = user.get("longName")
        out["short_name"] = user.get("shortName")
        out["node_id"] = user.get("id") or self.my_node_id()
        out["battery_level"] = dm.get("batteryLevel")
        out["uptime_seconds"] = dm.get("uptimeSeconds")
        out["latitude"] = pos.get("latitude")
        out["longitude"] = pos.get("longitude")
        # Position time is a unix timestamp when present.
        pos_time = pos.get("time")
        if pos_time:
            try:
                out["position_age_seconds"] = max(0.0, time.time() - float(pos_time))
            except (TypeError, ValueError):
                pass
        return out

    def nodes(self) -> dict[str, dict[str, Any]]:
        """Return the current nodes-db keyed by node id."""
        with self._lock:
            if self._iface is None:
                return {}
            return dict(getattr(self._iface, "nodes", {}) or {})

    def heartbeat(self) -> bool:
        """Cheap liveness check used by the watchdog. Returns False on failure."""
        with self._lock:
            if self._iface is None:
                return False
            try:
                # Touching nodes is enough to confirm the interface object
                # is responsive; an outright protocol ping is not exposed.
                _ = getattr(self._iface, "nodes", None)
                self.stats.last_heartbeat_time = time.monotonic()
                return True
            except Exception:  # noqa: BLE001
                log.debug("heartbeat failed", exc_info=True)
                return False

    def my_node_id(self) -> str | None:
        """Our own node id in the '!xxxxxxxx' format used everywhere else.

        Returned shape matches what the SQLite logger stores in to_id, so
        callers can compare directly to detect DMs sent to us.
        """
        with self._lock:
            if self._iface is None:
                return None
            try:
                my_info = getattr(self._iface, "myInfo", None)
                num = getattr(my_info, "my_node_num", None) if my_info else None
                if num:
                    return f"!{int(num):08x}"
            except Exception:  # noqa: BLE001
                log.debug("my_node_id lookup failed", exc_info=True)
            return None

    def channels(self) -> list[tuple[int, str]]:
        """Return active channels as (index, name) tuples.

        Reads from the local node's channel table populated during the
        initial sync. Channels with role DISABLED are skipped. If the
        library has not populated channels yet (or the call fails),
        returns just (0, "default") so the GUI still has something to show.
        """
        fallback = [(0, "default")]
        with self._lock:
            if self._iface is None:
                return fallback
            try:
                local_node = getattr(self._iface, "localNode", None)
                ch_list = getattr(local_node, "channels", None) if local_node else None
                if not ch_list:
                    return fallback
                out: list[tuple[int, str]] = []
                for idx, ch in enumerate(ch_list):
                    role = getattr(ch, "role", None)
                    # role enum: 0=DISABLED, 1=PRIMARY, 2=SECONDARY
                    if role == 0:
                        continue
                    settings = getattr(ch, "settings", None)
                    name = (getattr(settings, "name", "") or "").strip() if settings else ""
                    if not name:
                        name = "default" if idx == 0 else f"ch{idx}"
                    out.append((idx, name))
                return out or fallback
            except Exception:  # noqa: BLE001
                log.debug("channels() failed", exc_info=True)
                return fallback
