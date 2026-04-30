from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from . import exif, filetypes, hashing
from .db import now_iso


@dataclass
class ScanStats:
    seen: int = 0
    inserted: int = 0
    updated: int = 0
    skipped_unknown: int = 0
    hash_errors: int = 0


def iter_files(root: Path) -> Iterator[Path]:
    for p in root.rglob("*"):
        if p.is_file() and not p.name.startswith("."):
            yield p


def scan(
    conn: sqlite3.Connection,
    disk_id: int,
    root: Path,
    progress: Callable[[Path, ScanStats], None] | None = None,
    disk_prefix: str = "",
) -> ScanStats:
    """Scan files under `root` and index them.

    `disk_prefix` is prepended to each file's relative path before storage,
    so a partial scan of a disk still records disk-rooted paths (e.g. when
    scanning `/Volumes/D1/Pictures` with disk_prefix="Pictures").
    """
    prefix = disk_prefix.strip("/").replace("\\", "/")
    stats = ScanStats()
    commit_every = 100
    for path in iter_files(root):
        stats.seen += 1
        try:
            category = filetypes.categorize(path)
            if not filetypes.is_known(category):
                stats.skipped_unknown += 1
                if progress:
                    progress(path, stats)
                continue

            rel = str(path.relative_to(root))
            relative_path = f"{prefix}/{rel}" if prefix else rel
            st = path.stat()
            size = st.st_size
            mtime_iso = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(
                timespec="seconds"
            )
            sha = hashing.sha256_file(path)

            phashes = hashing.PerceptualHashes()
            if filetypes.supports_perceptual_hash(category):
                phashes = hashing.perceptual_hashes(path)
                if phashes.error:
                    stats.hash_errors += 1

            exif_data = (
                exif.extract(path)
                if category in filetypes.PERCEPTUAL_HASH_CATEGORIES
                else exif.ExifData()
            )

            action = _upsert_photo(
                conn,
                disk_id=disk_id,
                relative_path=relative_path,
                filename=path.name,
                file_size=size,
                file_type=category,
                sha256=sha,
                phashes=phashes,
                exif_data=exif_data,
                file_mtime=mtime_iso,
            )
            if action == "inserted":
                stats.inserted += 1
            elif action == "updated":
                stats.updated += 1
        except Exception as e:  # noqa: BLE001 - one bad file shouldn't kill a 22k-file scan
            stats.hash_errors += 1
            print(f"\nERROR processing {path}: {type(e).__name__}: {e}", flush=True)

        if stats.seen % commit_every == 0:
            conn.commit()
        if progress:
            progress(path, stats)

    conn.commit()
    return stats


def _upsert_photo(
    conn: sqlite3.Connection,
    *,
    disk_id: int,
    relative_path: str,
    filename: str,
    file_size: int,
    file_type: str,
    sha256: str,
    phashes: hashing.PerceptualHashes,
    exif_data: exif.ExifData,
    file_mtime: str,
) -> str:
    now = now_iso()
    existing = conn.execute(
        "SELECT id FROM photos WHERE disk_id = ? AND relative_path = ?",
        (disk_id, relative_path),
    ).fetchone()

    if existing:
        conn.execute(
            """
            UPDATE photos SET
                filename = ?, file_size = ?, file_type = ?, sha256 = ?,
                phash = ?, dhash = ?, whash = ?, width = ?, height = ?,
                exif_datetime = ?, exif_camera_make = ?, exif_camera_model = ?,
                exif_gps_lat = ?, exif_gps_lon = ?, file_mtime = ?, last_seen = ?
            WHERE id = ?
            """,
            (
                filename, file_size, file_type, sha256,
                phashes.phash, phashes.dhash, phashes.whash, phashes.width, phashes.height,
                exif_data.datetime_iso, exif_data.camera_make, exif_data.camera_model,
                exif_data.gps_lat, exif_data.gps_lon, file_mtime, now,
                existing["id"],
            ),
        )
        return "updated"

    conn.execute(
        """
        INSERT INTO photos (
            disk_id, relative_path, filename, file_size, file_type, sha256,
            phash, dhash, whash, width, height,
            exif_datetime, exif_camera_make, exif_camera_model,
            exif_gps_lat, exif_gps_lon, file_mtime, first_seen, last_seen
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            disk_id, relative_path, filename, file_size, file_type, sha256,
            phashes.phash, phashes.dhash, phashes.whash, phashes.width, phashes.height,
            exif_data.datetime_iso, exif_data.camera_make, exif_data.camera_model,
            exif_data.gps_lat, exif_data.gps_lon, file_mtime, now, now,
        ),
    )
    return "inserted"
