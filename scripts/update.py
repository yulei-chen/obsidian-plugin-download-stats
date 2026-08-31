"""Daily incremental update: fetch the newest snapshots and store them.

Ten commits are looked back over by default rather than just the newest one, so
that a run following several failed ones automatically fills in the missing days
without manual intervention.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from common import (
    DEFAULT_DB,
    PLUGINS_PATH,
    SOURCE_REPO,
    STATS_PATH,
    PluginIndex,
    connect,
    fetch,
    mark_processed,
    parse_stats,
    replace_versions,
    set_meta,
    ssl_context,
    store_snapshot,
    to_epoch_day,
    update_metadata,
)

API_COMMITS = f"https://api.github.com/repos/{SOURCE_REPO}/commits"
RAW_BASE = f"https://raw.githubusercontent.com/{SOURCE_REPO}"


def recent_commits(limit: int) -> list[tuple[str, str]]:
    """Return the most recent commits touching the stats file, oldest first."""
    url = f"{API_COMMITS}?path={STATS_PATH}&per_page={limit}"
    request = urllib.request.Request(url, headers=_api_headers())
    with urllib.request.urlopen(request, timeout=60, context=ssl_context()) as response:
        payload = json.load(response)
    commits = [(item["sha"], item["commit"]["committer"]["date"]) for item in payload]
    commits.reverse()
    return commits


def _api_headers() -> dict[str, str]:
    headers = {
        "User-Agent": "obsidian-plugin-download-stats",
        "Accept": "application/vnd.github+json",
    }
    # A token raises the rate limit from 60/h to 5000/h inside Actions.
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch the latest snapshots into the database")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--lookback", type=int, default=10, help="look back over the N newest commits")
    args = parser.parse_args()

    conn = connect(args.db)
    index = PluginIndex(conn)
    done = {row[0] for row in conn.execute("SELECT sha FROM processed_commits")}

    commits = recent_commits(args.lookback)
    pending = [(sha, when) for sha, when in commits if sha not in done]
    if not pending:
        print("Already up to date.")
        return 0

    print(f"{len(pending)} new snapshots to process")
    latest_stats: dict = {}

    for sha, committed_at in pending:
        raw = fetch(f"{RAW_BASE}/{sha}/{STATS_PATH}")
        stats = parse_stats(raw)
        if not stats:
            print(f"  {sha[:8]} malformed, skipped")
            mark_processed(conn, [sha])
            continue

        day = to_epoch_day(
            dt.datetime.fromisoformat(committed_at.replace("Z", "+00:00"))
            .astimezone(dt.timezone.utc)
            .date()
        )
        store_snapshot(conn, index, day, sha, committed_at, stats)
        mark_processed(conn, [sha])
        latest_stats = stats
        print(f"  {sha[:8]}  {committed_at[:10]}  {len(stats)} plugins")

    if latest_stats:
        replace_versions(conn, index, latest_stats)

    try:
        metadata_raw = fetch(f"{RAW_BASE}/master/{PLUGINS_PATH}")
        count = update_metadata(conn, index, metadata_raw)
        print(f"Refreshed metadata for {count} plugins")
    except (urllib.error.URLError, ValueError) as error:
        # Metadata only affects display names, so a failure here must not fail
        # the whole update.
        print(f"Metadata refresh failed, keeping previous values: {error}")

    set_meta(conn, "updated_at", dt.datetime.now(dt.timezone.utc).isoformat())
    conn.commit()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
