# photoindex

Index, deduplicate, and consolidate photos across multiple external disks — including disks that aren't connected at the same time.

## Why this exists

The starting point is a typical "decades of photos spread across many external disks" mess:

- The same photo lives on multiple disks under different filenames (e.g. `IMG_0571.JPG` on one disk, `CB6C8194-7952-46D9-A4AC-C7827387A457.JPG` on another) and at different resolutions.
- Different photos sometimes share the same filename across folders (so naive merging would silently overwrite content).
- Some folders have meaningful names (`1999_TonyaAaronWedding`); many don't (Facebook hashes, `IMG_0001.JPG`, etc.).
- The disks aren't all online at once, so any "did I already see this photo?" decision has to be made from an index — not from re-reading the file.

`photoindex` builds a single SQLite index over every disk you've scanned, identifies exact and near-duplicate photos using SHA-256 + perceptual hashing, and produces a verified, collision-safe copy plan to consolidate everything onto one destination disk.

## Project goals

1. **Never lose a photo.** Source disks are read-only. Different photos with the same filename are flagged and renamed at the destination. Cross-disk duplicates are detected by content, not filename.
2. **Work without all disks connected.** Once a disk has been scanned, its photos are represented in the index by their hashes, dimensions, EXIF, and source path. Future dedup decisions don't require the original disk.
3. **Detect duplicates that filenames hide.** Perceptual hashing groups visually identical photos that have different SHA-256 (re-saves, format conversions, social-media uploads).
4. **Produce a full audit trail.** Every copy is recorded with source disk, source path, destination path, and SHA-256 verified at the destination.
5. **Stay reviewable.** Borderline near-duplicates are surfaced for human review rather than silently auto-merged. Manual decisions persist across re-runs.

## How a copy to destination works

This is the headline workflow: take many overlapping disks of photos, end up with one consolidated copy on a destination disk, with no duplicates and no overwritten files. **It's a two-step process — building a plan, then executing it — so you can review what's about to happen before any disk I/O.**

### Step 1: build a plan (no files copied yet)

```bash
photoindex plan --plan-id v1 --layout by-date \
    --dest /Volumes/Consolidated --max-distance 8
```

The planner does this in the index only:

1. Walk every photo in `photos`.
2. For each photo, decide whether it's a **canonical** (the keeper of its dedup group) or a **drop** (a near-duplicate of a canonical at perceptual hash distance ≤ `--max-distance`).
3. For each canonical, derive a destination *relative* path from `--layout`.
4. Detect any filename collisions the chosen layout would produce and append `(2)`, `(3)`, … suffixes; record the reason.
5. Write one row per canonical into `copy_plan`, plus one row into `plan_runs` recording the plan parameters.

No bytes are copied yet. You can inspect the plan before committing:

```bash
photoindex plan-show --plan-id v1 --limit 30
```

### Step 2: execute the plan (the actual copy)

```bash
photoindex execute-plan --plan-id v1 \
    --mount Photos_03=/Volumes/Photos_03 \
    --mount "Toshiba_Ext=/Volumes/TOSHIBA EXT" \
    --yes
```

For every row in `copy_plan` that doesn't already have a verified entry in `copy_log`:

1. Resolve the source absolute path: `<mount-for-disk_label> + <source-relative-path>`.
2. Open the source **read-only** and stream it to the destination, computing SHA-256 in-flight from the bytes being written.
3. Compare the streamed SHA to the SHA recorded when the photo was scanned. If they differ, the partial destination file is unlinked and the row is reported as `sha_mismatch`.
4. On success, preserve the source's `mtime` and mode at the destination via `shutil.copystat`.
5. Insert one row into `copy_log` recording (source disk label + source relative path) → (destination absolute path) with `dest_sha256` and `verified=1`.

After this completes, every file under `--dest` has a verified SHA-256 matching the source, and `copy_log` is the **authoritative ledger** that lets you trace any destination file back to its origin disk and path.

### What "non-duplicate" means here

