"""Shared database schema, date helpers, and snapshot parsing.

The three core modelling decisions are documented in the README:
1. Only the `downloads` total is kept as a time series; per-version numbers are
   stored for the latest snapshot only.
2. Cumulative values are stored rather than deltas; daily/weekly/monthly
   aggregations are all derived at query time.
3. Plugins use an integer surrogate key so the slug is not repeated across
   millions of rows.
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import os
import sqlite3
import ssl
import urllib.request
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "stats.db"
SOURCE_REPO = "obsidianmd/obsidian-releases"
STATS_PATH = "community-plugin-stats.json"
PLUGINS_PATH = "community-plugins.json"

_EPOCH = dt.date(1970, 1, 1)
_USER_AGENT = "obsidian-plugin-download-stats (+https://github.com)"

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS plugins (
    id          INTEGER PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,
    name        TEXT,
    author      TEXT,
    description TEXT,
    repo        TEXT
);

-- Main table: one row per plugin per day holding the cumulative download count.
-- WITHOUT ROWID makes the primary key the data itself, saving a B-tree level.
CREATE TABLE IF NOT EXISTS downloads (
    plugin_id INTEGER NOT NULL REFERENCES plugins(id),
    day       INTEGER NOT NULL,
    total     INTEGER NOT NULL,
    PRIMARY KEY (plugin_id, day)
) WITHOUT ROWID;

-- Which commit each day was sourced from, used for resuming and for tracing
-- anomalies back to the upstream snapshot.
CREATE TABLE IF NOT EXISTS snapshots (
    day          INTEGER PRIMARY KEY,
    sha          TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    plugin_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_commits (
    sha TEXT PRIMARY KEY
);

-- Latest snapshot only. Expanding per-version counts into a time series would
-- produce roughly 200 million rows for no practical benefit.
CREATE TABLE IF NOT EXISTS versions (
    plugin_id INTEGER NOT NULL REFERENCES plugins(id),
    version   TEXT NOT NULL,
    downloads INTEGER NOT NULL,
    PRIMARY KEY (plugin_id, version)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def to_epoch_day(date: dt.date) -> int:
    return (date - _EPOCH).days


def from_epoch_day(day: int) -> dt.date:
    return _EPOCH + dt.timedelta(days=day)


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@functools.lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    """TLS context with CA bundle fallbacks.

    Python installed from python.org on macOS ships without any usable CA
    bundle and fails every request with CERTIFICATE_VERIFY_FAILED. Try the
    system default, then certifi, then the usual system paths, so the scripts
    run unmodified both locally and in CI.
    """
    context = ssl.create_default_context()
    if ssl.get_default_verify_paths().cafile:
        return context

    candidates = []
    try:
        import certifi

        candidates.append(certifi.where())
    except ImportError:
        pass
    candidates += ["/etc/ssl/cert.pem", "/opt/homebrew/etc/ca-certificates/cert.pem"]

    for path in candidates:
        if path and os.path.exists(path):
            context.load_verify_locations(cafile=path)
            return context
    return context


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=60, context=ssl_context()) as response:
        return response.read()


def parse_stats(raw: bytes | str) -> dict[str, dict[str, Any]]:
    """Parse a community-plugin-stats.json snapshot into {slug: {total, versions}}.

    Entries look like {"downloads": 4773, "updated": 1709754303000, "0.1.0": 22, ...}
    where every key other than downloads/updated is a version number. Early
    snapshots contain entries with missing or oddly typed fields; those are
    skipped rather than failing the whole backfill.
    """
    data = json.loads(raw)
    if not isinstance(data, dict):
        return {}

    result: dict[str, dict[str, Any]] = {}
    for slug, entry in data.items():
        if not isinstance(entry, dict):
            continue
        total = entry.get("downloads")
        if not isinstance(total, int):
            continue
        versions = {
            key: value
            for key, value in entry.items()
            if key not in ("downloads", "updated") and isinstance(value, int)
        }
        result[slug] = {"total": total, "versions": versions}
    return result


class PluginIndex:
    """In-memory slug -> integer primary key cache, to avoid a query per row."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._ids: dict[str, int] = dict(
            conn.execute("SELECT slug, id FROM plugins").fetchall()
        )

    def id_for(self, slug: str) -> int:
        cached = self._ids.get(slug)
        if cached is not None:
            return cached
        cursor = self._conn.execute(
            "INSERT INTO plugins (slug) VALUES (?) ON CONFLICT(slug) DO NOTHING", (slug,)
        )
        if cursor.lastrowid and cursor.rowcount:
            plugin_id = cursor.lastrowid
        else:
            plugin_id = self._conn.execute(
                "SELECT id FROM plugins WHERE slug = ?", (slug,)
            ).fetchone()[0]
        self._ids[slug] = plugin_id
        return plugin_id


def store_snapshot(
    conn: sqlite3.Connection,
    index: PluginIndex,
    day: int,
    sha: str,
    committed_at: str,
    stats: dict[str, dict[str, Any]],
) -> None:
    """Write one day of cumulative counts. Later writes win for the same day."""
    rows = [(index.id_for(slug), day, entry["total"]) for slug, entry in stats.items()]
    conn.executemany(
        "INSERT INTO downloads (plugin_id, day, total) VALUES (?, ?, ?) "
        "ON CONFLICT(plugin_id, day) DO UPDATE SET total = excluded.total",
        rows,
    )
    conn.execute(
        "INSERT INTO snapshots (day, sha, committed_at, plugin_count) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(day) DO UPDATE SET sha = excluded.sha, "
        "committed_at = excluded.committed_at, plugin_count = excluded.plugin_count",
        (day, sha, committed_at, len(rows)),
    )


def replace_versions(
    conn: sqlite3.Connection, index: PluginIndex, stats: dict[str, dict[str, Any]]
) -> None:
    rows = [
        (index.id_for(slug), version, count)
        for slug, entry in stats.items()
        for version, count in entry["versions"].items()
    ]
    conn.execute("DELETE FROM versions")
    conn.executemany(
        "INSERT INTO versions (plugin_id, version, downloads) VALUES (?, ?, ?)", rows
    )


def update_metadata(conn: sqlite3.Connection, index: PluginIndex, raw: bytes | str) -> int:
    """Fill in display fields (name, author, repo) from community-plugins.json."""
    entries = json.loads(raw)
    rows = []
    for entry in entries:
        slug = entry.get("id")
        if not slug:
            continue
        rows.append(
            (
                entry.get("name"),
                entry.get("author"),
                entry.get("description"),
                entry.get("repo"),
                index.id_for(slug),
            )
        )
    conn.executemany(
        "UPDATE plugins SET name = ?, author = ?, description = ?, repo = ? WHERE id = ?",
        rows,
    )
    return len(rows)


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def mark_processed(conn: sqlite3.Connection, shas: Iterable[str]) -> None:
    conn.executemany(
        "INSERT INTO processed_commits (sha) VALUES (?) ON CONFLICT(sha) DO NOTHING",
        ((sha,) for sha in shas),
    )
