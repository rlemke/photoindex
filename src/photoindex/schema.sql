PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS disks (
    id           INTEGER PRIMARY KEY,
    label        TEXT NOT NULL UNIQUE,
    volume_uuid  TEXT,
    notes        TEXT,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS photos (
    id                 INTEGER PRIMARY KEY,
    disk_id            INTEGER NOT NULL REFERENCES disks(id),
    relative_path      TEXT NOT NULL,
    filename           TEXT NOT NULL,
    file_size          INTEGER NOT NULL,
    file_type          TEXT NOT NULL,
    sha256             TEXT NOT NULL,
    phash              TEXT,
    dhash              TEXT,
    whash              TEXT,
    width              INTEGER,
    height             INTEGER,
    exif_datetime      TEXT,
    exif_camera_make   TEXT,
    exif_camera_model  TEXT,
    exif_gps_lat       REAL,
    exif_gps_lon       REAL,
    file_mtime         TEXT NOT NULL,
    first_seen         TEXT NOT NULL,
    last_seen          TEXT NOT NULL,
    UNIQUE(disk_id, relative_path)
);

CREATE INDEX IF NOT EXISTS idx_photos_sha256 ON photos(sha256);
CREATE INDEX IF NOT EXISTS idx_photos_filename ON photos(filename);
CREATE INDEX IF NOT EXISTS idx_photos_phash_prefix ON photos(substr(phash, 1, 4));
CREATE INDEX IF NOT EXISTS idx_photos_file_type ON photos(file_type);

-- Pairs of photos judged similar by perceptual hashing. photo_a_id < photo_b_id by convention.
CREATE TABLE IF NOT EXISTS similar_pairs (
    photo_a_id      INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    photo_b_id      INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    phash_distance  INTEGER,
    dhash_distance  INTEGER,
    whash_distance  INTEGER,
    PRIMARY KEY (photo_a_id, photo_b_id),
    CHECK (photo_a_id < photo_b_id)
);

-- Non-dedup linkages, e.g. RAW+JPEG sidecar pairs that should travel together.
CREATE TABLE IF NOT EXISTS pair_links (
    photo_a_id  INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    photo_b_id  INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    link_type   TEXT NOT NULL,
    PRIMARY KEY (photo_a_id, photo_b_id, link_type),
    CHECK (photo_a_id < photo_b_id)
);

-- User decisions about specific photo pairs that override automated dedup.
-- status: 'confirmed_dup' (treat as same regardless of phash distance)
--      |  'confirmed_distinct' (always exclude from grouping even if close)
CREATE TABLE IF NOT EXISTS manual_overrides (
    photo_a_id   INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    photo_b_id   INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    status       TEXT NOT NULL CHECK (status IN ('confirmed_dup', 'confirmed_distinct')),
    reason       TEXT,
    decided_at   TEXT NOT NULL,
    PRIMARY KEY (photo_a_id, photo_b_id),
    CHECK (photo_a_id < photo_b_id)
);

-- One row per "plan run": parameters of how a copy plan was built.
CREATE TABLE IF NOT EXISTS plan_runs (
    plan_run_id   TEXT PRIMARY KEY,
    dest_root     TEXT NOT NULL,
    layout        TEXT NOT NULL,
    max_distance  INTEGER NOT NULL,
    built_at      TEXT NOT NULL,
    notes         TEXT
);

-- A planned copy operation (one row per chosen-canonical photo per plan run).
CREATE TABLE IF NOT EXISTS copy_plan (
    id                 INTEGER PRIMARY KEY,
    plan_run_id        TEXT NOT NULL,
    photo_id           INTEGER NOT NULL REFERENCES photos(id),
    dest_relative_path TEXT NOT NULL,
    rename_reason      TEXT,
    created_at         TEXT NOT NULL,
    UNIQUE(plan_run_id, dest_relative_path)
);

CREATE INDEX IF NOT EXISTS idx_copy_plan_run ON copy_plan(plan_run_id);

-- A record of an executed copy. Keeps source disk + path even if the source disk later goes offline.
CREATE TABLE IF NOT EXISTS copy_log (
    id                    INTEGER PRIMARY KEY,
    plan_id               INTEGER NOT NULL REFERENCES copy_plan(id),
    source_disk_label     TEXT NOT NULL,
    source_relative_path  TEXT NOT NULL,
    dest_absolute_path    TEXT NOT NULL,
    dest_sha256           TEXT NOT NULL,
    copied_at             TEXT NOT NULL,
    verified              INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO schema_version (version) VALUES (1);
