# Copyright 2026 Ralph Lemke
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from . import hashing
from .db import now_iso

_CHUNK = 1024 * 1024


@dataclass
class CopyResult:
    plan_id: int
    photo_id: int
    disk_label: str
    source_rel: str
    dest_rel: str
    status: str
    dest_abs: Path | None = None
    dest_sha: str | None = None
    error: str | None = None


@dataclass
class ExecuteStats:
    total: int = 0
    copied: int = 0
    dest_match: int = 0
    missing_mount: int = 0
    missing_source: int = 0
    sha_mismatch: int = 0
    dest_mismatch: int = 0
    errors: int = 0


def _stream_copy_with_sha(source: Path, dest: Path) -> str:
    h = hashlib.sha256()
    with source.open("rb") as src, dest.open("wb") as dst:
        while chunk := src.read(_CHUNK):
            dst.write(chunk)
            h.update(chunk)
    return h.hexdigest()


def _log_copy(
    conn: sqlite3.Connection,
    plan_id: int,
    disk_label: str,
    source_rel: str,
    dest_abs: Path,
    dest_sha: str,
) -> None:
    conn.execute(
        """
        INSERT INTO copy_log
            (plan_id, source_disk_label, source_relative_path,
             dest_absolute_path, dest_sha256, copied_at, verified)
        VALUES (?, ?, ?, ?, ?, ?, 1)
        """,
        (plan_id, disk_label, source_rel, str(dest_abs), dest_sha, now_iso()),
    )


def execute_plan(
    conn: sqlite3.Connection,
    plan_run_id: str,
    mounts: dict[str, Path],
    dest_root: Path | None = None,
    limit: int | None = None,
) -> Iterator[CopyResult]:
    """Yield a CopyResult for each plan row processed. Side-effect: writes copy_log
    rows for successful copies and 'dest_match' (already present) cases.

    Resumable: rows already verified in copy_log are skipped.
    """
    run = conn.execute(
        "SELECT * FROM plan_runs WHERE plan_run_id = ?", (plan_run_id,)
    ).fetchone()
    if run is None:
        raise ValueError(f"no plan with id {plan_run_id!r}")
    if dest_root is None:
        dest_root = Path(run["dest_root"])

    sql = """
        SELECT cp.id AS plan_id, cp.photo_id, cp.dest_relative_path,
               d.label AS disk_label, p.relative_path AS source_rel,
               p.sha256 AS expected_sha
        FROM copy_plan cp
        JOIN photos p ON p.id = cp.photo_id
        JOIN disks d ON d.id = p.disk_id
        WHERE cp.plan_run_id = ?
          AND NOT EXISTS (
              SELECT 1 FROM copy_log cl WHERE cl.plan_id = cp.id AND cl.verified = 1
          )
        ORDER BY d.label, p.relative_path
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"

    rows = conn.execute(sql, (plan_run_id,)).fetchall()

    for r in rows:
        plan_id = r["plan_id"]
        photo_id = r["photo_id"]
        disk_label = r["disk_label"]
        source_rel = r["source_rel"]
        dest_rel = r["dest_relative_path"]
        expected_sha = r["expected_sha"]

        def make(status: str, **kw) -> CopyResult:
            return CopyResult(
                plan_id=plan_id,
                photo_id=photo_id,
                disk_label=disk_label,
                source_rel=source_rel,
                dest_rel=dest_rel,
                status=status,
                **kw,
            )

        if disk_label not in mounts:
            yield make("missing_mount", error=f"no --mount for disk {disk_label!r}")
            continue

        source_abs = mounts[disk_label] / source_rel
        if not source_abs.is_file():
            yield make("missing_source", error=f"not found: {source_abs}")
            continue

        dest_abs = dest_root / dest_rel

        if dest_abs.exists():
            existing_sha = hashing.sha256_file(dest_abs)
            if existing_sha == expected_sha:
                _log_copy(conn, plan_id, disk_label, source_rel, dest_abs, existing_sha)
                conn.commit()
                yield make("dest_match", dest_abs=dest_abs, dest_sha=existing_sha)
                continue
            else:
                yield make(
                    "dest_mismatch",
                    dest_abs=dest_abs,
                    dest_sha=existing_sha,
                    error=f"existing dest sha {existing_sha} != expected {expected_sha}",
                )
                continue

        dest_abs.parent.mkdir(parents=True, exist_ok=True)
        try:
            dest_sha = _stream_copy_with_sha(source_abs, dest_abs)
        except OSError as e:
            yield make("errors", error=f"copy failed: {e}")
            continue

        if dest_sha != expected_sha:
            try:
                dest_abs.unlink()
            except OSError:
                pass
            yield make(
                "sha_mismatch",
                error=f"copied SHA {dest_sha} != expected {expected_sha}",
            )
            continue

        try:
            shutil.copystat(source_abs, dest_abs)
        except OSError:
            pass

        _log_copy(conn, plan_id, disk_label, source_rel, dest_abs, dest_sha)
        conn.commit()
        yield make("copied", dest_abs=dest_abs, dest_sha=dest_sha)
