# Copyright 2026 Ralph Lemke
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import click

from . import copyexec, copyplan, db, dedup, maintenance, scanner

DEFAULT_DB_PATH = Path.home() / ".photoindex" / "index.sqlite"


@click.group()
@click.option(
    "--db",
    "db_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=DEFAULT_DB_PATH,
    show_default=True,
    help="Path to the SQLite index file.",
)
@click.pass_context
def main(ctx: click.Context, db_path: Path) -> None:
    """Index and deduplicate photos across multiple external disks."""
    ctx.ensure_object(dict)
    ctx.obj["db_path"] = db_path


@main.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--disk-label", required=True, help="Logical name for this source disk.")
@click.option("--volume-uuid", default=None, help="Optional volume UUID for stable identification.")
@click.option(
    "--disk-prefix",
    default="",
    help="Path prefix (relative to disk root) to prepend to scanned paths, e.g. "
    "'Pictures/2024' if you're scanning /Volumes/D1/Pictures/2024 of disk D1.",
)
@click.pass_context
def scan(
    ctx: click.Context, path: Path, disk_label: str, volume_uuid: str | None, disk_prefix: str
) -> None:
    """Scan a directory tree and index all photos found."""
    conn = db.connect(ctx.obj["db_path"])
    disk_id = db.upsert_disk(conn, disk_label, volume_uuid)

    def progress(file_path: Path, stats: scanner.ScanStats) -> None:
        if stats.seen % 50 == 0:
            click.echo(
                f"\r  seen={stats.seen} inserted={stats.inserted} "
                f"updated={stats.updated} skipped={stats.skipped_unknown} "
                f"hash_errors={stats.hash_errors}",
                nl=False,
                err=True,
            )

    click.echo(f"Scanning {path} as disk '{disk_label}'...", err=True)
    stats = scanner.scan(conn, disk_id, path, progress=progress, disk_prefix=disk_prefix)
    click.echo("", err=True)
    click.echo(
        f"Done. seen={stats.seen} inserted={stats.inserted} "
        f"updated={stats.updated} skipped={stats.skipped_unknown} "
        f"hash_errors={stats.hash_errors}"
    )


@main.command()
@click.pass_context
def stats(ctx: click.Context) -> None:
    """Show counts in the index, broken down by disk and file type."""
    conn = db.connect(ctx.obj["db_path"])
    rows = conn.execute(
        """
        SELECT d.label AS disk, p.file_type, COUNT(*) AS n
        FROM photos p
        JOIN disks d ON d.id = p.disk_id
        GROUP BY d.label, p.file_type
        ORDER BY d.label, p.file_type
        """
    ).fetchall()
    if not rows:
        click.echo("Index is empty.")
        return
    click.echo(f"{'disk':<20} {'type':<10} {'count':>8}")
    for r in rows:
        click.echo(f"{r['disk']:<20} {r['file_type']:<10} {r['n']:>8}")
    total = conn.execute("SELECT COUNT(*) AS n FROM photos").fetchone()["n"]
    click.echo(f"{'TOTAL':<20} {'':<10} {total:>8}")


@main.command("find-dups")
@click.option(
    "--candidate-threshold",
    type=int,
    default=dedup.DEFAULT_CANDIDATE_THRESHOLD,
    show_default=True,
    help="Pairs with phash distance <= this are stored.",
)
@click.option(
    "--confirmed-threshold",
    type=int,
    default=dedup.DEFAULT_CONFIRMED_THRESHOLD,
    show_default=True,
    help="Pairs with phash distance <= this are counted as confirmed.",
)
@click.pass_context
def find_dups(ctx: click.Context, candidate_threshold: int, confirmed_threshold: int) -> None:
    """Recompute the similar_pairs table from current photo phashes."""
    conn = db.connect(ctx.obj["db_path"])
    click.echo("Computing pairwise phash distances...", err=True)
    stats = dedup.find_near_pairs(
        conn,
        candidate_threshold=candidate_threshold,
        confirmed_threshold=confirmed_threshold,
    )
    click.echo(
        f"Compared {stats.photos_compared} photos "
        f"({stats.pairs_examined:,} pairs). "
        f"Stored {stats.pairs_kept} pairs at d<={candidate_threshold} "
        f"(confirmed d<={confirmed_threshold}: {stats.confirmed_pairs}, "
        f"candidate: {stats.candidate_pairs})."
    )


