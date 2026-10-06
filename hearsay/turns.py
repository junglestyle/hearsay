"""Speaker turns from our own transcripts, and the operator's input resolved onto them.

Transcripts are Parakeet and pyannote output made on the dev box (hearsay/transcribe.py)
from a conversation's WAV. A transcript is used only if it was made from the
exact WAV reprocess just assembled (same sha256); otherwise the conversation
is pending until the dev box redoes it. Turn times are offsets into that WAV,
so they line up with the audio by construction.

The operator's labels and names are durable input that must survive
re-transcription and changing conversation boundaries, so they are matched to
turns by absolute time, not by id. The one exception is a rename, which names
a person rather than turns. New records store absolute spans ("at"); older
ones point into Omi's timeline and are placed via omi_timeline. A turn
inherits a record when the record's span covers at least half of the turn.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

# A pause this long starts a new turn even when the speaker doesn't change.
MAX_GAP = 2.0
# Fraction of a turn a labeled span must cover for the turn to inherit it.
MIN_COVER = 0.5
# Correction to the live-arrival estimate of Omi's segment time 0, measured on
# five real conversations (+/-1.5 s). Only places records made before our
# own conversation boundaries (see omi_timeline).
LIVE_ARRIVAL_OFFSET = -0.5

SCHEMA = """
DROP TABLE IF EXISTS turns;
DROP TABLE IF EXISTS operator_input;
-- Consecutive words from one diarized speaker, from our own transcript.
CREATE TABLE turns (
    turn_id TEXT PRIMARY KEY,        -- conversation_id:idx; changes if the transcript changes
    conversation_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    start REAL NOT NULL,             -- seconds into the conversation WAV
    end REAL NOT NULL,
    text TEXT NOT NULL,
    diar_speaker TEXT,               -- diarized speaker within this conversation only
    confidence REAL                  -- mean word confidence, NULL if no word has one
);
-- The operator's labels and tags as they apply to the current turns.
CREATE TABLE operator_input (
    turn_id TEXT NOT NULL,
    kind TEXT NOT NULL,              -- label | name | clip | mixed | skip
    value TEXT                       -- owner / not_owner / unsure for label, the name for name and clip
);
"""


class OperatorInput(NamedTuple):
    labels: dict[str, str]           # turn_id -> owner | not_owner | unsure
    names: dict[str, str]            # turn_id -> name
    mixed: set[str]
    skipped: set[str]
    clips: dict[str, str]            # turn_id -> name, for a clip tagged on its own


def split_turns(segments: list[dict]) -> list[dict]:
    """Group aligned words into turns: a new turn at each speaker change or long pause."""
    turns, pending = [], []  # pending: words without timestamps, before any timed word
    for segment in segments:
        words = segment.get("words") or []
        if not words:
            # Alignment failed for the whole segment: keep it as one turn.
            turns.append({"start": segment["start"], "end": segment["end"], "words": [segment["text"].strip()],
                          "speaker": segment.get("speaker"), "scores": []})
            continue
        for w in words:
            if "start" not in w or "end" not in w:
                # Numbers and symbols often get no timestamp; they belong to the current turn.
                (turns[-1]["words"] if turns else pending).append(w["word"])
                continue
            speaker = w.get("speaker", segment.get("speaker"))
            current = turns[-1] if turns else None
            if current is None or speaker != current["speaker"] or w["start"] - current["end"] > MAX_GAP:
                turns.append({"start": w["start"], "end": w["end"], "words": pending + [w["word"]],
                              "speaker": speaker, "scores": []})
                pending = []
            else:
                current["end"] = w["end"]
                current["words"].append(w["word"])
            if "score" in w:
                turns[-1]["scores"].append(w["score"])
    return turns


def build_turns(db_path: Path, transcripts_dir: Path) -> dict:
    db = sqlite3.connect(db_path)
    counts = {"conversations": 0, "transcribed": 0, "pending": 0, "turns": 0}
    try:
        db.executescript(SCHEMA)
        for conversation_id, wav_sha256 in db.execute(
            "SELECT conversation_id, wav_sha256 FROM conversation_audio WHERE wav_file IS NOT NULL ORDER BY conversation_id"
        ).fetchall():
            counts["conversations"] += 1
            path = transcripts_dir / f"{conversation_id}.json"
            transcript = json.loads(path.read_text()) if path.exists() else None
            if transcript is None or transcript["wav_sha256"] != wav_sha256:
                counts["pending"] += 1
                continue
            counts["transcribed"] += 1
            for idx, turn in enumerate(split_turns(transcript["segments"])):
                confidence = sum(turn["scores"]) / len(turn["scores"]) if turn["scores"] else None
                db.execute(
                    "INSERT INTO turns VALUES (?,?,?,?,?,?,?,?)",
                    (f"{conversation_id}:{idx:04d}", conversation_id, idx, turn["start"], turn["end"],
                     " ".join(turn["words"]), turn["speaker"], confidence),
                )
                counts["turns"] += 1
        db.commit()
    finally:
        db.close()
    return counts


def wall(zero_at: str, start: float, end: float) -> list[str]:
    """A span in a conversation WAV as absolute UTC times, how new records store it."""
    zero = datetime.fromisoformat(zero_at).timestamp()
    return [datetime.fromtimestamp(zero + t, timezone.utc).isoformat() for t in (start, end)]


def omi_timeline(db: sqlite3.Connection) -> tuple[dict[str, float], dict[str, tuple[str, float, float]]]:
    """Where older records point: Omi conversations' zero points (unix seconds),
    and each Omi segment's (conversation, start, end) relative to that zero.

    Records made before our own conversation boundaries name Omi segments, or
    spans in WAVs that started at Omi's segment time 0. That zero was estimated
    from live transcript arrival (arrival - segment end, minimised over the
    conversation's segments, shifted by LIVE_ARRIVAL_OFFSET); it is recomputed
    here from raw, so those records keep landing where they were made.
    """
    latest = {}  # Omi conversation -> its latest memory payload
    for conversation_id, path, started_at in db.execute(
        "SELECT m.conversation_id, m.payload_path, m.started_at FROM memories m"
        " JOIN payloads p ON p.path = m.payload_path ORDER BY p.received_at, p.path"
    ):
        latest[conversation_id] = (path, started_at)
    zeros, segments = {}, {}
    for conversation_id, (path, started_at) in latest.items():
        live = db.execute(
            "SELECT lp.received_at, ls.end FROM segments ms"
            " JOIN segments ls ON ls.segment_id = ms.segment_id"
            " JOIN payloads lp ON lp.path = ls.payload_path"
            " WHERE ms.payload_path = ? AND lp.type = 'transcript'",
            (path,),
        ).fetchall()
        if live:
            zeros[conversation_id] = min(
                datetime.fromisoformat(received_at).timestamp() - end for received_at, end in live
            ) + LIVE_ARRIVAL_OFFSET
        else:
            zeros[conversation_id] = datetime.fromisoformat(started_at).timestamp()
        for segment_id, start, end in db.execute(
            "SELECT segment_id, start, end FROM segments WHERE payload_path = ?", (path,)
        ):
            segments[segment_id] = (conversation_id, start, end)
    return zeros, segments


def resolve(db: sqlite3.Connection, labels_text: str, tags_text: str) -> OperatorInput:
    """The operator's labels and tags, matched to the current turns by absolute time."""
    zeros, segments = omi_timeline(db)
    turns = [
        (turn_id, datetime.fromisoformat(zero_at).timestamp() + start, datetime.fromisoformat(zero_at).timestamp() + end)
        for turn_id, start, end, zero_at in db.execute(
            "SELECT t.turn_id, t.start, t.end, ca.zero_at FROM turns t"
            " JOIN conversation_audio ca ON ca.conversation_id = t.conversation_id"
        )
    ]

    def spans(record: dict) -> list[tuple[float, float]]:
        if "at" in record:
            return [tuple(datetime.fromisoformat(t).timestamp() for t in span) for span in record["at"]]
        if "turns" in record:  # relative to an Omi conversation's zero point
            return [(zeros[t["conversation_id"]] + t["start"], zeros[t["conversation_id"]] + t["end"])
                    for t in record["turns"] if t["conversation_id"] in zeros]
        ids = record.get("segment_ids") or ([record["segment_id"]] if "segment_id" in record else [])
        return [(zeros[segments[i][0]] + segments[i][1], zeros[segments[i][0]] + segments[i][2])
                for i in ids if i in segments]

    def covered(record: dict) -> list[str]:
        hits = []
        for start, end in spans(record):
            for turn_id, t_start, t_end in turns:
                overlap = min(end, t_end) - max(start, t_start)
                if t_end > t_start and overlap >= MIN_COVER * (t_end - t_start):
                    hits.append(turn_id)
        return hits

    result = OperatorInput({}, {}, set(), set(), {})
    for line in labels_text.splitlines():
        record = json.loads(line)
        for turn_id in covered(record):
            result.labels[turn_id] = record["label"]
    for line in tags_text.splitlines():
        record = json.loads(line)
        if record["type"] == "rename":
            # A person, not turns: every turn named "from" so far becomes "to",
            # in any cluster. Renaming onto an existing name merges the two.
            for tagged in (result.names, result.clips):
                for turn_id, name in list(tagged.items()):
                    if name == record["from"]:
                        tagged[turn_id] = record["to"]
            continue
        for turn_id in covered(record):
            if record["type"] == "name" and record["name"]:
                result.names[turn_id] = record["name"]
            elif record["type"] == "name":
                result.names.pop(turn_id, None)  # the operator took a name back
            elif record["type"] == "clip" and record["name"]:
                result.clips[turn_id] = record["name"]
            elif record["type"] == "clip":
                result.clips.pop(turn_id, None)
            elif record["type"] == "mixed":
                result.mixed.add(turn_id)
            elif record["type"] == "skip":
                result.skipped.add(turn_id)
    return result


def read_operator_files(labels_dir: Path) -> tuple[str, str]:
    def text(name: str) -> str:
        path = labels_dir / name
        return path.read_text() if path.exists() else ""

    return text("labels.jsonl"), text("tags.jsonl")


def record_operator_input(db_path: Path, labels_dir: Path) -> dict:
    """Materialize the resolved input, for reprocess steps and the report."""
    db = sqlite3.connect(db_path)
    try:
        found = resolve(db, *read_operator_files(labels_dir))
        rows = [(t, "label", v) for t, v in found.labels.items()]
        rows += [(t, "name", v) for t, v in found.names.items()]
        rows += [(t, "clip", v) for t, v in found.clips.items()]
        rows += [(t, "mixed", None) for t in found.mixed] + [(t, "skip", None) for t in found.skipped]
        db.executemany("INSERT INTO operator_input VALUES (?,?,?)", sorted(rows, key=lambda r: (r[0], r[1])))
        db.commit()
    finally:
        db.close()
    return {"labeled_turns": len(found.labels), "named_turns": len(found.names)}
