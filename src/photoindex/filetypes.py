from __future__ import annotations

from pathlib import Path

# Categories: jpeg, heic, png, tiff, bmp, gif, raw, video, other
_EXT_TO_CATEGORY: dict[str, str] = {
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".jpe": "jpeg",
    ".png": "png",
    ".heic": "heic",
    ".heif": "heic",
    ".tif": "tiff",
    ".tiff": "tiff",
    ".bmp": "bmp",
    ".gif": "gif",
    ".cr2": "raw",
    ".cr3": "raw",
    ".nef": "raw",
    ".nrw": "raw",
    ".arw": "raw",
    ".srf": "raw",
    ".sr2": "raw",
    ".rw2": "raw",
    ".raf": "raw",
    ".orf": "raw",
    ".dng": "raw",
    ".pef": "raw",
    ".raw": "raw",
    ".mp4": "video",
    ".m4v": "video",
    ".mov": "video",
    ".avi": "video",
    ".mkv": "video",
    ".mts": "video",
    ".m2ts": "video",
    ".wmv": "video",
    ".3gp": "video",
    ".mpg": "video",
    ".mpeg": "video",
}

PERCEPTUAL_HASH_CATEGORIES = {"jpeg", "heic", "png", "tiff", "bmp", "gif"}


def categorize(path: Path) -> str:
    return _EXT_TO_CATEGORY.get(path.suffix.lower(), "other")


def supports_perceptual_hash(category: str) -> bool:
    return category in PERCEPTUAL_HASH_CATEGORIES


def is_known(category: str) -> bool:
    return category != "other"