A non-duplicate file at the destination is a **canonical** of its dedup group. The dedup engine groups photos that are either:

- Byte-identical (same SHA-256) — usually exact copies across disks.
- Visually identical at perceptual hash distance ≤ `--max-distance` (default 8) — re-saves, format conversions, social-media re-uploads, etc.

Within each group the keeper is picked deterministically: **highest pixel count → largest file size → EXIF datetime present → earliest `file_mtime` → lowest internal photo id**. Group members that are *not* the canonical are simply absent from the plan — never copied, but also never deleted from the source. They stay on their source disks untouched. The plan's audit trail tells you which canonical represents each dropped duplicate.

Photos with no near-duplicates ("singletons") are also canonicals — they're always copied.

### Resumability and idempotency

`execute-plan` is safe to run repeatedly with the same `--plan-id`:

- Rows already in `copy_log` with `verified=1` are skipped.
- If a destination file already exists with the **right** SHA-256 (e.g. from an interrupted prior run), it's re-logged into `copy_log` without re-copying — surfaced as `dest_match`.
- If a destination file already exists with a **wrong** SHA, the executor refuses to overwrite — surfaced as `dest_mismatch`. You decide manually.

So if you Ctrl-C a copy halfway through, just rerun `execute-plan --plan-id <same>` and it picks up where it left off.

### Adding more disks later — the `--plan-id` lifecycle

A plan is **immutable once executed**. The planner refuses to rebuild a `--plan-id` that has rows in `copy_log`, because doing so would orphan the audit trail. The pattern when adding a new disk is therefore:

1. Scan the new disk:
   ```bash
   photoindex scan /Volumes/NewDisk --disk-label NewDisk
   ```
2. Re-run dedup so the new photos cluster against everything already indexed:
   ```bash
   photoindex find-dups
   ```
3. Build a *new* plan with a *new* `--plan-id` (e.g. `v2`, `v3`, `final`):
   ```bash
   photoindex plan --plan-id v2 --layout by-date \
       --dest /Volumes/Consolidated --max-distance 8
   ```
4. Execute it:
   ```bash
   photoindex execute-plan --plan-id v2 --mount …
   ```
   Destination files that were `v1` canonicals and *still are* `v2` canonicals get `dest_match` (no re-copy). Genuinely new canonicals are streamed fresh. Files that were `v1` canonicals but for which `v2` picked a *different* (e.g. higher-resolution) canonical now exist at the destination but aren't in `v2`'s ledger — these are **orphans**.
5. Optionally remove the orphans:
   ```bash
   photoindex cleanup-orphans --plan-id v2 --apply
   ```

The destination grows monotonically (until you cleanup) as new disks are added to the index.

### What the destination looks like

The `--layout` flag at plan time controls the destination tree shape.

**`mirror` (default)** — preserves source folder structure under each disk's label. Collision-free by construction:

```
/Volumes/Consolidated/
├── Photos_03/
│   ├── Lindas_Pictures/
│   │   └── 2010-01/IMG_0123.JPG
│   └── Ralphs_Pictures/
│       └── 1999_TonyaAaronWedding/DSC_0001.JPG
└── Toshiba_Ext/
    └── Family_Photos/Lemke/…
```

**`by-date`** — organizes everything into year/month folders by EXIF datetime:

```
/Volumes/Consolidated/
├── 1999/
│   ├── 1999-07/DSC_0001.JPG
│   └── 1999_TonyaAaronWedding/extra.jpg     # source-folder fallback
├── 2010/
│   ├── 2010-01/IMG_0123.JPG
│   └── 2010-02/IMG_0124 (2).JPG             # collision-renamed
├── …
└── unsorted/
    └── <source-folder>/IMG_0001.JPG          # no EXIF, no year-in-folder
```

The `by-date` resolution chain for each photo is:

1. EXIF datetime present and in 1980–2039 → `<YYYY>/<YYYY>-<MM>/<filename>`.
2. Source folder name contains a 4-digit year → `<YYYY>/<source-folder>/<filename>`.
3. Otherwise → `unsorted/<source-folder>/<filename>`.

