"""Find conversations in the continuous audio stream, from speech alone.

Runs voice activity detection (Silero) over every continuous run of audio,
then groups the speech: a conversation ends after GAP seconds without speech,
or at an end mark the owner made with the pendant's button. One with less
than MIN_SPEECH seconds of speech is dropped, unless there's a start mark
within TAP_WINDOW of it (a self-note, marked on purpose). Content is
never consulted, so conversations can run for hours; splitting by topic is a
downstream concern. Dropped speech stays in raw and returns if the rules change.

Measured on 8.7 h of real audio (2026-09-28): Omi's conversations held 99%
of detected speech, and a 3 min gap never split one of them (1-2 min did),
though it merges Omi's back-to-back conversations into one.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from hearsay.assemble import SAMPLE_RATE, place_bursts
from hearsay.cache import Cache, audio_key

# A conversation ends after this long without speech.
GAP = 180.0
# Conversations with less speech than this are dropped (stray remarks,
# passing chatter).
MIN_SPEECH = 30.0
# A start mark this close to a stretch of speech keeps it, however short.
TAP_WINDOW = 30.0
# Audio kept around the first and last speech, so words aren't clipped.
PAD = 1.0
# Bursts closer than this are one continuous run of audio.
CONTIGUOUS = 0.05
# Cache key prefix: the detector and settings that produced a cached result.
VAD_VERSION = "silero-vad-6.2.3:16k:min_silence_500"

SCHEMA = """
DROP TABLE IF EXISTS conversations;
CREATE TABLE conversations (
    conversation_id TEXT PRIMARY KEY, -- c + UTC time of its first speech; stable as it grows
    start REAL NOT NULL,              -- unix seconds, PAD before the first speech
    end REAL NOT NULL,                -- unix seconds, PAD after the last speech
    speech_seconds REAL NOT NULL,
    open INTEGER NOT NULL             -- 1 if the audio ends less than GAP after its last speech
);
DROP TABLE IF EXISTS conversation_taps;
-- Start marks within TAP_WINDOW of a conversation.
CREATE TABLE conversation_taps (
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    at REAL NOT NULL                  -- unix seconds
);
"""


def near(tap: float, start: float, end: float) -> bool:
    return start - TAP_WINDOW <= tap <= end + TAP_WINDOW


def group_speech(speech: list[tuple[float, float]], taps: list[float] = (),
                 ends: list[float] = ()) -> list[tuple[float, float, float]]:
    """(start, end, seconds of speech) per conversation, from speech intervals
    and the times of start marks (taps) and end marks. Speech that starts
    after an end mark begins a new conversation, however soon it follows."""
    groups = []
    last_start = None
    for start, end in sorted(speech):
        split = any(last_start < mark <= start for mark in ends) if last_start is not None else False
        last_start = start
        if groups and start - groups[-1][1] <= GAP and not split:
            groups[-1][1] = max(groups[-1][1], end)
            groups[-1][2] += end - start
        else:
            groups.append([start, end, end - start])
    return [(s, e, n) for s, e, n in groups if n >= MIN_SPEECH or any(near(t, s, e) for t in taps)]


def detect_speech(raw_dir: Path, bursts: list, cache: Cache | None = None) -> list[tuple[float, float]]:
    """Speech intervals in wall-clock seconds. With a cache, a run of audio seen
    before (same bytes) is not analyzed again; only new audio costs time."""
    # Imported here: torch is heavy, and only the worker image has it.
    import numpy as np
    import torch
    from silero_vad import get_speech_timestamps, load_silero_vad

    runs = []
    for start, end, paths in bursts:
        if runs and abs(start - runs[-1][1]) < CONTIGUOUS:
            runs[-1][1] = end
            runs[-1][2].extend(paths)
        else:
            runs.append([start, end, list(paths)])

    model = None
    speech = []
    for start, _, paths in runs:
        pcm = b"".join((raw_dir / p).read_bytes() for p in paths)
        key = audio_key(VAD_VERSION, pcm)
        cached = cache.get(key) if cache else None
        if cached is not None:
            found = json.loads(cached)
        else:
            model = model or load_silero_vad()
            samples = torch.from_numpy(np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768)
            found = [(s["start"], s["end"]) for s in get_speech_timestamps(
                samples, model, sampling_rate=SAMPLE_RATE, min_silence_duration_ms=500, return_seconds=True)]
            if cache:
                cache.put(key, json.dumps(found).encode())
        speech += [(start + s, start + e) for s, e in found]
    return speech


def conversation_id(start: float) -> str:
    return "c" + datetime.fromtimestamp(start, timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def find_conversations(raw_dir: Path, db_path: Path, cache: Cache | None = None) -> dict:
    db = sqlite3.connect(db_path)
    try:
        bursts = place_bursts(db)
        speech = detect_speech(raw_dir, bursts, cache)
        taps = [at for (at,) in db.execute("SELECT at FROM marks WHERE kind = 'start'")]
        ends = [at for (at,) in db.execute("SELECT at FROM marks WHERE kind = 'end'")]
        stream_end = max((end for _, end, _ in bursts), default=0.0)
        groups = group_speech(speech, taps, ends)
        rows = [
            (conversation_id(start), start - PAD, end + PAD, seconds, int(stream_end - end < GAP))
            for start, end, seconds in groups
        ]
        tap_rows = [(conversation_id(start), t) for start, end, _ in groups for t in sorted(taps)
                    if near(t, start, end)]
        db.executescript(SCHEMA)
        db.executemany("INSERT INTO conversations VALUES (?,?,?,?,?)", rows)
        db.executemany("INSERT INTO conversation_taps VALUES (?,?)", tap_rows)
        db.commit()
    finally:
        db.close()
    return {
        "speech_minutes": round(sum(e - s for s, e in speech) / 60, 1),
        "conversations": len(rows),
        "open": sum(r[4] for r in rows),
        "with_taps": len({r[0] for r in tap_rows}),
        "audio_hours": round(sum(end - start for start, end, _ in bursts) / 3600, 1),
    }
