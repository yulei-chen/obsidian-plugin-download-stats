"""Export the SQLite database into the static JSON the site consumes.

Output:
    site/data/index.json            search index over every plugin
    site/data/plugins/<slug>.json   full time series for one plugin

`totals` in each plugin file is the cumulative download count, where index i
corresponds to day start + i and a day without a snapshot is null. Daily,
weekly and monthly views are all derived from that single array in the browser
rather than being precomputed, which keeps them consistent when history is
backfilled or corrected.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import sys
from pathlib import Path

from common import DEFAULT_DB, ROOT, connect, from_epoch_day

# Obsidian plugin ids only use these characters; the check also prevents a
# malicious slug from escaping the output directory.
SAFE_SLUG = re.compile(r"^[A-Za-z0-9._-]+$")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate static JSON from the database")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=ROOT / "site" / "data")
    parser.add_argument("--pretty", action="store_true", help="indent output for debugging")
    args = parser.parse_args()

    conn = connect(args.db)
    plugin_dir = args.out / "plugins"
    if plugin_dir.exists():
        shutil.rmtree(plugin_dir)
    plugin_dir.mkdir(parents=True, exist_ok=True)

    plugins = {
        row[0]: row
        for row in conn.execute(
            "SELECT id, slug, name, author, description, repo FROM plugins"
        )
    }
    versions: dict[int, list[tuple[str, int]]] = {}
    for plugin_id, version, count in conn.execute(
        "SELECT plugin_id, version, downloads FROM versions"
    ):
        versions.setdefault(plugin_id, []).append((version, count))

    separators = (", ", ": ") if args.pretty else (",", ":")
    indent = 2 if args.pretty else None

    index: list[list] = []
    written = 0
    skipped: list[str] = []

    for plugin_id, days, totals in _iter_series(conn):
        record = plugins.get(plugin_id)
        if record is None:
            continue
        _, slug, name, author, description, repo = record

        if not SAFE_SLUG.match(slug):
            skipped.append(slug)
            continue

        start = days[0]
        end = days[-1]
        # Expand into a day-aligned dense array, leaving gaps as null. Filling
        # gaps with zero would create huge phantom spikes in the daily view.
        dense: list[int | None] = [None] * (end - start + 1)
        for day, total in zip(days, totals):
            dense[day - start] = total

        payload = {
            "id": slug,
            "name": name or slug,
            "author": author,
            "description": description,
            "repo": repo,
            "start": start,
            "startDate": from_epoch_day(start).isoformat(),
            "totals": dense,
            "versions": sorted(versions.get(plugin_id, []), key=lambda item: -item[1]),
        }
        path = plugin_dir / f"{slug}.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=separators, indent=indent),
            encoding="utf-8",
        )
        written += 1
        index.append([slug, name or slug, author or "", totals[-1], end])

    index.sort(key=lambda item: -item[3])

    span = conn.execute("SELECT MIN(day), MAX(day) FROM snapshots").fetchone()
    index_payload = {
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "firstDay": span[0],
        "lastDay": span[1],
        "firstDate": from_epoch_day(span[0]).isoformat() if span[0] is not None else None,
        "lastDate": from_epoch_day(span[1]).isoformat() if span[1] is not None else None,
        "fields": ["id", "name", "author", "downloads", "lastDay"],
        "plugins": index,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "index.json").write_text(
        json.dumps(index_payload, ensure_ascii=False, separators=separators, indent=indent),
        encoding="utf-8",
    )

    total_bytes = sum(p.stat().st_size for p in plugin_dir.glob("*.json"))
    print(f"Exported {written} plugins, {total_bytes / 1024 / 1024:.1f} MB total")
    print(f"Index is {(args.out / 'index.json').stat().st_size / 1024:.0f} KB")
    if skipped:
        print(f"Skipped {len(skipped)} plugins with unsafe ids: {', '.join(skipped[:5])}")

    conn.close()
    return 0


def _iter_series(conn):
    """Stream one time series per plugin so millions of rows never sit in memory."""
    cursor = conn.execute(
        "SELECT plugin_id, day, total FROM downloads ORDER BY plugin_id, day"
    )
    current_id = None
    days: list[int] = []
    totals: list[int] = []

    for plugin_id, day, total in cursor:
        if plugin_id != current_id:
            if current_id is not None and days:
                yield current_id, days, totals
            current_id = plugin_id
            days, totals = [], []
        days.append(day)
        totals.append(total)

    if current_id is not None and days:
        yield current_id, days, totals


if __name__ == "__main__":
    sys.exit(main())
