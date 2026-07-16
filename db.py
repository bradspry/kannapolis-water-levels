"""
Simple SQLite store for Kannapolis/Concord water source readings.

Tables
------
lake_readings   — city reservoir levels scraped from concordnc.gov
stream_readings — USGS stream gauge flows (ft³/s)
cube_readings   — Cube Hydro Carolinas reservoir elevations (hourly)

This program is free software: you can redistribute it and/or modify it
under the terms of the GNU General Public License v3.0. See LICENSE.
"""

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "water_data.db"


def _connect() -> sqlite3.Connection:
    """Open a WAL-mode connection with row access by column name."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create tables if they don't exist."""
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS lake_readings (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                collected_at     TEXT    NOT NULL,
                lake_name        TEXT    NOT NULL,
                level_text       TEXT    NOT NULL,
                inches_from_full REAL               -- + below full, - above, 0 = full
            );

            CREATE TABLE IF NOT EXISTS stream_readings (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                collected_at TEXT    NOT NULL,
                site_name    TEXT    NOT NULL,
                site_no      TEXT    NOT NULL,
                flow_cfs     REAL,
                percentile   REAL
            );

            -- Migrate existing stream_readings table if percentile column is missing
            -- (SQLite ignores duplicate column errors when using a separate try block)
        """)
        try:
            conn.execute("ALTER TABLE stream_readings ADD COLUMN percentile REAL")
        except Exception:
            pass
        conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_lake_name_time
                ON lake_readings (lake_name, collected_at);

            CREATE INDEX IF NOT EXISTS idx_stream_site_time
                ON stream_readings (site_name, collected_at);

            CREATE TABLE IF NOT EXISTS cube_readings (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                collected_at  TEXT    NOT NULL,
                lake_name     TEXT    NOT NULL,
                elevation_ft  REAL,
                ft_below_full REAL
            );

            CREATE INDEX IF NOT EXISTS idx_cube_lake_time
                ON cube_readings (lake_name, collected_at);
        """)


def _parse_inches(level_text: str) -> float | None:
    """
    Convert level text to a signed float (inches from full).
      "Full"              →  0.0
      "10.8\" Below Full" → +10.8
      "3.0\" Above Full"  →  -3.0
    """
    text = level_text.strip().lower()
    if text == "full":
        return 0.0
    m = re.search(r"([\d.]+)", text)
    if not m:
        return None
    val = float(m.group(1))
    return -val if "above" in text else val


