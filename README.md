# Obsidian Plugin Download Stats

Look up any Obsidian community plugin and chart its download history, daily,
weekly or monthly.

The data comes from `community-plugin-stats.json` in
[obsidianmd/obsidian-releases](https://github.com/obsidianmd/obsidian-releases).
That file is updated once a day, but it only contains the **current** cumulative
counts — there is no history in it. The history is reconstructed from the file's
git commits: roughly 1992 commits since 2020-10-30, one snapshot per day.

## Architecture

No server, no hosted database, no paid components.

```
obsidian-releases (git history)
        │
        ├── backfill.py   one-off: walk 1992 commits to rebuild history
        └── update.py     daily: fetch the newest snapshot
                │
                ▼
          stats.db (SQLite)  ── stored as a GitHub release asset
                │
                ▼
          export.py  generates 7000+ static JSON files
                │
                ▼
          GitHub Pages  static site, the browser fetches JSON directly
```

**Why keep the database in a release asset?** It is on the order of 100 MB, and
committing it daily would make git retain a full copy of every day, growing the
repository to tens of gigabytes within a year. Release assets are overwritten in
place and do not count towards repository size. For the same reason the
generated files under `site/data/` are never committed either; they are packaged
straight into a Pages artifact by `actions/upload-pages-artifact`. **The
repository itself stays at a few hundred kilobytes of code.**

## Three modelling decisions

**Only the `downloads` total is kept as a time series.** Each plugin carries an
average of 13.5 per-version entries in the source file (95596 across the whole
registry). Turning those into time series would mean 95596 × 2130 ≈ **200
million rows**, against an upper bound of 15 million for the totals alone.
Per-version numbers are only meaningful as a current value, so just the latest
snapshot is stored.

**Cumulative values are stored, not deltas.** A daily increment is the difference
between two adjacent days, and weekly/monthly views resample those increments —
all in the browser. Precomputing three aggregations wastes space and drifts out
of sync with the detail whenever history gets corrected.

**Plugins use an integer surrogate key.** Seven thousand string slugs repeated
across two thousand days would put hundreds of megabytes into the ID column
alone. A `plugins` table maps slug to ID, and the main table is `WITHOUT ROWID`
so the primary key is the data.

```sql
CREATE TABLE downloads (
    plugin_id INTEGER NOT NULL REFERENCES plugins(id),
    day       INTEGER NOT NULL,   -- UTC epoch day
    total     INTEGER NOT NULL,   -- cumulative downloads
    PRIMARY KEY (plugin_id, day)
) WITHOUT ROWID;
```

## Quirks in the data

Charts come out wrong unless these are handled:

**The cumulative count can go down.** Obsidian prunes inflated numbers, and a
plugin that is delisted and relisted resets its count. Adjacent days producing a
negative difference is normal. Negative increments are clamped to zero and
counted, and the UI reports how many occurred.

**Missing days must not be filled with zero.** A plugin is absent from the JSON
before it is listed and after it is delisted, and upstream occasionally skips a
commit. Those days are `null` in the data. Filling them with zero produces huge
phantom spikes in the daily view, so an increment spanning N days is spread
evenly across them instead.

**Dates must be normalised to UTC.** Upstream commits around 00:30 UTC, so using
a local timezone shifts everything by a day. When a day has several commits, the
last one wins.

## Usage

A first deployment needs one backfill; everything after that is automatic.

```bash
# Backfill all history locally (clones the 192 MB source repo, ~10 minutes)
python scripts/backfill.py

# Or just the last 10 days, for a quick check
python scripts/update.py --lookback 10

# Generate the static data and preview
python scripts/export.py
python -m http.server 8000 --directory site
```

On GitHub: trigger the **Backfill history** workflow once by hand, after which
**Update and publish** runs daily at 03:30 UTC. In the repository settings, set
the Pages source to **GitHub Actions**.

No third-party dependencies; the Python standard library (3.11+) is enough.

The series math is unit tested — gap spreading, clamped drops, week and month
alignment:

```bash
node --test tests/*.test.js
```

## Size of the generated output

| | Size |
|---|---|
| One plugin's full history (2130 days) | 18 KB, 5 KB gzipped |
| Search index (7147 plugins) | 418 KB, 156 KB gzipped |
| Whole site | about 73 MB |

GitHub Pages allows 1 GB per site, so there is plenty of headroom. Looking up a
plugin costs the index plus a single 5 KB file, which is why the time series is
stored as plain arrays rather than delta encoded.
