# Copyright 2026 Ralph Lemke
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator

# Calibration on Maxine_Lemke_Album (810 photos): pairs at d<=14 are tight visual matches;
# 14<d<=18 needs corroboration; >18 trends toward coincidental similarity.
DEFAULT_CONFIRMED_THRESHOLD = 14
DEFAULT_CANDIDATE_THRESHOLD = 18


@dataclass
class FindDupsStats:
    photos_compared: int = 0
    pairs_examined: int = 0
    pairs_kept: int = 0
    confirmed_pairs: int = 0
    candidate_pairs: int = 0


def find_near_pairs(
    conn: sqlite3.Connection,
    candidate_threshold: int = DEFAULT_CANDIDATE_THRESHOLD,
    confirmed_threshold: int = DEFAULT_CONFIRMED_THRESHOLD,
) -> FindDupsStats:
    """Recompute similar_pairs from scratch using phash hamming distance.

    Uses numpy uint64 + np.bitwise_count for vectorized popcount, which is
    ~100x faster than per-pair Python at the scales we hit (200k+ photos).
    All pairs at phash distance <= candidate_threshold are written.
    """
    import numpy as np

    rows = conn.execute(
        "SELECT id, phash, dhash FROM photos WHERE phash IS NOT NULL ORDER BY id"
    ).fetchall()
    n = len(rows)

    ids = np.fromiter((r["id"] for r in rows), dtype=np.int64, count=n)
    phashes = np.fromiter(
        (int(r["phash"], 16) for r in rows), dtype=np.uint64, count=n
    )
    dhashes = np.zeros(n, dtype=np.uint64)
    has_dhash = np.zeros(n, dtype=bool)
    for i, r in enumerate(rows):
        if r["dhash"]:
            dhashes[i] = int(r["dhash"], 16)
            has_dhash[i] = True

    pairs_to_insert: list[tuple[int, int, int, int | None]] = []
    pairs_examined = n * (n - 1) // 2
    confirmed = 0
    candidate = 0

    for i in range(n - 1):
        diffs = phashes[i + 1 :] ^ phashes[i]
        d_p = np.bitwise_count(diffs)
        close = np.flatnonzero(d_p <= candidate_threshold)
        if close.size == 0:
            continue
        ai = int(ids[i])
        i_has_d = has_dhash[i]
        for k in close.tolist():
            j = i + 1 + k
            d_p_val = int(d_p[k])
            d_d_val: int | None = None
            if i_has_d and has_dhash[j]:
                d_d_val = int(np.bitwise_count(dhashes[i] ^ dhashes[j]))
            pairs_to_insert.append((ai, int(ids[j]), d_p_val, d_d_val))
            if d_p_val <= confirmed_threshold:
                confirmed += 1
            else:
                candidate += 1

    # Rebuild similar_pairs atomically — phashes may have changed since last run.
    with conn:
        conn.execute("DELETE FROM similar_pairs")
        conn.executemany(
            """
            INSERT INTO similar_pairs (photo_a_id, photo_b_id, phash_distance, dhash_distance)
            VALUES (?, ?, ?, ?)
            """,
            pairs_to_insert,
        )

    return FindDupsStats(
        photos_compared=n,
        pairs_examined=pairs_examined,
        pairs_kept=len(pairs_to_insert),
        confirmed_pairs=confirmed,
        candidate_pairs=candidate,
    )


# ----- Grouping -----


@dataclass
class PhotoBrief:
    photo_id: int
    disk_label: str
    relative_path: str
    filename: str
    width: int | None
    height: int | None
    file_size: int
    exif_datetime: str | None
    camera_make: str | None
    camera_model: str | None
    sha256: str


@dataclass
class CandidatePair:
    a: PhotoBrief
    b: PhotoBrief
    phash_distance: int
    dhash_distance: int | None
    hint: str | None


# Filenames that look like "save another copy" siblings: `foo(1).jpg`, `foo (2).jpg`, `foo_copy.jpg`.
_SUFFIX_RE = re.compile(r"^(?P<stem>.+?)(?:\s*\((\d+)\)|[_\-\s]copy)\.(?P<ext>[^.]+)$", re.IGNORECASE)


def _strip_save_suffix(filename: str) -> str:
    m = _SUFFIX_RE.match(filename)
    if m:
        return f"{m.group('stem')}.{m.group('ext')}".lower()
    return filename.lower()


def _exif_delta_seconds(a: str | None, b: str | None) -> float | None:
    if not a or not b:
        return None
    try:
        return abs((datetime.fromisoformat(a) - datetime.fromisoformat(b)).total_seconds())
    except ValueError:
        return None