Filename collisions in any of these buckets get an auto-incrementing `(N)` suffix and the reason is recorded in `copy_plan.rename_reason`.

## Pipeline at a glance

The pipeline has four phases, each backed by an SQLite table or set of tables:

```
[scan] ──> photos              # one row per file on every disk ever scanned
            │ sha256, phash, dhash, whash,
            │ exif_datetime, camera, gps, dimensions,
            │ disk_label + relative_path (the natural key)
            ▼
[find-dups] ──> similar_pairs   # all (photo_a, photo_b) at phash distance ≤ 18
            │
            ▼
[groups + plan] ──> copy_plan   # one row per "unique" photo to be copied,
                    plan_runs   # with destination path computed from --layout
            │
            ▼
[execute-plan] ──> copy_log     # one row per actually-copied file,
                                # with destination SHA-256 verified
            │
            ▼
[cleanup-orphans / verify-copies]   # maintenance against the final destination
```

User decisions about borderline pairs live in `manual_overrides` and survive `find-dups` re-runs.

## Installation

Requires Python 3.11+. From the project root:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

This installs the `photoindex` command into `.venv/bin/`. There are two ways to run it from then on:

```bash
# Option A — activate the venv once per shell session
source .venv/bin/activate
photoindex --help

# Option B — call the binary directly without activating
.venv/bin/photoindex --help
```

Both are equivalent. The examples below show the activated form (`photoindex …`); prepend `.venv/bin/` if you'd rather not activate.

The optional `[raw]` extra adds `rawpy` for native RAW decoding (not required — RAW files are SHA-256 indexed without it).

### Self-documentation via `--help`

Every command and subcommand prints its full flag list and defaults with `--help`:

```bash
photoindex --help                    # top-level commands
photoindex scan --help               # flags for one subcommand
photoindex execute-plan --help
```

Use this as the source of truth — the README describes intent and workflow, but `--help` is generated from the code itself and is always current.

## Quick start

```bash
# 1. Index a disk (or part of one)
photoindex scan /Volumes/MyDisk --disk-label MyDisk

# 2. Index another disk later (read-only on every source)
photoindex scan /Volumes/OtherDisk/Photos \
    --disk-label OtherDisk --disk-prefix Photos

# 3. Find duplicates across everything indexed so far
photoindex find-dups
photoindex groups            # human-readable groupings
photoindex stats

# 4. Build a copy plan and inspect it
photoindex plan --dest /Volumes/Consolidated --plan-id final
photoindex plan-show --plan-id final

# 5. Execute the plan (copies + SHA verifies, resumable)
photoindex execute-plan --plan-id final \
    --mount MyDisk=/Volumes/MyDisk \
    --mount OtherDisk=/Volumes/OtherDisk

# 6. Maintenance
photoindex verify-copies --plan-id final --sample 200
photoindex cleanup-orphans --plan-id final --apply
```

The default index path is `~/.photoindex/index.sqlite`. Override with `--db <path>` on any command. The default `--plan-id` is `default`.

## Tips & gotchas

A handful of things that bite in practice. Worth reading once.

### The `--db` flag must come before the subcommand

`photoindex` is a Click command group. Global flags like `--db` go before the subcommand name; subcommand-specific flags go after:

```bash
# right
photoindex --db /path/to/index.sqlite scan /Volumes/MyDisk --disk-label MyDisk

# wrong
photoindex scan /Volumes/MyDisk --disk-label MyDisk --db /path/to/index.sqlite
```

If you forget `--db`, you get the default `~/.photoindex/index.sqlite`. There's nothing wrong with that — just don't expect to see your data if you've been keeping it elsewhere.

### Paths with spaces need quoting

Mac volume names frequently contain spaces (`/Volumes/TOSHIBA EXT`, `/Volumes/My Passport for Mac`). Quote them, or escape the space:

