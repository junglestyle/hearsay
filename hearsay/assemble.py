"""Cut per-conversation WAV files out of the continuous audio stream.

Reads the database built by parse.rebuild and the raw audio bodies. Writes one
WAV per conversation plus the conversation_audio table. Everything here is
derived from raw and is rebuilt on every run.

Timing rules below were measured on real captures:
- Omi posts audio in bursts: 5-6 s of audio as 1 s chunks ~0.09 s apart.
  Consecutive bursts are nearly always contiguous audio, and receipt time
  jitters by about +/-0.5 s, so bursts are laid end to end and only
  re-anchored to receipt time when the two disagree, which means audio was lost.
- Segment start/end count from Omi's live transcription session, which is not
  always the memory's started_at (off by up to 31 min). The session start is
  estimated from live transcript arrival: arrival - segment end, minimised
  over the conversation's segments, then shifted by a measured offset.
"""

import sqlite3
import wave
from datetime import datetime, timezone
from pathlib import Path

SAMPLE_RATE = 16000
BYTES_PER_SECOND = SAMPLE_RATE * 2  # PCM16LE mono

# Chunks closer together than this belong to one burst.
BURST_GAP = 1.5
# When contiguous placement and receipt-time placement differ by more than
# this, audio was lost (or duplicated); trust receipt time.
REANCHOR = 2.0
# Correction to the live-arrival estimate of segment time 0. Measured by
# matching loudness in assembled WAVs to segments on five real conversations
# with a clear match; they scatter about +/-1.5 s around it.
LIVE_ARRIVAL_OFFSET = -0.5

SCHEMA = """
DROP TABLE IF EXISTS conversation_audio;
-- One row per conversation with a memory payload (the latest one).
CREATE TABLE conversation_audio (
    conversation_id TEXT PRIMARY KEY,
    memory_path TEXT NOT NULL REFERENCES payloads(path),
    zero_at TEXT NOT NULL,           -- wall time (UTC) of segment time 0
    zero_source TEXT NOT NULL,       -- live (transcript arrival) | started_at
    duration REAL NOT NULL,          -- seconds, up to the last segment's end
    coverage REAL NOT NULL,          -- fraction of duration with received audio
    wav_file TEXT                    -- relative to the audio dir; NULL if no audio
);
"""


def timestamp(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def place_bursts(db: sqlite3.Connection) -> list[tuple[float, float, list[str]]]:
    """Return (start, end, body paths) per burst, in wall-clock seconds."""
    rows = db.execute(
        "SELECT p.path, p.received_at, p.body_bytes, a.sample_rate FROM audio_chunks a"
        " JOIN payloads p ON p.path = a.payload_path ORDER BY p.received_at, p.path"
    ).fetchall()
    bursts = []  # [first receipt, last receipt, bytes, paths]
    for path, received_at, body_bytes, sample_rate in rows:
        if sample_rate != SAMPLE_RATE:
            raise ValueError(f"{path}: sample rate {sample_rate}, expected {SAMPLE_RATE}")
        t = timestamp(received_at)
        if bursts and t - bursts[-1][1] < BURST_GAP:
            bursts[-1][1] = t
            bursts[-1][2] += body_bytes
            bursts[-1][3].append(path)
        else:
            bursts.append([t, t, body_bytes, [path]])

    placed = []
    position = None
    for first_receipt, _, nbytes, paths in bursts:
        duration = nbytes / BYTES_PER_SECOND
        # The burst's audio ends about when its first chunk is posted.
        by_receipt = first_receipt - duration
        if position is None or abs(by_receipt - position) > REANCHOR:
            position = by_receipt
        placed.append((position, position + duration, paths))
        position += duration
    return placed


def zero_point(db: sqlite3.Connection, memory_path: str, started_at: str) -> tuple[float, str]:
    # Live copies of this memory's segments, matched by segment id.
    live = db.execute(
        "SELECT lp.received_at, ls.end FROM segments ms"
        " JOIN segments ls ON ls.segment_id = ms.segment_id"
        " JOIN payloads lp ON lp.path = ls.payload_path"
        " WHERE ms.payload_path = ? AND lp.type = 'transcript'",
        (memory_path,),
    ).fetchall()
    if not live:
        return timestamp(started_at), "started_at"
    return min(timestamp(received_at) - end for received_at, end in live) + LIVE_ARRIVAL_OFFSET, "live"


def cut(raw_dir: Path, bursts: list, start: float, duration: float) -> tuple[bytes, float]:
    """PCM for [start, start + duration), silence where no audio arrived."""
    total = round(duration * SAMPLE_RATE)
    pcm = bytearray(total * 2)
    covered = []
    for b_start, b_end, paths in bursts:
        if b_end <= start or b_start >= start + duration:
            continue
        audio = b"".join((raw_dir / p).read_bytes() for p in paths)
        offset = round((b_start - start) * SAMPLE_RATE)
        lo, hi = max(0, offset), min(total, offset + len(audio) // 2)
        pcm[lo * 2 : hi * 2] = audio[(lo - offset) * 2 : (hi - offset) * 2]
        covered.append((lo, hi))

    covered_samples, reach = 0, 0
    for lo, hi in sorted(covered):
        covered_samples += max(0, hi - max(lo, reach))
        reach = max(reach, hi)
    return bytes(pcm), (covered_samples / total if total else 0.0)


def write_wav(path: Path, pcm: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    tmp.replace(path)


def assemble(raw_dir: Path, db_path: Path, audio_dir: Path) -> dict:
    db = sqlite3.connect(db_path)
    try:
        bursts = place_bursts(db)
        latest = {}  # conversation_id -> (memory_path, started_at), latest memory wins
        for conversation_id, path, started_at in db.execute(
            "SELECT m.conversation_id, m.payload_path, m.started_at FROM memories m"
            " JOIN payloads p ON p.path = m.payload_path ORDER BY p.received_at, p.path"
        ):
            latest[conversation_id] = (path, started_at)

        rows = []
        for conversation_id, (memory_path, started_at) in sorted(latest.items()):
            duration = db.execute(
                "SELECT MAX(end) FROM segments WHERE payload_path = ?", (memory_path,)
            ).fetchone()[0]
            if not duration or duration <= 0:
                continue
            zero, source = zero_point(db, memory_path, started_at)
            pcm, coverage = cut(raw_dir, bursts, zero, duration)
            wav_file = None
            if coverage > 0:
                wav_file = f"{conversation_id}.wav"
                write_wav(audio_dir / wav_file, pcm)
            zero_at = datetime.fromtimestamp(zero, timezone.utc).isoformat()
            rows.append((conversation_id, memory_path, zero_at, source, duration, coverage, wav_file))

        db.executescript(SCHEMA)
        db.executemany("INSERT INTO conversation_audio VALUES (?,?,?,?,?,?,?)", rows)
        db.commit()
    finally:
        db.close()

    # WAVs are derived: remove any this run didn't produce.
    current = {r[6] for r in rows if r[6]}
    for wav in audio_dir.glob("*.wav"):
        if wav.name not in current:
            wav.unlink()

    return {
        "conversations": len(rows),
        "with_audio": len(current),
        "zero_from_live": sum(1 for r in rows if r[3] == "live"),
    }
