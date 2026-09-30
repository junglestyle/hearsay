"""Rebuild the SQLite database from raw payloads: Omi's webhooks and our
recorder's uploads (hearsay/capture.py).

Every run is a full rebuild: raw is the only source of truth, and the database
is a disposable view of it. Nothing here writes to raw.

Only facts stated by the payloads are stored. Live transcript fragments are not
merged, and nothing is assigned to a conversation by inference: live transcript
and audio payloads carry no conversation id, only memory payloads do.
"""

import hashlib
import json
import os
import sqlite3
from pathlib import Path

from hearsay import capture

SCHEMA = """
CREATE TABLE payloads (
    path TEXT PRIMARY KEY,           -- body file, relative to the raw dir
    type TEXT NOT NULL,              -- transcript | audio | memory | capture
    received_at TEXT NOT NULL,
    uid TEXT,
    idempotency_key TEXT,
    body_bytes INTEGER NOT NULL,
    body_sha256 TEXT NOT NULL,
    -- An Omi retry: same idempotency key and body as this earlier payload.
    -- Duplicates are recorded but not parsed.
    duplicate_of TEXT REFERENCES payloads(path)
);

-- Segments exactly as each payload carried them. Transcript payloads carry
-- live fragments; memory payloads carry Omi's final segments, which reuse the
-- id of the first live fragment they absorbed.
CREATE TABLE segments (
    payload_path TEXT NOT NULL REFERENCES payloads(path),
    idx INTEGER NOT NULL,
    segment_id TEXT NOT NULL,
    start REAL NOT NULL,             -- seconds from conversation start
    end REAL NOT NULL,
    text TEXT NOT NULL,
    speaker TEXT,
    speaker_id INTEGER,
    speaker_id_scope TEXT,
    speaker_identity_status TEXT,
    speaker_match_source TEXT,
    speaker_name TEXT,               -- memory payloads only
    is_user INTEGER,
    person_id TEXT,
    speech_profile_processed INTEGER,
    stt_provider TEXT,
    PRIMARY KEY (payload_path, idx)
);
CREATE INDEX segments_segment_id ON segments(segment_id);

-- One row per memory payload. Omi's summary content (structured) is not
-- parsed: interpreting content is out of scope. It remains in raw.
CREATE TABLE memories (
    payload_path TEXT PRIMARY KEY REFERENCES payloads(path),
    conversation_id TEXT NOT NULL,
    created_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    status TEXT,
    discarded INTEGER,
    source TEXT,
    language TEXT,
    recording_session_id TEXT
);
CREATE INDEX memories_conversation_id ON memories(conversation_id);

-- Omi's own recording of the conversation. Its started_at matches the
-- memory's created_at, not its started_at, and duration doesn't match the
-- conversation span. Segment start/end count from memories.started_at.
CREATE TABLE memory_audio_files (
    payload_path TEXT NOT NULL REFERENCES payloads(path),
    idx INTEGER NOT NULL,
    audio_file_id TEXT,
    started_at TEXT NOT NULL,
    duration REAL NOT NULL,
    PRIMARY KEY (payload_path, idx)
);

-- Raw PCM16LE mono. The bytes stay in raw; received_at is the only timing.
CREATE TABLE audio_chunks (
    payload_path TEXT PRIMARY KEY REFERENCES payloads(path),
    sample_rate INTEGER NOT NULL
);

-- Presses of the pendant's button, from capture uploads. Audio from those
-- uploads is decoded later (hearsay/capture.py); its bytes stay in raw.
CREATE TABLE button_events (
    payload_path TEXT NOT NULL REFERENCES payloads(path),
    at REAL NOT NULL,                -- unix seconds, on the recorder's clock
    event INTEGER NOT NULL           -- 1 tap, 2 double tap, 5 release after a hold
);
"""


class ParseFailed(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(f"{len(errors)} payload(s) failed to parse:\n" + "\n".join(errors))


def insert_segment(db: sqlite3.Connection, payload_path: str, idx: int, s: dict) -> None:
    db.execute(
        "INSERT INTO segments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            payload_path,
            idx,
            s["id"],
            float(s["start"]),
            float(s["end"]),
            s["text"],
            s.get("speaker"),
            s.get("speaker_id"),
            s.get("speaker_id_scope"),
            s.get("speaker_identity_status"),
            s.get("speaker_match_source"),
            s.get("speaker_name"),
            s.get("is_user"),
            s.get("person_id"),
            s.get("speech_profile_processed"),
            s.get("stt_provider"),
        ),
    )