```bash
photoindex scan "/Volumes/TOSHIBA EXT/Family_Photos" \
    --disk-label Toshiba_Ext --disk-prefix Family_Photos
```

The `--mount` argument to `execute-plan` is `LABEL=PATH`; if `PATH` has a space, the entire `LABEL=PATH` token must be quoted as one unit:

```bash
photoindex execute-plan --plan-id v3 \
    --mount "Toshiba_Ext=/Volumes/TOSHIBA EXT"
```

### `--disk-label` should be a stable, filesystem-friendly name

Use the same `--disk-label` every time you scan that physical disk. Different label = the index treats it as a *different* disk. Reasonable convention: replace spaces with underscores (`TOSHIBA EXT` → `Toshiba_Ext`). The label is what shows up in dest paths under the `mirror` layout and what `--mount` keys off at execute time.

### `--mount` auto-detection only matches exact names

`execute-plan` will auto-detect `--mount LABEL=/Volumes/LABEL` when the volume directory's name *exactly* matches the label. So `Photos_03` auto-detects (`/Volumes/Photos_03` exists), but `Toshiba_Ext` does *not* (the volume is mounted as `/Volumes/TOSHIBA EXT`). Pass `--mount` explicitly in those cases.

### Use `--disk-prefix` when scanning a sub-folder of a disk

If you scan only part of a disk, recorded paths are relative to whatever directory you pass. That's wrong if you ever expect to scan the rest of the same disk later, because you'll get duplicate relative paths under one `--disk-label`. The fix is to tell `scan` what part of the disk it's looking at:

```bash
# scanning a sub-folder of Photos_03
photoindex scan /Volumes/Photos_03/Lindas_Pictures \
    --disk-label Photos_03 --disk-prefix Lindas_Pictures
# recorded paths become: Lindas_Pictures/...
```

Then a later scan of `/Volumes/Photos_03/Ralphs_Pictures` with `--disk-prefix Ralphs_Pictures` lives in the same disk's index without colliding.

### Re-running `scan` is idempotent

The natural key is `(disk_id, relative_path)`. Re-running `scan` over the same files updates `last_seen` and overwrites hashes; it does *not* duplicate rows. Safe to interrupt and resume.

### Re-running `find-dups` clears and rebuilds `similar_pairs`

…but `manual_overrides` (your `confirm-pair` / `separate-pair` decisions) survive. Don't worry about losing manual review work when you re-scan.

### Building a new plan after executing the old one

`photoindex plan` *refuses* to overwrite a `--plan-id` that already has rows in `copy_log` (which would orphan the audit trail). When you've added new disks and want a refreshed plan, use a new `--plan-id` (e.g. `v2`, `v3`):

```bash
photoindex plan --plan-id v3 --layout by-date --dest /Volumes/Consolidated
```

The old plan's `copy_log` and the dest files it produced stay in place. To remove dest files that are no longer in the new plan, run `photoindex cleanup-orphans --plan-id v3 --apply`.

### `find-dups` is O(n²)

Every photo is compared against every other phashed photo. Approximate runtimes on a 2024-vintage laptop:

| Index size | Pairs compared | Wall time         |
| ---------- | -------------- | ----------------- |
| ~1k        | 500k           | < 1 second        |
| ~30k       | 450M           | ~2 minutes        |
| ~500k      | 125B           | several hours     |

Run it once after a scan completes, not after every individual file.

### Sources are read-only — by design

`scan` and `execute-plan` open every source file with `open("rb")`. There is no code path that writes back to a source disk. If you need to be paranoid, mount source disks read-only at the OS level too.

## Command reference

### `photoindex scan PATH --disk-label LABEL [--disk-prefix PREFIX]`

Walks `PATH` recursively and indexes every recognized photo or video. The combination of `--disk-label` plus the file's path-relative-to-PATH is the unique key, so re-running `scan` over the same directory updates rather than duplicates rows.

