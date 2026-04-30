from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import imagehash
from PIL import Image, UnidentifiedImageError

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:
    pass

# These photos are trusted user content; lift Pillow's anti-DoS pixel cap so legit large scans
# (e.g. a 21000x21000 negative scan) don't trigger DecompressionBombError.
Image.MAX_IMAGE_PIXELS = None

_SHA_CHUNK = 1024 * 1024


@dataclass
class PerceptualHashes:
    phash: str | None = None
    dhash: str | None = None
    whash: str | None = None
    width: int | None = None
    height: int | None = None
    error: str | None = None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_SHA_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def perceptual_hashes(path: Path) -> PerceptualHashes:
    try:
        with Image.open(path) as img:
            img.load()
            width, height = img.size
            # imagehash handles mode conversion internally for most cases.
            return PerceptualHashes(
                phash=str(imagehash.phash(img)),
                dhash=str(imagehash.dhash(img)),
                whash=str(imagehash.whash(img)),
                width=width,
                height=height,
            )
    except Exception as e:  # noqa: BLE001 - any decode failure -> soft skip
        return PerceptualHashes(error=f"{type(e).__name__}: {e}")
