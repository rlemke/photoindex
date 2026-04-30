from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from . import dedup
from .db import now_iso

_VALID_LAYOUTS = ("mirror", "by-date")
_YEAR_RE = re.compile(r"(19[8-9]\d|20[0-3]\d)")  # 1980..2039 anywhere in a path component


def _exif_year_month(exif_iso: str | None) -> tuple[int, int] | None:
    if not exif_iso:
        return None
    try:
        dt = datetime.fromisoformat(exif_iso)
    except ValueError:
        return None
    if not (1980 <= dt.year <= 2039):
        return None
    return dt.year, dt.month


def _year_from_folder_path(rel_path: str) -> int | None:
    """First 4-digit year (1980..2039) found in any path component except the filename."""
    parts = rel_path.split("/")
    for part in parts[:-1]:
        m = _YEAR_RE.search(part)
        if m:
            return int(m.group(0))
    return None


def _by_date_dest(p: sqlite3.Row) -> str:
    ym = _exif_year_month(p["exif_datetime"])
    if ym:
        y, m = ym
        return f"{y}/{y}-{m:02d}/{p['filename']}"

    year = _year_from_folder_path(p["relative_path"])
    parent = p["relative_path"].rsplit("/", 1)[0] if "/" in p["relative_path"] else ""
    folder_label = parent.split("/")[-1] if parent else "_root"
    if year is not None:
        return f"{year}/{folder_label}/{p['filename']}"

    flat_parent = parent.replace("/", "_") or "_root"
    return f"unsorted/{flat_parent}/{p['filename']}"


def _resolve_collision(dest: str, used: set[str]) -> tuple[str, str | None]:
    if dest not in used:
        return dest, None
    head, _, tail = dest.rpartition("/")
    if "." in tail:
        stem, _, ext = tail.rpartition(".")
        ext = "." + ext
    else:
        stem, ext = tail, ""
    n = 2
    while True:
        candidate_tail = f"{stem} ({n}){ext}"
        candidate = f"{head}/{candidate_tail}" if head else candidate_tail
        if candidate not in used:
            return candidate, f"collision: original was {dest}"
        n += 1


class PlanAlreadyExecutedError(Exception):
    """Raised when build_plan would orphan copy_log rows."""


@dataclass
class BuildStats:
    total_photos: int
    plan_size: int
    dropped_phash: int
    dropped_sha_only: int
    estimated_bytes: int