@main.command("groups")
@click.option(
    "--max-distance",
    type=int,
    default=dedup.DEFAULT_CONFIRMED_THRESHOLD,
    show_default=True,
    help="Pairs at or below this phash distance form a group.",
)
@click.option("--limit", type=int, default=None, help="Show at most N groups.")
@click.pass_context
def groups(ctx: click.Context, max_distance: int, limit: int | None) -> None:
    """List dedup groups (connected components in similar_pairs)."""
    conn = db.connect(ctx.obj["db_path"])
    n_total = 0
    n_files = 0
    for group in dedup.find_groups(conn, max_distance=max_distance):
        n_total += 1
        n_files += len(group.members)
        if limit is not None and n_total > limit:
            continue
        click.echo(
            f"\nGroup #{group.group_id}  ({len(group.members)} members)"
        )
        for m in group.members:
            tag = "KEEP" if m.is_canonical else "drop"
            dims = f"{m.width}x{m.height}" if m.width and m.height else "?x?"
            size_kb = m.file_size // 1024
            dist = (
                "  d=KEEP"
                if m.is_canonical
                else f"  d={m.phash_distance_to_canonical}"
            )
            click.echo(
                f"  {tag}  [{m.disk_label}] {m.relative_path}  "
                f"({dims}, {size_kb}KB, mtime={m.file_mtime[:10]}){dist}"
            )
    if n_total == 0:
        click.echo("No groups found at this threshold.")
        return
    click.echo(
        f"\nSummary: {n_total} groups covering {n_files} files "
        f"(would drop {n_files - n_total} duplicates)."
    )


def _format_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


@main.command("plan")
@click.option(
    "--dest",
    "dest_root",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Destination root for the future copy. Not written to until `execute` runs.",
)
@click.option(
    "--plan-id",
    "plan_run_id",
    default="default",
    show_default=True,
    help="Identifier for this plan run; re-running with the same id replaces the prior plan.",
)
@click.option(
    "--max-distance",
    type=int,
    default=8,
    show_default=True,
    help="phash distance threshold for grouping (must already exist in similar_pairs).",
)
@click.option(
    "--layout",
    type=click.Choice(["mirror", "by-date", "flat"]),
    default="mirror",
    show_default=True,
    help="mirror=preserve source paths under disk_label; "
    "by-date=group into YYYY/YYYY-MM/; "
    "flat=all files in dest_root, _NN suffix on collisions.",
)
@click.option(
    "--exclude-matches-disk",
    "exclude_disk_labels",
    multiple=True,
    metavar="LABEL",
    help="Drop any photo whose source disk is LABEL, OR whose SHA matches a "
    "photo on LABEL, OR whose phash is within --max-distance of a photo on "
    "LABEL. Repeatable. Use case: --exclude-matches-disk Google_Photos to "
    "produce a 'missing' plan.",
)
@click.pass_context
def plan_cmd(
    ctx: click.Context,
    dest_root: Path,
    plan_run_id: str,
    max_distance: int,
    layout: str,
    exclude_disk_labels: tuple[str, ...],
) -> None:
    """Build a dry-run copy plan from the current index."""
    conn = db.connect(ctx.obj["db_path"])
    n_pairs = conn.execute("SELECT COUNT(*) AS n FROM similar_pairs").fetchone()["n"]
    if n_pairs == 0:
        click.echo(
            "WARNING: similar_pairs is empty — run `photoindex find-dups` first "
            "or every photo will be treated as unique.",
            err=True,
        )

    try:
        stats = copyplan.build_plan(
            conn,
            plan_run_id=plan_run_id,
            dest_root=str(dest_root),
            max_distance=max_distance,
            layout=layout,
            exclude_disk_labels=list(exclude_disk_labels) or None,
        )
    except copyplan.PlanAlreadyExecutedError as e:
        click.echo(f"ERROR: {e}", err=True)
        ctx.exit(1)
    except ValueError as e:
        click.echo(f"ERROR: {e}", err=True)
        ctx.exit(1)
    click.echo(f"Plan '{plan_run_id}' built.")
    click.echo(f"  total photos in index:  {stats.total_photos}")
    click.echo(f"  to copy:                {stats.plan_size}")
    click.echo(f"  dropped (phash dedup):  {stats.dropped_phash}")
    click.echo(f"  dropped (sha-only):     {stats.dropped_sha_only}")
    if exclude_disk_labels:
        click.echo(
            f"  dropped (excluded):     {stats.dropped_excluded}  "
            f"(matches {', '.join(exclude_disk_labels)})"
        )
    click.echo(f"  estimated bytes:        {_format_bytes(stats.estimated_bytes)}")
    click.echo(f"  destination root:       {dest_root}")