- `--disk-label` is the *logical* name of the source disk (typically the volume name). Use the same label every time you scan that disk; pick a different label for a different disk.
- `--disk-prefix` is needed when you scan a *subfolder* of a disk: it gets prepended to recorded paths, so a later scan of a sibling subfolder under the same `--disk-label` doesn't collide. Example: `scan /Volumes/D1/Pictures --disk-label D1 --disk-prefix Pictures` records paths as `Pictures/...`.
- Unsupported file types (`.html`, `.ini`, `.db`, `.dll`, etc.) are silently skipped.
- Files whose names start with `.` are also skipped. This catches macOS metadata (`.DS_Store`, `._*` AppleDouble files) and Picasa-style thumbnail caches in `.tb/` subdirectories — neither of which are user content.
- Sources are opened read-only; `scan` never writes to them.
- Periodic commits every 100 files mean an interruption only loses the last few files of progress.

### `photoindex stats`

Prints a count of indexed photos broken down by disk and file type.

### `photoindex find-dups [--candidate-threshold N] [--confirmed-threshold N]`

Recomputes the `similar_pairs` table from current photo perceptual hashes. All pairs with phash hamming distance ≤ `--candidate-threshold` (default 18) are stored. The `--confirmed-threshold` (default 14) only affects the summary printout. Manual overrides in `manual_overrides` are preserved across reruns.

This is O(n²) over phashed photos. For ~30k photos it takes a few minutes; for ~500k photos expect tens of minutes.

### `photoindex groups [--max-distance N] [--limit N]`

Greedy clustering: highest-quality unclaimed photo becomes a canonical and claims every still-unclaimed photo *directly* within `--max-distance` (default 14) phash distance of it. Lower-quality / lower-resolution / later mtime photos are dropped. Output marks each member as `KEEP` or `drop` so you can eyeball the keeper picks.

Tighter `--max-distance` (e.g. 8) is best for production planning; looser (14) is useful for review.

### `photoindex review-candidates [--min-distance N] [--max-distance N] [--strong-only] [--limit N]`

Lists pairs whose phash distance falls between `--min-distance` (default 9) and `--max-distance` (default 14) — the borderline zone that the production planner *excludes*. Each pair is shown with side-by-side metadata (filename, dimensions, EXIF datetime, camera) and a heuristic hint:

- `STRONG` — looks like a save-copy or re-save: same dimensions and either matching EXIF datetime or sibling filenames like `foo.jpg` / `foo (1).jpg`.
- `BURST` — same dimensions and camera, EXIF a few seconds apart: likely a distinct shot from a burst sequence.
- `MAYBE` — dhash is also close, suggesting visual content matches.

Each pair is printed with a ready-to-paste `confirm-pair` / `separate-pair` command.

### `photoindex confirm-pair ID_A ID_B [--reason "..."]`

Records a `confirmed_dup` decision: this pair will be grouped together by `groups` and `plan` regardless of phash distance. Persists across `find-dups` reruns.

### `photoindex separate-pair ID_A ID_B [--reason "..."]`

Records a `confirmed_distinct` decision: this pair will never be grouped, even if phash distance falls below the threshold. Useful for two visually-similar photos that aren't actually the same content.

### `photoindex plan --dest ROOT [--plan-id NAME] [--max-distance N] [--layout mirror|by-date]`

Builds a fresh copy plan in `copy_plan` + `plan_runs`. For each unique photo:

- One canonical per dedup group at `--max-distance` (default 8) is included; the rest are dropped.
- For files without a perceptual hash (videos, RAW), uniqueness is by SHA-256.
- Singleton photos (not in any group) are included.

Layouts:

- `mirror` (default) — `<disk_label>/<source_relative_path>`. No filename collisions possible; preserves source folder structure.
- `by-date` — `<YYYY>/<YYYY>-<MM>/<filename>` from EXIF datetime. Falls back to `<YYYY>/<source_folder>/<filename>` if the source folder name contains a year, else `unsorted/<source_folder>/<filename>`. Filename collisions get an auto `(N)` suffix and the reason is recorded in `copy_plan.rename_reason`.