def build_plan(
    conn: sqlite3.Connection,
    plan_run_id: str,
    dest_root: str,
    max_distance: int = 8,
    layout: str = "mirror",
) -> BuildStats:
    """Build a fresh copy plan in copy_plan / plan_runs.

    For each "unique" photo in the index, write one copy_plan row with
    `dest_relative_path = "<disk_label>/<source_relative_path>"`. Uniqueness
    is decided by:

    * For images with a perceptual hash: the canonical of each near-dup
      group at phash distance <= max_distance is kept; the other group
      members are dropped from the plan.
    * For files without a perceptual hash (e.g. video): the first instance
      of each SHA-256 is kept.
    * Photos that aren't part of any group are kept as singletons.

    Idempotent: re-running with the same plan_run_id replaces the prior plan.
    """
    if layout not in _VALID_LAYOUTS:
        raise ValueError(f"unknown layout {layout!r}; choose from {_VALID_LAYOUTS}")

    n_logged = conn.execute(
        """
        SELECT COUNT(*) AS n FROM copy_log cl
        JOIN copy_plan cp ON cp.id = cl.plan_id
        WHERE cp.plan_run_id = ?
        """,
        (plan_run_id,),
    ).fetchone()["n"]
    if n_logged > 0:
        raise PlanAlreadyExecutedError(
            f"plan {plan_run_id!r} has {n_logged} rows in copy_log. "
            f"Rebuilding would orphan the ledger. "
            f"Use a different --plan-id (e.g. '{plan_run_id}_v2')."
        )

    n_pairs = conn.execute("SELECT COUNT(*) AS n FROM similar_pairs").fetchone()["n"]
    if n_pairs == 0:
        # Plan would copy everything — almost certainly a forgotten `find-dups` step.
        # We still allow it; the caller surfaces the warning.
        pass

    grouped_member_ids: set[int] = set()
    canonical_ids: set[int] = set()
    for group in dedup.find_groups(conn, max_distance=max_distance):
        canonical_ids.add(group.canonical.photo_id)
        for m in group.members:
            grouped_member_ids.add(m.photo_id)

    photos = conn.execute(
        """
        SELECT p.id, p.disk_id, d.label AS disk_label, p.relative_path,
               p.filename, p.sha256, p.phash, p.file_type, p.file_size,
               p.exif_datetime
        FROM photos p
        JOIN disks d ON d.id = p.disk_id
        ORDER BY p.id
        """
    ).fetchall()

    plan_rows: list[tuple] = []
    seen_sha_for_no_phash: set[str] = set()
    used_dest_paths: set[str] = set()
    dropped_phash = 0
    dropped_sha_only = 0
    estimated_bytes = 0

    now = now_iso()
    for p in photos:
        if p["phash"] is not None:
            if p["id"] in grouped_member_ids and p["id"] not in canonical_ids:
                dropped_phash += 1
                continue
        else:
            if p["sha256"] in seen_sha_for_no_phash:
                dropped_sha_only += 1
                continue
            seen_sha_for_no_phash.add(p["sha256"])

        if layout == "mirror":
            dest_base = f"{p['disk_label']}/{p['relative_path']}"
        else:  # by-date
            dest_base = _by_date_dest(p)
        dest, rename_reason = _resolve_collision(dest_base, used_dest_paths)
        used_dest_paths.add(dest)
        plan_rows.append((plan_run_id, p["id"], dest, rename_reason, now))
        estimated_bytes += p["file_size"]

    with conn:
        conn.execute("DELETE FROM copy_plan WHERE plan_run_id = ?", (plan_run_id,))
        conn.execute(
            """
            INSERT INTO plan_runs (plan_run_id, dest_root, layout, max_distance, built_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(plan_run_id) DO UPDATE SET
                dest_root = excluded.dest_root,
                layout = excluded.layout,
                max_distance = excluded.max_distance,
                built_at = excluded.built_at
            """,
            (plan_run_id, dest_root, layout, max_distance, now),
        )
        conn.executemany(
            """
            INSERT INTO copy_plan
                (plan_run_id, photo_id, dest_relative_path, rename_reason, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            plan_rows,
        )

    return BuildStats(
        total_photos=len(photos),
        plan_size=len(plan_rows),
        dropped_phash=dropped_phash,
        dropped_sha_only=dropped_sha_only,
        estimated_bytes=estimated_bytes,
    )


@dataclass
class PlanRow:
    photo_id: int
    disk_label: str
    source_relative_path: str
    dest_relative_path: str
    file_type: str
    file_size: int
    rename_reason: str | None


def get_plan_summary(conn: sqlite3.Connection, plan_run_id: str) -> dict:
    run = conn.execute(
        "SELECT * FROM plan_runs WHERE plan_run_id = ?", (plan_run_id,)
    ).fetchone()
    if run is None:
        return {}
    by_disk = conn.execute(
        """
        SELECT d.label AS disk, COUNT(*) AS n, SUM(p.file_size) AS bytes
        FROM copy_plan cp
        JOIN photos p ON p.id = cp.photo_id
        JOIN disks d ON d.id = p.disk_id
        WHERE cp.plan_run_id = ?
        GROUP BY d.label
        ORDER BY d.label
        """,
        (plan_run_id,),
    ).fetchall()
    by_type = conn.execute(
        """
        SELECT p.file_type AS file_type, COUNT(*) AS n
        FROM copy_plan cp
        JOIN photos p ON p.id = cp.photo_id
        WHERE cp.plan_run_id = ?
        GROUP BY p.file_type
        ORDER BY n DESC
        """,
        (plan_run_id,),
    ).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM copy_plan WHERE plan_run_id = ?", (plan_run_id,)
    ).fetchone()["n"]
    return {
        "run": dict(run),
        "by_disk": [dict(r) for r in by_disk],
        "by_type": [dict(r) for r in by_type],
        "total": total,
    }


def iter_plan_rows(conn: sqlite3.Connection, plan_run_id: str):
    cur = conn.execute(
        """
        SELECT cp.photo_id, d.label AS disk_label, p.relative_path AS source_rel,
               cp.dest_relative_path, p.file_type, p.file_size, cp.rename_reason
        FROM copy_plan cp
        JOIN photos p ON p.id = cp.photo_id
        JOIN disks d ON d.id = p.disk_id
        WHERE cp.plan_run_id = ?
        ORDER BY d.label, p.relative_path
        """,
        (plan_run_id,),
    )
    for r in cur:
        yield PlanRow(
            photo_id=r["photo_id"],
            disk_label=r["disk_label"],
            source_relative_path=r["source_rel"],
            dest_relative_path=r["dest_relative_path"],
            file_type=r["file_type"],
            file_size=r["file_size"],
            rename_reason=r["rename_reason"],
        )