@main.command("plan-show")
@click.option("--plan-id", "plan_run_id", default="default", show_default=True)
@click.option("--limit", type=int, default=20, show_default=True, help="Sample plan rows to print.")
@click.pass_context
def plan_show_cmd(ctx: click.Context, plan_run_id: str, limit: int) -> None:
    """Print a summary of a stored copy plan."""
    conn = db.connect(ctx.obj["db_path"])
    summary = copyplan.get_plan_summary(conn, plan_run_id)
    if not summary:
        click.echo(f"No plan with id '{plan_run_id}'.", err=True)
        ctx.exit(1)

    run = summary["run"]
    click.echo(
        f"Plan: {run['plan_run_id']}    built: {run['built_at']}    "
        f"layout: {run['layout']}    max_distance: {run['max_distance']}"
    )
    click.echo(f"Destination root: {run['dest_root']}")
    click.echo(f"Total to copy: {summary['total']}")

    click.echo("\nBy disk:")
    for r in summary["by_disk"]:
        click.echo(f"  {r['disk']:<20} {r['n']:>6}  {_format_bytes(r['bytes'] or 0)}")

    click.echo("\nBy file type:")
    for r in summary["by_type"]:
        click.echo(f"  {r['file_type']:<10} {r['n']:>6}")

    click.echo(f"\nSample plan rows (first {limit}):")
    for i, row in enumerate(copyplan.iter_plan_rows(conn, plan_run_id)):
        if i >= limit:
            break
        rename = f"  [rename: {row.rename_reason}]" if row.rename_reason else ""
        click.echo(
            f"  [{row.disk_label}] {row.source_relative_path}"
            f"  ->  {row.dest_relative_path}{rename}"
        )


@main.command("review-candidates")
@click.option("--min-distance", type=int, default=9, show_default=True)
@click.option("--max-distance", type=int, default=14, show_default=True)
@click.option("--limit", type=int, default=30, show_default=True)
@click.option("--strong-only", is_flag=True, help="Show only pairs flagged STRONG by the heuristic.")
@click.pass_context
def review_candidates_cmd(
    ctx: click.Context,
    min_distance: int,
    max_distance: int,
    limit: int,
    strong_only: bool,
) -> None:
    """List borderline candidate pairs (phash distance in [min, max]) for manual review."""
    conn = db.connect(ctx.obj["db_path"])
    shown = 0
    for cp in dedup.iter_candidates(
        conn, min_distance=min_distance, max_distance=max_distance, limit=None
    ):
        if strong_only and (cp.hint is None or not cp.hint.startswith("STRONG")):
            continue
        if shown >= limit:
            click.echo(f"\n... (showing first {limit}; use --limit to see more)")
            break
        shown += 1
        click.echo(
            f"\n[{shown}] phash={cp.phash_distance} dhash={cp.dhash_distance}"
        )
        for tag, m in (("a", cp.a), ("b", cp.b)):
            dims = f"{m.width}x{m.height}" if m.width and m.height else "?x?"
            kb = m.file_size // 1024
            exif = m.exif_datetime or "no-exif"
            cam = (
                f"{m.camera_make} {m.camera_model}".strip()
                if m.camera_make or m.camera_model
                else "no-camera"
            )
            click.echo(
                f"   {tag} (id={m.photo_id}): [{m.disk_label}] {m.relative_path}"
            )
            click.echo(f"        {dims}  {kb}KB  exif={exif}  cam={cam}")
        if cp.hint:
            click.echo(f"   hint: {cp.hint}")
        click.echo(
            f"   -> photoindex confirm-pair {cp.a.photo_id} {cp.b.photo_id}    "
            f"(or separate-pair to mark distinct)"
        )

    if shown == 0:
        click.echo("No candidates in range.")


def _record_pair(
    ctx: click.Context, photo_id_a: int, photo_id_b: int, status: str, reason: str | None
) -> None:
    conn = db.connect(ctx.obj["db_path"])
    for pid in (photo_id_a, photo_id_b):
        if conn.execute("SELECT 1 FROM photos WHERE id = ?", (pid,)).fetchone() is None:
            click.echo(f"No photo with id {pid}.", err=True)
            ctx.exit(1)
    dedup.set_manual_override(conn, photo_id_a, photo_id_b, status, reason)
    click.echo(f"Recorded: {status} for ({photo_id_a}, {photo_id_b}).")