def _candidate_hint(a: PhotoBrief, b: PhotoBrief, dhash_distance: int | None) -> str | None:
    same_dims = a.width and b.width and a.width == b.width and a.height == b.height
    same_camera = (
        a.camera_make and a.camera_make == b.camera_make
        and a.camera_model == b.camera_model
    )
    delta = _exif_delta_seconds(a.exif_datetime, b.exif_datetime)
    same_stem = _strip_save_suffix(a.filename) == _strip_save_suffix(b.filename)

    if same_stem and same_dims:
        return "STRONG: filenames look like save-copy siblings, same dimensions"
    if same_dims and delta == 0:
        return "STRONG: same dims, identical EXIF datetime — likely a re-save"
    if same_dims and delta is not None and delta <= 2 and same_camera:
        return f"BURST: same dims & camera, EXIF {delta:.0f}s apart — probably distinct shot"
    if same_dims and delta is not None and delta > 2 and same_camera:
        return f"BURST: EXIF {delta:.0f}s apart, same camera — probably distinct shot"
    if dhash_distance is not None and dhash_distance <= 4:
        return "MAYBE: dhash also close — visual content matches"
    return None


def iter_candidates(
    conn: sqlite3.Connection,
    min_distance: int = 9,
    max_distance: int = 14,
    limit: int | None = None,
) -> Iterator[CandidatePair]:
    """Yield pairs in [min_distance, max_distance] phash range that haven't been
    manually decided. Sorted by phash distance ascending (closest first)."""
    sql = """
        SELECT sp.photo_a_id, sp.photo_b_id, sp.phash_distance, sp.dhash_distance,
               pa.id AS a_id, da.label AS a_disk, pa.relative_path AS a_rel, pa.filename AS a_name,
               pa.width AS a_w, pa.height AS a_h, pa.file_size AS a_size,
               pa.exif_datetime AS a_exif, pa.exif_camera_make AS a_make,
               pa.exif_camera_model AS a_model, pa.sha256 AS a_sha,
               pb.id AS b_id, db.label AS b_disk, pb.relative_path AS b_rel, pb.filename AS b_name,
               pb.width AS b_w, pb.height AS b_h, pb.file_size AS b_size,
               pb.exif_datetime AS b_exif, pb.exif_camera_make AS b_make,
               pb.exif_camera_model AS b_model, pb.sha256 AS b_sha
        FROM similar_pairs sp
        JOIN photos pa ON pa.id = sp.photo_a_id
        JOIN photos pb ON pb.id = sp.photo_b_id
        JOIN disks da ON da.id = pa.disk_id
        JOIN disks db ON db.id = pb.disk_id
        WHERE sp.phash_distance >= ? AND sp.phash_distance <= ?
          AND NOT EXISTS (
              SELECT 1 FROM manual_overrides mo
              WHERE mo.photo_a_id = sp.photo_a_id AND mo.photo_b_id = sp.photo_b_id
          )
        ORDER BY sp.phash_distance ASC, sp.photo_a_id, sp.photo_b_id
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"

    for r in conn.execute(sql, (min_distance, max_distance)):
        a = PhotoBrief(
            photo_id=r["a_id"], disk_label=r["a_disk"], relative_path=r["a_rel"],
            filename=r["a_name"], width=r["a_w"], height=r["a_h"], file_size=r["a_size"],
            exif_datetime=r["a_exif"], camera_make=r["a_make"], camera_model=r["a_model"],
            sha256=r["a_sha"],
        )
        b = PhotoBrief(
            photo_id=r["b_id"], disk_label=r["b_disk"], relative_path=r["b_rel"],
            filename=r["b_name"], width=r["b_w"], height=r["b_h"], file_size=r["b_size"],
            exif_datetime=r["b_exif"], camera_make=r["b_make"], camera_model=r["b_model"],
            sha256=r["b_sha"],
        )
        yield CandidatePair(
            a=a,
            b=b,
            phash_distance=r["phash_distance"],
            dhash_distance=r["dhash_distance"],
            hint=_candidate_hint(a, b, r["dhash_distance"]),
        )


def set_manual_override(
    conn: sqlite3.Connection,
    photo_id_1: int,
    photo_id_2: int,
    status: str,
    reason: str | None = None,
) -> None:
    if status not in {"confirmed_dup", "confirmed_distinct"}:
        raise ValueError(f"unknown status {status!r}")
    if photo_id_1 == photo_id_2:
        raise ValueError("cannot mark a photo as a duplicate of itself")
    a, b = sorted((photo_id_1, photo_id_2))
    from .db import now_iso
    with conn:
        conn.execute(
            """
            INSERT INTO manual_overrides (photo_a_id, photo_b_id, status, reason, decided_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(photo_a_id, photo_b_id) DO UPDATE SET
                status = excluded.status,
                reason = excluded.reason,
                decided_at = excluded.decided_at
            """,
            (a, b, status, reason, now_iso()),
        )


@dataclass
class GroupMember:
    photo_id: int
    disk_label: str
    relative_path: str
    filename: str
    width: int | None
    height: int | None
    file_size: int
    exif_datetime: str | None
    file_mtime: str
    sha256: str
    is_canonical: bool = False
    phash_distance_to_canonical: int | None = None


@dataclass
class DupGroup:
    group_id: int
    members: list[GroupMember]

    @property
    def canonical(self) -> GroupMember:
        return next(m for m in self.members if m.is_canonical)


def _canonical_sort_key(m: GroupMember) -> tuple:
    pixels = (m.width or 0) * (m.height or 0)
    return (
        -pixels,
        -m.file_size,
        0 if m.exif_datetime else 1,
        m.file_mtime,
        m.photo_id,
    )


def find_groups(
    conn: sqlite3.Connection,
    max_distance: int = DEFAULT_CONFIRMED_THRESHOLD,
) -> Iterator[DupGroup]:
    """Yield greedy dedup groups: highest-quality unclaimed photo becomes a canonical
    and claims every still-unclaimed photo *directly* within max_distance of it.

    This avoids the false-positive clusters that connected-components forms across
    burst sequences (A~B~C where A is not actually similar to C).
    """
    photo_rows = {
        r["id"]: r
        for r in conn.execute(
            """
            SELECT p.id, d.label AS disk_label, p.relative_path, p.filename,
                   p.width, p.height, p.file_size, p.exif_datetime, p.file_mtime, p.sha256
            FROM photos p
            JOIN disks d ON d.id = p.disk_id
            WHERE p.phash IS NOT NULL
            """
        ).fetchall()
    }

    # User decisions override automated grouping.
    confirmed_dup_pairs: set[tuple[int, int]] = set()
    confirmed_distinct_pairs: set[tuple[int, int]] = set()
    for r in conn.execute("SELECT photo_a_id, photo_b_id, status FROM manual_overrides"):
        key = (r["photo_a_id"], r["photo_b_id"])
        if r["status"] == "confirmed_dup":
            confirmed_dup_pairs.add(key)
        else:
            confirmed_distinct_pairs.add(key)

    # Adjacency: photo_id -> [(neighbor_id, phash_distance), ...]
    neighbors: dict[int, list[tuple[int, int]]] = {pid: [] for pid in photo_rows}
    for r in conn.execute(
        "SELECT photo_a_id, photo_b_id, phash_distance FROM similar_pairs WHERE phash_distance <= ?",
        (max_distance,),
    ):
        a, b, d = r["photo_a_id"], r["photo_b_id"], r["phash_distance"]
        if (a, b) in confirmed_distinct_pairs:
            continue
        if a in neighbors and b in neighbors:
            neighbors[a].append((b, d))
            neighbors[b].append((a, d))

    # Manual confirmed_dup pairs always contribute to adjacency, regardless of distance.
    for a, b in confirmed_dup_pairs:
        if a not in neighbors or b not in neighbors:
            continue
        existing = conn.execute(
            "SELECT phash_distance FROM similar_pairs WHERE photo_a_id = ? AND photo_b_id = ?",
            (a, b),
        ).fetchone()
        d = existing["phash_distance"] if existing else 0
        if (b, d) not in neighbors[a]:
            neighbors[a].append((b, d))
        if (a, d) not in neighbors[b]:
            neighbors[b].append((a, d))

    def to_member(row, *, canonical: bool = False, dist: int | None = None) -> GroupMember:
        return GroupMember(
            photo_id=row["id"],
            disk_label=row["disk_label"],
            relative_path=row["relative_path"],
            filename=row["filename"],
            width=row["width"],
            height=row["height"],
            file_size=row["file_size"],
            exif_datetime=row["exif_datetime"],
            file_mtime=row["file_mtime"],
            sha256=row["sha256"],
            is_canonical=canonical,
            phash_distance_to_canonical=dist,
        )

    # Process candidates in canonical-priority order so the keeper gets to claim first.
    candidates = sorted(
        photo_rows.values(),
        key=lambda r: _canonical_sort_key(to_member(r)),
    )

    claimed: set[int] = set()
    next_group_id = 1
    for cand in candidates:
        cid = cand["id"]
        if cid in claimed:
            continue
        direct = [(nid, d) for nid, d in neighbors[cid] if nid not in claimed]
        if not direct:
            continue
        members = [to_member(cand, canonical=True)]
        for nid, d in sorted(direct, key=lambda x: x[1]):
            members.append(to_member(photo_rows[nid], dist=d))
        for m in members:
            claimed.add(m.photo_id)
        yield DupGroup(group_id=next_group_id, members=members)
        next_group_id += 1
