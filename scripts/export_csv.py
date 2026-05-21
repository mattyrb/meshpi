"""Export meshpi SQLite data to CSV.

Safe to run while meshpi is writing: SQLite WAL mode permits concurrent
readers. Reads the database path from the same config.toml the app uses,
so you don't have to keep paths in sync.

Examples:
    # Today's messages and the current node directory, default destination
    python scripts/export_csv.py --since today --tables messages,nodes

    # Last 7 days of every packet (including raw_json), to a chosen directory
    python scripts/export_csv.py --since 7d --tables packets --out ~/exports

    # Everything, ever, to stdout for piping
    python scripts/export_csv.py --since all --tables messages --stdout
"""

from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make `python scripts/export_csv.py` importable from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from meshpi import config as cfg_mod  # noqa: E402

log = logging.getLogger("meshpi.export_csv")


# Friendly table name -> SQL fragment.
# 'messages' is the curated view people usually want: text-bearing packets,
# minus the fat raw_json column. 'packets' is the full firehose.
TABLES: dict[str, dict[str, str]] = {
    "messages": {
        "select": (
            "SELECT rx_time_utc, from_id, to_id, channel, portnum, text, "
            "       rssi, snr, hop_limit, hop_start "
            "FROM packets WHERE text IS NOT NULL"
        ),
        "time_column": "rx_time_utc",
        "order_by": "ORDER BY id",
    },
    "packets": {
        "select": "SELECT * FROM packets",
        "time_column": "rx_time_utc",
        "order_by": "ORDER BY id",
    },
    "nodes": {
        "select": "SELECT * FROM nodes",
        "time_column": "",  # no per-row filter
        "order_by": "ORDER BY last_heard_utc DESC",
    },
}


def _since_to_cutoff(since: str) -> str | None:
    """Translate '--since today|1d|24h|7d|30d|all' to a UTC ISO 8601 cutoff.

    Returns None for 'all' (no time filter). Raises for unknown values.
    """
    s = since.strip().lower()
    if s in ("all", "everything"):
        return None
    if s == "today":
        # Start of today in UTC.
        return datetime.now(timezone.utc).date().isoformat()
    # Forms like 1d, 7d, 24h, 30d.
    if s.endswith("d"):
        try:
            days = int(s[:-1])
        except ValueError as exc:
            raise ValueError(f"unknown --since value: {since!r}") from exc
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        return cutoff.isoformat(timespec="seconds")
    if s.endswith("h"):
        try:
            hours = int(s[:-1])
        except ValueError as exc:
            raise ValueError(f"unknown --since value: {since!r}") from exc
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        return cutoff.isoformat(timespec="seconds")
    raise ValueError(
        f"unknown --since value: {since!r}. "
        "Use 'today', 'all', or e.g. '1d', '24h', '7d'."
    )


def _build_query(table_name: str, cutoff: str | None) -> tuple[str, tuple]:
    spec = TABLES[table_name]
    sql = spec["select"]
    params: tuple = ()
    if cutoff and spec["time_column"]:
        # Pick the right connector depending on whether the base SELECT
        # already has a WHERE clause (e.g. messages does; packets does not).
        connector = "AND" if "WHERE" in sql.upper() else "WHERE"
        if len(cutoff) == 10:  # YYYY-MM-DD: --since today
            sql += f" {connector} substr({spec['time_column']}, 1, 10) = ?"
        else:                  # full timestamp: --since 7d, 24h, ...
            sql += f" {connector} {spec['time_column']} >= ?"
        params = (cutoff,)
    if spec["order_by"]:
        sql += " " + spec["order_by"]
    return sql, params


def _export_one(
    conn: sqlite3.Connection,
    table_name: str,
    cutoff: str | None,
    target,
) -> int:
    sql, params = _build_query(table_name, cutoff)
    cur = conn.execute(sql, params)
    columns = [c[0] for c in cur.description]
    writer = csv.writer(target)
    writer.writerow(columns)
    n = 0
    for row in cur:
        writer.writerow(row)
        n += 1
    return n


def run(
    cfg: cfg_mod.Config,
    tables: list[str],
    since: str,
    out_dir: Path,
    use_stdout: bool,
) -> int:
    cutoff = _since_to_cutoff(since)
    db_path = Path(cfg.database.path)
    if not db_path.exists():
        log.error("SQLite db not found at %s", db_path)
        return 2

    # uri=True + ?mode=ro is a clean read-only open; pairs well with WAL.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        if use_stdout:
            for i, table in enumerate(tables):
                if i > 0:
                    sys.stdout.write("\n")
                if len(tables) > 1:
                    sys.stdout.write(f"# table: {table}\n")
                _export_one(conn, table, cutoff, sys.stdout)
            return 0

        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        for table in tables:
            since_tag = "all" if cutoff is None else since.replace("d", "d").replace("h", "h")
            out_path = out_dir / f"meshpi-{table}-{since_tag}-{stamp}.csv"
            with out_path.open("w", newline="", encoding="utf-8") as fh:
                n = _export_one(conn, table, cutoff, fh)
            log.info("wrote %s (%d rows)", out_path, n)
            print(f"{out_path}  ({n} rows)")
        return 0
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export meshpi SQLite to CSV")
    parser.add_argument(
        "--config",
        help="Path to config.toml (defaults to env MESHPI_CONFIG or repo config.toml)",
    )
    parser.add_argument(
        "--tables",
        default="messages,nodes",
        help="Comma-separated subset of: messages, packets, nodes. "
             "Default: messages,nodes",
    )
    parser.add_argument(
        "--since",
        default="today",
        help="Time window: 'today', 'all', or NNd / NNh (e.g. 7d, 24h). "
             "Default: today. Ignored for the 'nodes' table.",
    )
    parser.add_argument(
        "--out",
        default="~/meshpi-exports",
        help="Output directory. Default: ~/meshpi-exports",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Write to stdout instead of files (for piping).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    requested = [t.strip() for t in args.tables.split(",") if t.strip()]
    unknown = [t for t in requested if t not in TABLES]
    if unknown:
        log.error("unknown table(s): %s (known: %s)",
                  ", ".join(unknown), ", ".join(TABLES))
        return 2

    try:
        cfg = cfg_mod.load(args.config)
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return 1

    out_dir = Path(args.out).expanduser().resolve()
    try:
        return run(cfg, requested, args.since, out_dir, args.stdout)
    except ValueError as exc:
        log.error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