Refuses to rebuild a `--plan-id` that has already been executed (would orphan the `copy_log`). Use a different `--plan-id`.

### `photoindex plan-show [--plan-id NAME] [--limit N]`

Prints summary of an existing plan: parameters, total file count, per-disk and per-type counts, and the first N planned source→destination rows.

### `photoindex execute-plan --plan-id NAME [--mount LABEL=PATH ...] [--dest ROOT] [--limit N] [--yes]`

Copies every pending row of the plan from its source disk to the destination, computes SHA-256 in-flight, verifies against the indexed value, preserves source `mtime` and mode at the destination, and writes a `copy_log` row.

- `--mount LABEL=PATH` (repeatable) tells the executor where each source disk is currently mounted. If omitted, `/Volumes/<LABEL>` is auto-detected.
- `--dest` overrides the destination root that was recorded in the plan.
- Resumable: rows already verified in `copy_log` are skipped.
- If a destination file already exists with the correct SHA, the executor logs it as `dest_match` and moves on (recovers from interrupted prior runs).
- If a destination file exists with a *wrong* SHA, the executor refuses to overwrite — surfaces as `dest_mismatch`.

### `photoindex cleanup-orphans --plan-id NAME [--apply] [--dest ROOT] [--limit N]`

Lists files at the destination that aren't referenced by `copy_log` for `--plan-id`. By default it's a dry run; `--apply` deletes them and removes any matching `copy_log` rows from older plans. Skips `.DS_Store` and other macOS metadata.

### `photoindex verify-copies [--plan-id NAME] [--sample N]`

Re-reads every (or N random) destination file in `copy_log`, recomputes SHA-256, and compares against `dest_sha256`. Reports `ok`, `mismatch`, or `missing`. Use periodically to detect bit-rot at the destination.

## Index schema

Defined in [`src/photoindex/schema.sql`](src/photoindex/schema.sql). Key tables:

| Table              | Purpose                                                                 |
| ------------------ | ----------------------------------------------------------------------- |
| `disks`            | Logical disk identities (label, optional volume_uuid).                  |
| `photos`           | One row per file: hashes, EXIF, dimensions, source disk + path.         |
| `similar_pairs`    | `(a, b)` pairs at phash distance ≤ candidate_threshold (default 18).    |
| `manual_overrides` | User-confirmed `confirmed_dup` / `confirmed_distinct` decisions.        |
| `pair_links`       | Reserved for non-dedup linkages such as RAW+JPEG sidecar pairs.         |
| `plan_runs`        | One row per `--plan-id`: dest_root, layout, max_distance, built_at.     |
| `copy_plan`        | Per-plan: which photo, what destination relative path, rename reason.   |
| `copy_log`         | Per-plan: actually-copied files with verified destination SHA-256.      |

## Design notes

### Why d ≤ 8 by default for the plan?

Empirical calibration on the user's actual photos: at perceptual hash distance ≤ 8, virtually every match is a true save-copy or format conversion of the same image. The 9–14 band starts including legitimate burst shots from the same camera (consecutive frames of the same scene), which we don't want to silently dedup. The 14–18 band trends toward coincidental similarity. The `review-candidates` command exists for exactly the 9–14 zone.

### Why mirror layout by default?

It's the safest layout: `<disk_label>/<relative_path>` can never produce filename collisions because `(disk_id, relative_path)` is already unique in the index. `by-date` is useful for organization but introduces collisions that need rename suffixes.

### Greedy clustering vs connected components

An earlier implementation used connected-components on `similar_pairs` for grouping. That overgrouped burst sequences into bogus clusters (A~B, B~C, C~D where A is not actually similar to D). The current approach is greedy: pick the best photo, claim only the photos *directly* within threshold, repeat. This avoids transitive false positives.

### What if two source disks have the same relative_path?

That's fine — the natural key is `(disk_id, relative_path)`. Only same-disk same-path collisions are forbidden, which happens automatically because each disk has one `disk_id`.

## License & status

This is a personal-project tool. No license declared.
