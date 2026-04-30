from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

# macOS scatters these alongside user content; never treat them as orphans.
_METADATA_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}


def _is_metadata(path: Path) -> bool:
    return path.name in _METADATA_NAMES or path.name.startswith("._")


@dataclass
class CleanupStats:
    found: int = 0
    deleted: int = 0
    failed: int = 0


def find_orphans(
    conn: sqlite3.Connection, plan_run_id: str, dest_root: Path
) -> list[Path]:
    """Files under dest_root that aren't in plan_run_id's copy_log."""
    expected: set[str] = {
        r["dest_absolute_path"]
        for r in conn.execute(
            """
            SELECT cl.dest_absolute_path FROM copy_log cl
            JOIN copy_plan cp ON cp.id = cl.plan_id
            WHERE cp.plan_run_id = ?
            """,
            (plan_run_id,),
        )
    }

    orphans: list[Path] = []
    for path in dest_root.rglob("*"):
        if not path.is_file():
            continue
        if _is_metadata(path):
            continue
        if str(path) not in expected:
            orphans.append(path)
    return orphans


def delete_orphans(
    conn: sqlite3.Connection, orphans: list[Path], limit: int | None = None
) -> CleanupStats:
    """Delete the given orphan files and remove any matching copy_log rows."""
    stats = CleanupStats(found=len(orphans))
    for path in orphans:
        if limit is not None and stats.deleted >= limit:
            break
        try:
            path.unlink()
        except OSError:
            stats.failed += 1
            continue
        conn.execute(
            "DELETE FROM copy_log WHERE dest_absolute_path = ?", (str(path),)
        )
        stats.deleted += 1
    conn.commit()
    return stats


@dataclass
class VerifyResult:
    dest_path: str
    expected_sha: str
    status: str  # 'ok', 'mismatch', 'missing'
    actual_sha: str | None = None


def verify_copies(
    conn: sqlite3.Connection,
    plan_run_id: str | None = None,
    sample: int | None = None,
) -> Iterator[VerifyResult]:
    """Re-SHA destination files and compare to the recorded dest_sha256.

    plan_run_id filters to a single plan; sample picks a random subset.
    """
    sql = """
        SELECT cl.dest_absolute_path, cl.dest_sha256
        FROM copy_log cl
        JOIN copy_plan cp ON cp.id = cl.plan_id
        WHERE cl.verified = 1
    """
    params: tuple = ()
    if plan_run_id is not None:
        sql += " AND cp.plan_run_id = ?"
        params = (plan_run_id,)
    if sample is not None:
        sql += f" ORDER BY RANDOM() LIMIT {int(sample)}"

    for r in conn.execute(sql, params):
        path = Path(r["dest_absolute_path"])
        if not path.exists():
            yield VerifyResult(
                dest_path=r["dest_absolute_path"],
                expected_sha=r["dest_sha256"],
                status="missing",
            )
            continue
        h = hashlib.sha256()
        try:
            with path.open("rb") as f:
                while chunk := f.read(1024 * 1024):
                    h.update(chunk)
        except OSError:
            yield VerifyResult(
                dest_path=r["dest_absolute_path"],
                expected_sha=r["dest_sha256"],
                status="missing",
            )
            continue
        actual = h.hexdigest()
        yield VerifyResult(
            dest_path=r["dest_absolute_path"],
            expected_sha=r["dest_sha256"],
            actual_sha=actual,
            status="ok" if actual == r["dest_sha256"] else "mismatch",
        )
