from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import ExifTags, Image

_DATETIME_TAGS = ("DateTimeOriginal", "DateTimeDigitized", "DateTime")


@dataclass
class ExifData:
    datetime_iso: str | None = None
    camera_make: str | None = None
    camera_model: str | None = None
    gps_lat: float | None = None
    gps_lon: float | None = None


def extract(path: Path) -> ExifData:
    try:
        with Image.open(path) as img:
            exif = img.getexif()
    except Exception:
        return ExifData()

    if not exif:
        return ExifData()

    tag_lookup = {ExifTags.TAGS.get(t, t): v for t, v in exif.items()}

    return ExifData(
        datetime_iso=_first_datetime(tag_lookup),
        camera_make=_clean(tag_lookup.get("Make")),
        camera_model=_clean(tag_lookup.get("Model")),
        **_gps(exif),
    )


def _clean(value: object) -> str | None:
    if value is None:
        return None
    s = str(value).strip().strip("\x00")
    return s or None


def _first_datetime(tags: dict) -> str | None:
    for key in _DATETIME_TAGS:
        raw = tags.get(key)
        if not raw:
            continue
        try:
            dt = datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S")
            return dt.isoformat()
        except ValueError:
            continue
    return None


def _gps(exif) -> dict:
    gps_ifd = exif.get_ifd(ExifTags.IFD.GPSInfo) if hasattr(ExifTags, "IFD") else None
    if not gps_ifd:
        return {"gps_lat": None, "gps_lon": None}

    def _ratio(value) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _to_degrees(dms) -> float | None:
        try:
            d, m, s = (_ratio(x) for x in dms)
            return d + m / 60 + s / 3600
        except Exception:
            return None

    lat = _to_degrees(gps_ifd.get(ExifTags.GPS.GPSLatitude)) if hasattr(ExifTags, "GPS") else None
    lon = _to_degrees(gps_ifd.get(ExifTags.GPS.GPSLongitude)) if hasattr(ExifTags, "GPS") else None
    if hasattr(ExifTags, "GPS"):
        if lat is not None and gps_ifd.get(ExifTags.GPS.GPSLatitudeRef) == "S":
            lat = -lat
        if lon is not None and gps_ifd.get(ExifTags.GPS.GPSLongitudeRef) == "W":
            lon = -lon
    return {"gps_lat": lat, "gps_lon": lon}