@main.command("confirm-pair")
@click.argument("photo_id_a", type=int)
@click.argument("photo_id_b", type=int)
@click.option("--reason", default=None)
@click.pass_context
def confirm_pair_cmd(ctx, photo_id_a, photo_id_b, reason):
    """Mark two photos as duplicates (overrides distance threshold during grouping)."""
    _record_pair(ctx, photo_id_a, photo_id_b, "confirmed_dup", reason)


@main.command("separate-pair")
@click.argument("photo_id_a", type=int)
@click.argument("photo_id_b", type=int)
@click.option("--reason", default=None)
@click.pass_context
def separate_pair_cmd(ctx, photo_id_a, photo_id_b, reason):
    """Mark two photos as definitely distinct (excluded from grouping even if close)."""
    _record_pair(ctx, photo_id_a, photo_id_b, "confirmed_distinct", reason)


@main.command("execute-plan")
@click.option("--plan-id", "plan_run_id", default="default", show_default=True)
@click.option(
    "--mount",
    "mount_specs",
    multiple=True,
    help="Disk mount mapping in label=path form. Repeatable. Falls back to /Volumes/<label>.",
)
@click.option(
    "--dest",
    "dest_override",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override destination root from the plan.",
)
@click.option("--limit", type=int, default=None, help="Process at most N pending rows.")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.pass_context
def execute_plan_cmd(
    ctx: click.Context,
    plan_run_id: str,
    mount_specs: tuple[str, ...],
    dest_override: Path | None,
    limit: int | None,
    yes: bool,
) -> None:
    """Copy files referenced by a stored plan, verifying SHA-256 at the destination."""
    mounts: dict[str, Path] = {}
    for spec in mount_specs:
        if "=" not in spec:
            raise click.BadParameter(f"--mount must be label=path, got {spec!r}")
        label, path = spec.split("=", 1)
        mounts[label] = Path(path)

    conn = db.connect(ctx.obj["db_path"])
    run = conn.execute(
        "SELECT * FROM plan_runs WHERE plan_run_id = ?", (plan_run_id,)
    ).fetchone()
    if run is None:
        click.echo(f"No plan with id '{plan_run_id}'.", err=True)
        ctx.exit(1)
    dest_root = dest_override if dest_override else Path(run["dest_root"])

    plan_disks = [
        r["label"]
        for r in conn.execute(
            """
            SELECT DISTINCT d.label FROM copy_plan cp
            JOIN photos p ON p.id = cp.photo_id
            JOIN disks d ON d.id = p.disk_id
            WHERE cp.plan_run_id = ?
            """,
            (plan_run_id,),
        ).fetchall()
    ]
    for label in plan_disks:
        if label not in mounts:
            candidate = Path(f"/Volumes/{label}")
            if candidate.is_dir():
                mounts[label] = candidate
                click.echo(f"  auto-detected mount: {label} -> {candidate}", err=True)

    pending = conn.execute(
        """
        SELECT COUNT(*) AS n FROM copy_plan cp
        WHERE cp.plan_run_id = ?
          AND NOT EXISTS (SELECT 1 FROM copy_log cl WHERE cl.plan_id = cp.id AND cl.verified = 1)
        """,
        (plan_run_id,),
    ).fetchone()["n"]

    if pending == 0:
        click.echo("Nothing to do — all plan rows already in copy_log.")
        return

    n = pending if limit is None else min(pending, limit)
    click.echo(f"About to copy up to {n} files to {dest_root}")
    if not yes:
        click.confirm("Continue?", abort=True)

    stats = copyexec.ExecuteStats()
    for result in copyexec.execute_plan(
        conn, plan_run_id, mounts, dest_root=dest_root, limit=limit
    ):
        stats.total += 1
        if result.status == "copied":
            stats.copied += 1
        elif result.status == "dest_match":
            stats.dest_match += 1
        elif result.status == "missing_mount":
            stats.missing_mount += 1
        elif result.status == "missing_source":
            stats.missing_source += 1
        elif result.status == "sha_mismatch":
            stats.sha_mismatch += 1
        elif result.status == "dest_mismatch":
            stats.dest_mismatch += 1
        else:
            stats.errors += 1

        if result.status not in {"copied", "dest_match"}:
            click.echo(
                f"  [{result.status}] {result.disk_label}/{result.source_rel}: {result.error}",
                err=True,
            )
        if stats.total % 25 == 0:
            click.echo(
                f"\r  copied={stats.copied}/{n} dest_match={stats.dest_match} "
                f"missing_source={stats.missing_source} errors={stats.sha_mismatch + stats.errors}",
                nl=False,
                err=True,
            )

    click.echo("", err=True)
    click.echo(
        f"Done. copied={stats.copied} dest_match={stats.dest_match} "
        f"missing_mount={stats.missing_mount} missing_source={stats.missing_source} "
        f"sha_mismatch={stats.sha_mismatch} dest_mismatch={stats.dest_mismatch} "
        f"errors={stats.errors}"
    )


