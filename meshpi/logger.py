"""SQLite logger for meshpi.

Design notes:
- WAL mode and batched commits to avoid one fsync per packet.
- Stores both the raw packet as JSON and parsed columns, so any field we
  did not think to extract later is still recoverable.
- Dedup on (packet_id, from_id) since the same mesh packet is rebroadcast.
- Captures RSSI, SNR, hop limit, hop start, plus position when present.
- Timestamps in UTC, ISO 8601.
- Node IDs are stored as their string form ("!abcdef12"), normalized.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS packets (
  id           INTEGER PRIMARY KEY,
  packet_id    INTEGER,
  rx_time_utc  TEXT NOT NULL,
  from_id      TEXT,
  to_id        TEXT,
  channel      INTEGER,
  portnum      TEXT,
  text         TEXT,
  latitude     REAL,
  longitude    REAL,
  altitude     REAL,
  rssi         REAL,
  snr          REAL,
  hop_limit    INTEGER,
  hop_start    INTEGER,
  raw_json     TEXT NOT NULL,
  inserted_at  TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_packet_dedup
  ON packets(packet_id, from_id);

CREATE INDEX IF NOT EXISTS ix_packets_rx_time ON packets(rx_time_utc);
CREATE INDEX IF NOT EXISTS ix_packets_from   ON packets(from_id);

CREATE TABLE IF NOT EXISTS nodes (
  node_id        TEXT PRIMARY KEY,
  long_name      TEXT,
  short_name     TEXT,
  last_heard_utc TEXT,
  battery_level  INTEGER,
  latitude       REAL,
  longitude      REAL,
  snr            REAL,
  updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_state (
  key   TEXT PRIMARY KEY,
  value TEXT
);
"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_node_id(value: Any) -> str | None:
    """Return a stable string node id, '!xxxxxxxx' style when possible."""
    if value is None:
        return None
    if isinstance(value, int):
        return f"!{value:08x}"
    s = str(value).strip()
    if not s:
        return None
    # Some library paths emit decimal ints as strings; coerce when possible.
    if s.isdigit():
        try:
            return f"!{int(s):08x}"
        except ValueError:
            return s
    return s


def _parse_packet(pkt: dict[str, Any]) -> dict[str, Any]:
    """Pull the columns we care about out of a meshtastic packet dict."""
    decoded = pkt.get("decoded") or {}
    position = decoded.get("position") or {}

    text = decoded.get("text")
    if text is None and "payload" in decoded:
        # Some non-text payloads are bytes; skip storing them as text.
        payload = decoded.get("payload")
        if isinstance(payload, str):
            text = payload

    return {
        "packet_id": pkt.get("id"),
        "from_id": _normalize_node_id(pkt.get("fromId") or pkt.get("from")),
        "to_id": _normalize_node_id(pkt.get("toId") or pkt.get("to")),
        "channel": pkt.get("channel"),
        "portnum": decoded.get("portnum"),
        "text": text,
        "latitude": position.get("latitude") or position.get("latitudeI"),
        "longitude": position.get("longitude") or position.get("longitudeI"),
        "altitude": position.get("altitude"),
        "rssi": pkt.get("rxRssi"),
        "snr": pkt.get("rxSnr"),
        "hop_limit": pkt.get("hopLimit"),
        "hop_start": pkt.get("hopStart"),
    }


def _json_default(o: Any) -> Any:
    """Best-effort JSON encoder for protobuf-flavored objects."""
    try:
        return str(o)
    except Exception:  # noqa: BLE001
        return repr(o)


class SqliteLogger:
    """Buffered SQLite writer. Thread-safe; flushes on batch size or interval."""

    def __init__(
        self,
        db_path: str | Path,
        batch_size: int = 50,
        batch_seconds: float = 5.0,
    ):
        self.db_path = Path(db_path)
        self.batch_size = batch_size
        self.batch_seconds = batch_seconds
        self._buffer: list[tuple] = []
        self._lock = threading.Lock()
        self._last_flush = time.monotonic()
        self._conn: sqlite3.Connection | None = None

    # ----- lifecycle -----

    def open(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False because we explicitly serialize via self._lock.
        self._conn = sqlite3.connect(
            self.db_path, check_same_thread=False, timeout=30.0
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        log.info("SQLite logger ready at %s", self.db_path)

    def close(self) -> None:
        with self._lock:
            self._flush_locked(force=True)
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ----- writes -----

    def log_packet(self, packet: dict[str, Any]) -> None:
        if self._conn is None:
            raise RuntimeError("logger not opened")
        parsed = _parse_packet(packet)
        raw_json = json.dumps(packet, default=_json_default, separators=(",", ":"))
        row = (
            parsed["packet_id"],
            _utc_now_iso(),
            parsed["from_id"],
            parsed["to_id"],
            parsed["channel"],
            parsed["portnum"],
            parsed["text"],
            parsed["latitude"],
            parsed["longitude"],
            parsed["altitude"],
            parsed["rssi"],
            parsed["snr"],
            parsed["hop_limit"],
            parsed["hop_start"],
            raw_json,
            _utc_now_iso(),
        )
        with self._lock:
            self._buffer.append(row)
            self._maybe_flush_locked()

    def upsert_node(self, node: dict[str, Any]) -> None:
        if self._conn is None:
            raise RuntimeError("logger not opened")
        user = node.get("user") or {}
        position = node.get("position") or {}
        device_metrics = node.get("deviceMetrics") or {}

        node_id = _normalize_node_id(node.get("num") or user.get("id"))
        if not node_id:
            return

        row = {
            "node_id": node_id,
            "long_name": user.get("longName"),
            "short_name": user.get("shortName"),
            "last_heard_utc": _utc_now_iso(),
            "battery_level": device_metrics.get("batteryLevel"),
            "latitude": position.get("latitude"),
            "longitude": position.get("longitude"),
            "snr": node.get("snr"),
            "updated_at": _utc_now_iso(),
        }
        with self._lock:
            assert self._conn is not None
            self._conn.execute(
                """
                INSERT INTO nodes
                  (node_id, long_name, short_name, last_heard_utc,
                   battery_level, latitude, longitude, snr, updated_at)
                VALUES
                  (:node_id, :long_name, :short_name, :last_heard_utc,
                   :battery_level, :latitude, :longitude, :snr, :updated_at)
                ON CONFLICT(node_id) DO UPDATE SET
                  long_name      = COALESCE(excluded.long_name, nodes.long_name),
                  short_name     = COALESCE(excluded.short_name, nodes.short_name),
                  last_heard_utc = excluded.last_heard_utc,
                  battery_level  = COALESCE(excluded.battery_level, nodes.battery_level),
                  latitude       = COALESCE(excluded.latitude, nodes.latitude),
                  longitude      = COALESCE(excluded.longitude, nodes.longitude),
                  snr            = COALESCE(excluded.snr, nodes.snr),
                  updated_at     = excluded.updated_at
                """,
                row,
            )

    # ----- flush -----

    def _maybe_flush_locked(self) -> None:
        now = time.monotonic()
        if (
            len(self._buffer) >= self.batch_size
            or (now - self._last_flush) >= self.batch_seconds
        ):
            self._flush_locked()

    def _flush_locked(self, force: bool = False) -> None:
        if self._conn is None:
            return
        if not self._buffer and not force:
            return
        if self._buffer:
            try:
                self._conn.executemany(
                    """
                    INSERT OR IGNORE INTO packets
                      (packet_id, rx_time_utc, from_id, to_id, channel, portnum,
                       text, latitude, longitude, altitude, rssi, snr,
                       hop_limit, hop_start, raw_json, inserted_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    self._buffer,
                )
                self._conn.commit()
            except sqlite3.Error:
                log.exception("sqlite flush failed; dropping buffer")
            finally:
                self._buffer.clear()
        self._last_flush = time.monotonic()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked(force=True)

    # ----- read helpers used by the GUI -----

    def recent_messages(self, limit: int = 50) -> list[dict[str, Any]]:
        if self._conn is None:
            return []
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT rx_time_utc, from_id, to_id, channel, text
                  FROM packets
                 WHERE text IS NOT NULL
                 ORDER BY id DESC
                 LIMIT ?
                """,
                (limit,),
            )
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def known_nodes(self) -> list[dict[str, Any]]:
        if self._conn is None:
            return []
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT node_id, long_name, short_name, last_heard_utc,
                       battery_level, latitude, longitude, snr
                  FROM nodes
                 ORDER BY last_heard_utc DESC NULLS LAST
                """
            )
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
