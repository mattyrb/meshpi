"""Optional PostGIS sync. Off by default.

Reads rows from the meshpi SQLite database that are newer than the last
synced id (tracked in sync_state) and inserts them into a PostGIS table
with a Point geometry built from lat/lon stored as EPSG:4326. Any
reprojection (e.g. to EPSG:5070) is left to downstream analysis.

Run from cron or a systemd timer. Reads its connection string from the
same TOML config the app uses.

Target table DDL (run once, manually, with your preferred role):

    CREATE EXTENSION IF NOT EXISTS postgis;
    CREATE SCHEMA IF NOT EXISTS meshpi;
    CREATE TABLE IF NOT EXISTS meshpi.packets (
      sqlite_id    BIGINT PRIMARY KEY,
      packet_id    BIGINT,
      rx_time_utc  TIMESTAMPTZ NOT NULL,
      from_id      TEXT,
      to_id        TEXT,
      channel      INTEGER,
      portnum      TEXT,
      text         TEXT,
      altitude     DOUBLE PRECISION,
      rssi         DOUBLE PRECISION,
      snr          DOUBLE PRECISION,
      hop_limit    INTEGER,
      hop_start    INTEGER,
      raw_json     JSONB NOT NULL,
      geom         geometry(Point, 4326),
      inserted_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS ix_meshpi_packets_geom ON meshpi.packets USING GIST(geom);
    CREATE INDEX IF NOT EXISTS ix_meshpi_packets_rx_time ON meshpi.packets(rx_time_utc);
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

# Make `python scripts/postgis_sync.py` importable from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from meshpi import config as cfg_mod  # noqa: E402

log = logging.getLogger("meshpi.postgis_sync")

SYNC_KEY = "postgis_last_sqlite_id"


def _get_cursor(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT value FROM sync_state WHERE key = ?", (SYNC_KEY,))
    row = cur.fetchone()
    return int(row[0]) if row else 0


def _set_cursor(conn: sqlite3.Connection, value: int) -> None:
    conn.execute(
        """
        INSERT INTO sync_state(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (SYNC_KEY, str(value)),
    )


def run(cfg: cfg_mod.Config) -> int:
    if not cfg.postgis.enabled:
        log.info("postgis sync is disabled in config; nothing to do")
        return 0

    try:
        import psycopg
    except ImportError:
        log.error(
            "psycopg not installed. pip install 'psycopg[binary]==3.1.18' "
            "(already in requirements.txt)"
        )
        return 2

    sqlite_path = Path(cfg.database.path)
    if not sqlite_path.exists():
        log.error("SQLite db not found at %s", sqlite_path)
        return 2

    moved = 0
    with sqlite3.connect(sqlite_path) as slite:
        slite.execute("PRAGMA journal_mode=WAL")
        last_id = _get_cursor(slite)
        log.info("starting sync from sqlite id > %d", last_id)

        cur = slite.execute(
            """
            SELECT id, packet_id, rx_time_utc, from_id, to_id, channel, portnum,
                   text, latitude, longitude, altitude, rssi, snr,
                   hop_limit, hop_start, raw_json
              FROM packets
             WHERE id > ?
             ORDER BY id ASC
             LIMIT ?
            """,
            (last_id, cfg.postgis.batch_size),
        )
        rows = cur.fetchall()
        if not rows:
            log.info("nothing new to sync")
            return 0

        with psycopg.connect(cfg.postgis.dsn, autocommit=False) as pg:
            with pg.cursor() as pcur:
                insert_sql = f"""
                INSERT INTO {cfg.postgis.table} (
                    sqlite_id, packet_id, rx_time_utc, from_id, to_id,
                    channel, portnum, text, altitude, rssi, snr,
                    hop_limit, hop_start, raw_json, geom
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                    CASE
                      WHEN %s IS NOT NULL AND %s IS NOT NULL
                      THEN ST_SetSRID(ST_MakePoint(%s, %s), 4326)
                      ELSE NULL
                    END
                )
                ON CONFLICT (sqlite_id) DO NOTHING
                """
                for row in rows:
                    (
                        sid, packet_id, rx_time_utc, from_id, to_id,
                        channel, portnum, text, latitude, longitude,
                        altitude, rssi, snr, hop_limit, hop_start, raw_json,
                    ) = row
                    # Ensure raw_json is valid JSON text.
                    try:
                        json.loads(raw_json)
                    except (TypeError, ValueError):
                        raw_json = json.dumps({"_unparsed": raw_json})
                    pcur.execute(
                        insert_sql,
                        (
                            sid, packet_id, rx_time_utc, from_id, to_id,
                            channel, portnum, text, altitude, rssi, snr,
                            hop_limit, hop_start, raw_json,
                            longitude, latitude, longitude, latitude,
                        ),
                    )
                    moved += 1
                    last_id = max(last_id, sid)
            pg.commit()

        _set_cursor(slite, last_id)
        slite.commit()

    log.info("synced %d row(s); new cursor=%d", moved, last_id)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync meshpi SQLite to PostGIS")
    parser.add_argument(
        "--config",
        help="Path to config.toml (defaults to env MESHPI_CONFIG or repo config.toml)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        cfg = cfg_mod.load(args.config)
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return 1
    return run(cfg)


if __name__ == "__main__":
    sys.exit(main())
