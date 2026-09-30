"""Cut per-conversation WAV files out of the continuous audio stream.

Reads the conversations found in the stream (hearsay/conversations.py) and
the raw audio bodies. Writes one WAV per conversation plus the
conversation_audio table. Everything here is derived from raw and is rebuilt
on every run.

Omi posts audio in bursts: 5-6 s of audio as 1 s chunks ~0.09 s apart
(measured on real captures). Consecutive bursts are nearly always contiguous
audio, and receipt time jitters by about +/-0.5 s, so bursts are laid end to
end and only re-anchored to receipt time when the two disagree, which means
audio was lost.
"""

import hashlib
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

SCHEMA = """
DROP TABLE IF EXISTS conversation_audio;
-- One row per conversation.
CREATE TABLE conversation_audio (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(conversation_id),
    zero_at TEXT NOT NULL,           -- wall time (UTC) where the WAV starts
    duration REAL NOT NULL,          -- seconds
    coverage REAL NOT NULL,          -- fraction of duration with received audio
    wav_file TEXT,                   -- relative to the audio dir; NULL if no audio
    wav_sha256 TEXT                  -- transcripts are only used for this exact WAV
);
"""


def timestamp(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def place_bursts(db: sqlite3.Connection) -> list[tuple[float, float, list[str]]]:
    """Return (start, end, body paths) per burst, in wall-clock seconds.

    Imported audio (hearsay/imports.py) comes first, one burst per import with
    an absolute path to its decoded PCM, so live audio laid after it wins
    wherever the two overlap. Our recorder's audio (hearsay/capture.py) comes
    next, one burst per run, then Omi's webhook audio. The recorder and Omi's
    app can't both be connected to the pendant, so those two never overlap.
    """
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
    if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'imported_audio'").fetchone():
        for start, duration, pcm_path in db.execute(
            "SELECT start, duration, pcm_path FROM imported_audio ORDER BY start, file"
        ):
            placed.append((start, start + duration, [pcm_path]))
    if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'captured_audio'").fetchone():
        for start, duration, pcm_path in db.execute(
            "SELECT start, duration, pcm_path FROM captured_audio ORDER BY start"
        ):
            placed.append((start, start + duration, [pcm_path]))
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


def cut(raw_dir: Path, bursts: list, start: float, duration: float) -> tuple[bytes, float]:
    """PCM for [start, start + duration), silence where no audio arrived."""
    total = round(duration * SAMPLE_RATE)
    pcm = bytearray(total * 2)
    covered = []
    for b_start, b_end, paths in bursts:
        if b_end <= start or b_start >= start + duration:
            continue
        offset = round((b_start - start) * SAMPLE_RATE)
        if paths[0].endswith(".pcm"):
            # Decoded audio (an import, a recorder run) can be hours long:
            # read only the part needed.
            skip = max(0, -offset)
            with open(paths[0], "rb") as f:
                f.seek(skip * 2)
                audio = f.read((total - max(0, offset)) * 2)
            offset += skip
        else:
            audio = b"".join((raw_dir / p).read_bytes() for p in paths)
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
        rows = []
        for conversation_id, start, end in db.execute(
            "SELECT conversation_id, start, end FROM conversations ORDER BY start"
        ).fetchall():
            pcm, coverage = cut(raw_dir, bursts, start, end - start)
            wav_file = wav_sha256 = None
            if coverage > 0:
                wav_file = f"{conversation_id}.wav"
                write_wav(audio_dir / wav_file, pcm)
                wav_sha256 = hashlib.sha256((audio_dir / wav_file).read_bytes()).hexdigest()
            zero_at = datetime.fromtimestamp(start, timezone.utc).isoformat()
            rows.append((conversation_id, zero_at, end - start, coverage, wav_file, wav_sha256))

        db.executescript(SCHEMA)
        db.executemany("INSERT INTO conversation_audio VALUES (?,?,?,?,?,?)", rows)
        db.commit()
    finally:
        db.close()

    return {"conversations": len(rows), "with_audio": sum(1 for r in rows if r[4])}


def remove_stale_wavs(audio_dir: Path, db_path: Path) -> int:
    """Delete WAVs the database no longer refers to.

    Separate from assemble: reprocess calls it only after the new database is
    in place, so the one readers see never points at a deleted file.
    """
    db = sqlite3.connect(db_path)
    try:
        current = {name for (name,) in db.execute("SELECT wav_file FROM conversation_audio WHERE wav_file IS NOT NULL")}
    finally:
        db.close()
    stale = [wav for wav in audio_dir.glob("*.wav") if wav.name not in current]
    for wav in stale:
        wav.unlink()
    return len(stale)