def parse_transcript(db: sqlite3.Connection, path: str, body: bytes) -> None:
    # Captures are an object, not the bare array Omi's docs describe.
    # session_id in the body is the user id, so it isn't stored.
    segments = json.loads(body)["segments"]
    if not isinstance(segments, list):
        raise ValueError("segments is not a list")
    for idx, s in enumerate(segments):
        insert_segment(db, path, idx, s)


def parse_memory(db: sqlite3.Connection, path: str, body: bytes) -> None:
    m = json.loads(body)
    db.execute(
        "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            path,
            m["id"],
            m.get("created_at"),
            m.get("started_at"),
            m.get("finished_at"),
            m.get("status"),
            m.get("discarded"),
            m.get("source"),
            m.get("language"),
            (m.get("external_data") or {}).get("recording_session_id"),
        ),
    )
    for idx, s in enumerate(m["transcript_segments"]):
        insert_segment(db, path, idx, s)
    for idx, a in enumerate(m.get("audio_files") or []):
        db.execute(
            "INSERT INTO memory_audio_files VALUES (?,?,?,?,?)",
            (path, idx, a.get("id"), a["started_at"], float(a["duration"])),
        )


def parse_audio(db: sqlite3.Connection, path: str, body: bytes, query: dict) -> None:
    if len(body) % 2:
        raise ValueError(f"odd byte count {len(body)} for PCM16")
    db.execute("INSERT INTO audio_chunks VALUES (?,?)", (path, int(query["sample_rate"])))


def parse_capture(db: sqlite3.Connection, path: str, body: bytes) -> None:
    for at, kind, data in capture.read_records(body):
        if kind == capture.BUTTON:
            db.execute("INSERT INTO button_events VALUES (?,?,?)", (path, at, capture.button_event(data)))


def load_payloads(db: sqlite3.Connection, raw_dir: Path) -> tuple[dict, list[str]]:
    counts = {"transcript": 0, "audio": 0, "memory": 0, "capture": 0, "duplicate": 0}
    errors = []
    sidecars = []
    for sidecar_path in raw_dir.glob("*/*/*.json"):
        sidecars.append((json.loads(sidecar_path.read_bytes()), sidecar_path))
    # Earliest delivery wins, so duplicate_of is stable across runs.
    sidecars.sort(key=lambda item: (item[0]["received_at"], str(item[1])))

    first_delivery = {}
    for sc, sidecar_path in sidecars:
        body_path = sidecar_path.parent / sc["body_file"]
        path = str(body_path.relative_to(raw_dir))
        try:
            body = body_path.read_bytes()
            sha = hashlib.sha256(body).hexdigest()
            if sha != sc["body_sha256"]:
                raise ValueError("body sha256 does not match sidecar")
            query = dict(sc["query"])
            key = dict(sc["headers"]).get("idempotency-key")
            webhook_type = sc["webhook_type"]

            duplicate_of = first_delivery.get((webhook_type, key, sha)) if key else None
            db.execute(
                "INSERT INTO payloads VALUES (?,?,?,?,?,?,?,?)",
                (path, webhook_type, sc["received_at"], query.get("uid"), key, len(body), sha, duplicate_of),
            )
            if duplicate_of:
                counts["duplicate"] += 1
                continue
            if key:
                first_delivery[(webhook_type, key, sha)] = path

            if webhook_type == "transcript":
                parse_transcript(db, path, body)
            elif webhook_type == "memory":
                parse_memory(db, path, body)
            elif webhook_type == "audio":
                parse_audio(db, path, body, query)
            elif webhook_type == "capture":
                parse_capture(db, path, body)
            else:
                raise ValueError(f"unknown webhook type {webhook_type!r}")
            counts[webhook_type] += 1
        except (OSError, KeyError, TypeError, ValueError) as e:
            errors.append(f"{path}: {e!r}")
    return counts, errors


def rebuild(raw_dir: Path, db_path: Path) -> dict:
    """Rebuild db_path from raw_dir. Replaces db_path only if every payload parses."""
    # The receiver writes the body first, then the sidecar. A body without a
    # sidecar is an incomplete write (or one still in flight) and is skipped.
    incomplete = sum(1 for b in raw_dir.glob("*/*/*.body") if not b.with_suffix(".json").exists())

    tmp_path = db_path.with_name(db_path.name + ".tmp")
    tmp_path.unlink(missing_ok=True)
    db = sqlite3.connect(tmp_path)
    try:
        db.executescript(SCHEMA)
        counts, errors = load_payloads(db, raw_dir)
        db.commit()
    finally:
        db.close()

    if errors:
        tmp_path.unlink()
        raise ParseFailed(errors)
    os.replace(tmp_path, db_path)
    counts["incomplete"] = incomplete
    return counts
