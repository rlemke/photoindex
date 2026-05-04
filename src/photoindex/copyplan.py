from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from . import dedup
from .db import now_iso

_VALID_LAYOUTS = ("mirror", "by-date", "flat")
_YEAR_RE = re.compile(r"(19[8-9]\d|20[0-3]\d)")  # 1980..2039 anywhere in a path component
_MAX_PATH_COMPONENT = 200  # macOS NAME_MAX is 255; leave headroom for filename appended after


def _safe_path_component(name: str, max_len: int = _MAX_PATH_COMPONENT) -> str:
    """Truncate an oversize path component to fit the filesystem's NAME_MAX,
    appending a short SHA1 hash so two long names that share a prefix don't collide."""
    if len(name) <= max_len:
        return name
    h = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return name[: max_len - 9] + "_" + h


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


def _flat_dest(p: sqlite3.Row) -> str:
    return _safe_path_component(p["filename"])


def _by_date_dest(p: sqlite3.Row) -> str:
    filename = _safe_path_component(p["filename"])
    ym = _exif_year_month(p["exif_datetime"])
    if ym:
        y, m = ym
        return f"{y}/{y}-{m:02d}/{filename}"

    year = _year_from_folder_path(p["relative_path"])
    parent = p["relative_path"].rsplit("/", 1)[0] if "/" in p["relative_path"] else ""
    folder_label = _safe_path_component(parent.split("/")[-1] if parent else "_root")
    if year is not None:
        return f"{year}/{folder_label}/{filename}"

    flat_parent = _safe_path_component(parent.replace("/", "_") or "_root")
    return f"unsorted/{flat_parent}/{filename}"


def _resolve_collision(
    dest: str, used: set[str], style: str = "paren"
) -> tuple[str, str | None]:
    if dest not in used:
        return dest, None
    head, _, tail = dest.rpartition("/")
    if "." in tail:
        stem, _, ext = tail.rpartition(".")
        ext = "." + ext
    else:
        stem, ext = tail, ""
    n = 1 if style == "underscore" else 2
    while True:
        if style == "underscore":
            candidate_tail = f"{stem}_{n:02d}{ext}"
        else:
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
    dropped_excluded: int
    estimated_bytes: int


def build_plan(
    conn: sqlite3.Connection,
    plan_run_id: str,
    dest_root: str,
    max_distance: int = 8,
    layout: str = "mirror",
    exclude_disk_labels: list[str] | None = None,
) -> BuildStats:
    """Build a fresh copy plan in copy_plan / plan_runs.

    For each "unique" photo in the index, write one copy_plan row with
    `dest_relative_path` derived from `layout`. Uniqueness is decided by:

    * For images with a perceptual hash: the canonical of each near-dup
      group at phash distance <= max_distance is kept; the other group
      members are dropped from the plan.
    * For files without a perceptual hash (e.g. video): the first instance
      of each SHA-256 is kept.
    * Photos that aren't part of any group are kept as singletons.

    If `exclude_disk_labels` is given, drop any photo whose source disk
    is on that list, OR whose SHA-256 matches any photo on those disks,
    OR whose phash is within `max_distance` of any photo on those disks.
    Use case: produce a "missing" plan against an external reference (e.g.
    a Google Photos export) — the result is everything not already there.

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

    excluded_disk_ids: set[int] = set()
    excluded_shas: set[str] = set()
    excluded_phash_match_ids: set[int] = set()
    if exclude_disk_labels:
        placeholders = ",".join("?" * len(exclude_disk_labels))
        rows = conn.execute(
            f"SELECT id, label FROM disks WHERE label IN ({placeholders})",
            list(exclude_disk_labels),
        ).fetchall()
        found = {r["label"]: r["id"] for r in rows}
        missing_labels = set(exclude_disk_labels) - set(found.keys())
        if missing_labels:
            raise ValueError(
                f"unknown disk label(s) for exclusion: {sorted(missing_labels)}"
            )
        excluded_disk_ids = set(found.values())
        ids_csv = ",".join(str(i) for i in excluded_disk_ids)
        excluded_shas = {
            r["sha256"]
            for r in conn.execute(
                f"SELECT DISTINCT sha256 FROM photos WHERE disk_id IN ({ids_csv})"
            )
        }
        excluded_phash_match_ids = {
            r["photo_id"]
            for r in conn.execute(
                f"""
                SELECT sp.photo_a_id AS photo_id
                FROM similar_pairs sp
                JOIN photos pb ON pb.id = sp.photo_b_id
                WHERE pb.disk_id IN ({ids_csv}) AND sp.phash_distance <= ?
                UNION
                SELECT sp.photo_b_id AS photo_id
                FROM similar_pairs sp
                JOIN photos pa ON pa.id = sp.photo_a_id
                WHERE pa.disk_id IN ({ids_csv}) AND sp.phash_distance <= ?
                """,
                (max_distance, max_distance),
            )
        }

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
    dropped_excluded = 0
    estimated_bytes = 0
    collision_style = "underscore" if layout == "flat" else "paren"

    now = now_iso()
    for p in photos:
        if (
            p["disk_id"] in excluded_disk_ids
            or p["sha256"] in excluded_shas
            or p["id"] in excluded_phash_match_ids
        ):
            dropped_excluded += 1
            continue

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
        elif layout == "flat":
            dest_base = _flat_dest(p)
        else:  # by-date
            dest_base = _by_date_dest(p)
        dest, rename_reason = _resolve_collision(
            dest_base, used_dest_paths, style=collision_style
        )
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
        dropped_excluded=dropped_excluded,
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
