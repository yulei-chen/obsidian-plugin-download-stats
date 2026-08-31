"""One-off backfill: walk the obsidian-releases commit history to rebuild
the daily download counts.

Usage:
    python scripts/backfill.py                 # clone the source repo and backfill everything
    python scripts/backfill.py --limit 50      # only the 50 oldest commits, for a quick check

The run is resumable: processed commits are recorded in the processed_commits
table and skipped on a rerun.
"""

from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import sys
import time
from pathlib import Path

from common import (
    DEFAULT_DB,
    PLUGINS_PATH,
    ROOT,
    SOURCE_REPO,
    STATS_PATH,
    PluginIndex,
    connect,
    from_epoch_day,
    mark_processed,
    parse_stats,
    replace_versions,
    set_meta,
    store_snapshot,
    to_epoch_day,
    update_metadata,
)

DEFAULT_CLONE = ROOT / ".cache" / "obsidian-releases"


def ensure_clone(repo_dir: Path) -> None:
    if (repo_dir / "HEAD").exists() or (repo_dir / ".git").exists():
        print(f"Reusing existing clone at {repo_dir}")
        subprocess.run(
            ["git", "-C", str(repo_dir), "fetch", "--quiet", "origin", "master"], check=True
        )
        return

    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    print(f"Cloning {SOURCE_REPO} (~192 MB, master only, no working tree)…")
    subprocess.run(
        [
            "git", "clone", "--no-checkout", "--single-branch",
            "--branch", "master",
            f"https://github.com/{SOURCE_REPO}.git", str(repo_dir),
        ],
        check=True,
    )


def list_commits(repo_dir: Path) -> list[tuple[str, str]]:
    """List every commit touching the stats file, oldest first."""
    output = subprocess.run(
        [
            "git", "-C", str(repo_dir), "log", "--reverse",
            "--format=%H\t%cI", "origin/master", "--", STATS_PATH,
        ],
        check=True, capture_output=True, text=True,
    ).stdout
    commits = []
    for line in output.splitlines():
        sha, _, committed_at = line.partition("\t")
        if sha and committed_at:
            commits.append((sha, committed_at))
    return commits


class BlobReader:
    """Long-lived `git cat-file --batch` process.

    Spawning `git show` once per commit would burn minutes on process creation
    alone across two thousand commits, so revisions are fed to a single
    long-running process instead.
    """

    def __init__(self, repo_dir: Path) -> None:
        self._proc = subprocess.Popen(
            ["git", "-C", str(repo_dir), "cat-file", "--batch"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        )

    def read(self, sha: str, path: str) -> bytes | None:
        assert self._proc.stdin and self._proc.stdout
        self._proc.stdin.write(f"{sha}:{path}\n".encode())
        self._proc.stdin.flush()

        header = self._proc.stdout.readline().decode().strip()
        if header.endswith(("missing", "ambiguous")) or " blob " not in header:
            return None
        size = int(header.rsplit(" ", 1)[1])
        payload = self._proc.stdout.read(size)
        self._proc.stdout.read(1)  # cat-file emits a trailing newline per blob
        return payload

    def close(self) -> None:
        if self._proc.stdin:
            self._proc.stdin.close()
        self._proc.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill daily download counts from git history")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--repo-dir", type=Path, default=DEFAULT_CLONE)
    parser.add_argument("--limit", type=int, default=None, help="process only the N oldest commits")
    args = parser.parse_args()

    ensure_clone(args.repo_dir)

    conn = connect(args.db)
    index = PluginIndex(conn)

    done = {row[0] for row in conn.execute("SELECT sha FROM processed_commits")}
    commits = list_commits(args.repo_dir)
    pending = [(sha, when) for sha, when in commits if sha not in done]
    if args.limit:
        pending = pending[: args.limit]

    print(f"{len(commits)} commits in history, {len(done)} already done, {len(pending)} to process")
    if not pending:
        print("Nothing to backfill.")
        return 0

    reader = BlobReader(args.repo_dir)
    started = time.monotonic()
    latest_stats: dict = {}
    batch: list[str] = []
    skipped = 0

    try:
        for position, (sha, committed_at) in enumerate(pending, start=1):
            raw = reader.read(sha, STATS_PATH)
            if raw is None:
                skipped += 1
                batch.append(sha)
                continue

            try:
                stats = parse_stats(raw)
            except ValueError:
                # A few early commits contain malformed or non-JSON content;
                # skip them instead of aborting the whole backfill.
                skipped += 1
                batch.append(sha)
                continue

            if not stats:
                skipped += 1
                batch.append(sha)
                continue

            day = to_epoch_day(_parse_utc_date(committed_at))
            store_snapshot(conn, index, day, sha, committed_at, stats)
            latest_stats = stats
            batch.append(sha)

            if position % 50 == 0 or position == len(pending):
                mark_processed(conn, batch)
                batch.clear()
                conn.commit()
                elapsed = time.monotonic() - started
                rate = position / elapsed if elapsed else 0
                remaining = (len(pending) - position) / rate if rate else 0
                print(
                    f"  {position}/{len(pending)}  {committed_at[:10]}  "
                    f"{rate:.0f} commit/s  ~{remaining:.0f}s left",
                    flush=True,
                )
    finally:
        reader.close()

    if batch:
        mark_processed(conn, batch)

    if latest_stats:
        replace_versions(conn, index, latest_stats)

    metadata_raw = read_metadata(args.repo_dir)
    if metadata_raw:
        count = update_metadata(conn, index, metadata_raw)
        print(f"Filled in name/author metadata for {count} plugins")

    set_meta(conn, "backfilled_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    conn.commit()

    _report(conn, skipped)
    conn.close()
    return 0


def _parse_utc_date(committed_at: str) -> dt.date:
    parsed = dt.datetime.fromisoformat(committed_at.replace("Z", "+00:00"))
    return parsed.astimezone(dt.timezone.utc).date()


def read_metadata(repo_dir: Path) -> bytes | None:
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "show", f"origin/master:{PLUGINS_PATH}"],
        capture_output=True,
    )
    return result.stdout if result.returncode == 0 else None


def _report(conn, skipped: int) -> None:
    rows = conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
    plugins = conn.execute("SELECT COUNT(*) FROM plugins").fetchone()[0]
    days = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    span = conn.execute("SELECT MIN(day), MAX(day) FROM snapshots").fetchone()
    print("\nBackfill complete")
    print(f"  plugins   {plugins}")
    print(f"  snapshots {days}")
    print(f"  rows      {rows:,}")
    if span[0] is not None:
        print(f"  span      {from_epoch_day(span[0])} → {from_epoch_day(span[1])}")
    if skipped:
        print(f"  skipped invalid commits: {skipped}")


if __name__ == "__main__":
    sys.exit(main())