@main.command("cleanup-orphans")
@click.option("--plan-id", "plan_run_id", default="v2", show_default=True,
              help="Plan whose copy_log defines the canonical set; orphans = dest files not in it.")
@click.option("--dest", "dest_override", type=click.Path(file_okay=False, path_type=Path), default=None)
@click.option("--apply", is_flag=True, help="Actually delete orphans (default is dry-run).")
@click.option("--limit", type=int, default=None, help="Cap number of deletions.")
@click.pass_context
def cleanup_orphans_cmd(
    ctx: click.Context,
    plan_run_id: str,
    dest_override: Path | None,
    apply: bool,
    limit: int | None,
) -> None:
    """List or delete dest files not referenced by an active plan."""
    conn = db.connect(ctx.obj["db_path"])
    run = conn.execute(
        "SELECT * FROM plan_runs WHERE plan_run_id = ?", (plan_run_id,)
    ).fetchone()
    if run is None:
        click.echo(f"No plan with id '{plan_run_id}'.", err=True)
        ctx.exit(1)
    dest_root = dest_override if dest_override else Path(run["dest_root"])

    if not dest_root.is_dir():
        click.echo(f"Destination root does not exist: {dest_root}", err=True)
        ctx.exit(1)

    orphans = maintenance.find_orphans(conn, plan_run_id, dest_root)
    if not orphans:
        click.echo(f"No orphans under {dest_root} (plan '{plan_run_id}').")
        return

    click.echo(f"Found {len(orphans)} orphan files under {dest_root} (not in plan '{plan_run_id}').")
    for p in orphans[:10]:
        click.echo(f"  {p}")
    if len(orphans) > 10:
        click.echo(f"  ... and {len(orphans) - 10} more")

    if not apply:
        click.echo("\n(dry-run; pass --apply to actually delete)")
        return

    stats = maintenance.delete_orphans(conn, orphans, limit=limit)
    click.echo(
        f"Deleted {stats.deleted}/{stats.found} orphans"
        f"{' (failed: ' + str(stats.failed) + ')' if stats.failed else ''}."
    )


@main.command("verify-copies")
@click.option("--plan-id", "plan_run_id", default=None,
              help="Restrict to one plan_run_id (default: all).")
@click.option("--sample", type=int, default=None,
              help="Verify a random sample of N files (default: all).")
@click.pass_context
def verify_copies_cmd(ctx: click.Context, plan_run_id: str | None, sample: int | None) -> None:
    """Re-SHA destination files and report mismatches/missing."""
    conn = db.connect(ctx.obj["db_path"])
    n_ok = n_mismatch = n_missing = 0
    n_total = 0
    for r in maintenance.verify_copies(conn, plan_run_id=plan_run_id, sample=sample):
        n_total += 1
        if r.status == "ok":
            n_ok += 1
        elif r.status == "mismatch":
            n_mismatch += 1
            click.echo(
                f"  MISMATCH  {r.dest_path}\n"
                f"    expected={r.expected_sha[:16]}... actual={r.actual_sha[:16]}..."
            )
        else:
            n_missing += 1
            click.echo(f"  MISSING   {r.dest_path}")
        if n_total % 250 == 0:
            click.echo(
                f"\r  checked={n_total} ok={n_ok} mismatch={n_mismatch} missing={n_missing}",
                nl=False, err=True,
            )
    click.echo("", err=True)
    click.echo(f"Done. checked={n_total} ok={n_ok} mismatch={n_mismatch} missing={n_missing}")


if __name__ == "__main__":
    main()