def store_lake_readings(levels: dict[str, str]) -> None:
    """Persist a dict of {lake_name: level_text} with the current UTC timestamp."""
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        (now, name, text, _parse_inches(text))
        for name, text in levels.items()
    ]
    with _connect() as conn:
        conn.executemany(
            "INSERT INTO lake_readings (collected_at, lake_name, level_text, inches_from_full) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )


def store_stream_readings(usgs_data: dict[str, dict]) -> None:
    """Persist USGS gauge flows and percentile rank with the current UTC timestamp."""
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        (now, name, info["site_no"], info.get("flow_cfs"), info.get("percentile"))
        for name, info in usgs_data.items()
    ]
    with _connect() as conn:
        conn.executemany(
            "INSERT INTO stream_readings (collected_at, site_name, site_no, flow_cfs, percentile) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )


def store_cube_readings(cube_data: dict) -> None:
    """Persist Cube Hydro reservoir readings with the current UTC timestamp."""
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        (now, info["display"], info.get("elevation_ft"), info.get("ft_below_full"))
        for info in cube_data.values()
    ]
    with _connect() as conn:
        conn.executemany(
            "INSERT INTO cube_readings (collected_at, lake_name, elevation_ft, ft_below_full) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )


def get_cube_history(lake_name: str, days: int = 30) -> list[sqlite3.Row]:
    """Return Cube Hydro rows for a reservoir ordered oldest-first, limited to N days."""
    with _connect() as conn:
        return conn.execute(
            """
            SELECT collected_at, elevation_ft, ft_below_full
            FROM cube_readings
            WHERE lake_name = ?
              AND collected_at >= datetime('now', ?)
            ORDER BY collected_at
            """,
            (lake_name, f"-{days} days"),
        ).fetchall()


def get_lake_history(lake_name: str, days: int = 30) -> list[sqlite3.Row]:
    """Return rows for a lake ordered oldest-first, limited to the last N days."""
    with _connect() as conn:
        return conn.execute(
            """
            SELECT collected_at, level_text, inches_from_full
            FROM lake_readings
            WHERE lake_name = ?
              AND collected_at >= datetime('now', ?)
            ORDER BY collected_at
            """,
            (lake_name, f"-{days} days"),
        ).fetchall()


def get_stream_history(site_name: str, days: int = 30) -> list[sqlite3.Row]:
    """Return rows for a gauge ordered oldest-first, limited to the last N days."""
    with _connect() as conn:
        return conn.execute(
            """
            SELECT collected_at, flow_cfs, percentile
            FROM stream_readings
            WHERE site_name = ?
              AND collected_at >= datetime('now', ?)
            ORDER BY collected_at
            """,
            (site_name, f"-{days} days"),
        ).fetchall()


def lake_trend(lake_name: str) -> float | None:
    """
    Change in inches_from_full between the two most recent distinct calendar days.
    Positive = dropped further below full; negative = rose toward full.
    """
    with _connect() as conn:
        rows = conn.execute("""
            SELECT date(collected_at) AS day, AVG(inches_from_full) AS val
            FROM lake_readings WHERE lake_name = ?
            GROUP BY day ORDER BY day DESC LIMIT 2
        """, (lake_name,)).fetchall()
    if len(rows) < 2 or rows[0]["val"] is None or rows[1]["val"] is None:
        return None
    return rows[0]["val"] - rows[1]["val"]


def stream_trend(site_name: str) -> float | None:
    """
    Change in flow_cfs between the two most recent distinct calendar days.
    Positive = flow increased; negative = flow decreased.
    """
    with _connect() as conn:
        rows = conn.execute("""
            SELECT date(collected_at) AS day, AVG(flow_cfs) AS val
            FROM stream_readings WHERE site_name = ?
            GROUP BY day ORDER BY day DESC LIMIT 2
        """, (site_name,)).fetchall()
    if len(rows) < 2 or rows[0]["val"] is None or rows[1]["val"] is None:
        return None
    return rows[0]["val"] - rows[1]["val"]


def reservoir_trend(lake_name: str) -> float | None:
    """
    Change in ft_below_full between the two most recent distinct calendar days.
    Positive = dropped further below full; negative = rose toward full.
    """
    with _connect() as conn:
        rows = conn.execute("""
            SELECT date(collected_at) AS day, AVG(ft_below_full) AS val
            FROM cube_readings WHERE lake_name = ?
            GROUP BY day ORDER BY day DESC LIMIT 2
        """, (lake_name,)).fetchall()
    if len(rows) < 2 or rows[0]["val"] is None or rows[1]["val"] is None:
        return None
    return rows[0]["val"] - rows[1]["val"]


def summary() -> None:
    """Print a quick count of stored readings per source."""
    with _connect() as conn:
        print("Lake readings:")
        for row in conn.execute(
            "SELECT lake_name, COUNT(*) as n, MAX(collected_at) as last "
            "FROM lake_readings GROUP BY lake_name ORDER BY lake_name"
        ):
            print(f"  {row['lake_name']}: {row['n']} readings, last {row['last']}")

        print("Stream readings:")
        for row in conn.execute(
            "SELECT site_name, COUNT(*) as n, MAX(collected_at) as last "
            "FROM stream_readings GROUP BY site_name ORDER BY site_name"
        ):
            print(f"  {row['site_name']}: {row['n']} readings, last {row['last']}")

        print("Cube Hydro readings:")
        for row in conn.execute(
            "SELECT lake_name, COUNT(*) as n, MAX(collected_at) as last "
            "FROM cube_readings GROUP BY lake_name ORDER BY lake_name"
        ):
            print(f"  {row['lake_name']}: {row['n']} readings, last {row['last']}")


if __name__ == "__main__":
    init_db()
    summary()
