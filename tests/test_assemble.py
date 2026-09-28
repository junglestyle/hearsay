import hashlib
import json
import sqlite3
import wave
from datetime import datetime, timedelta, timezone

from hearsay.assemble import assemble
from hearsay.conversations import SCHEMA as CONVERSATIONS_SCHEMA
from hearsay.parse import rebuild

T = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
CHUNK = 32000  # one second of 16 kHz PCM16


def write_payload(raw, webhook_type, received_at, body, query):
    """Raw layout as the receiver writes it, but with a chosen receipt time."""
    day = raw / webhook_type / received_at.strftime("%Y-%m-%d")
    day.mkdir(parents=True, exist_ok=True)
    stem = received_at.strftime("%Y%m%dT%H%M%S.%fZ") + "-00000000"
    (day / f"{stem}.body").write_bytes(body)
    sidecar = {
        "received_at": received_at.isoformat(),
        "webhook_type": webhook_type,
        "query": query,
        "headers": [["idempotency-key", stem]],
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "body_file": f"{stem}.body",
    }
    (day / f"{stem}.json").write_text(json.dumps(sidecar))


def audio_burst(raw, burst, posted_at):
    """Five 1 s chunks, each filled with a sample value unique to (burst, chunk)."""
    for j in range(5):
        pcm = (burst * 10 + j + 1).to_bytes(2, "little") * (CHUNK // 2)
        write_payload(raw, "audio", posted_at + timedelta(seconds=0.09 * j), pcm,
                      [["sample_rate", "16000"], ["uid", "u"]])


def segment(id, start, end):
    return {"id": id, "text": "synthetic", "speaker": "SPEAKER_00", "start": start, "end": end, "is_user": False}


def memory(id, started_at, segments):
    return json.dumps({"id": id, "started_at": started_at.isoformat(), "transcript_segments": segments}).encode()


def seconds(s):
    return T + timedelta(seconds=s)


def add_conversations(db_path, rows):
    """What find_conversations would write, without running voice detection."""
    db = sqlite3.connect(db_path)
    db.executescript(CONVERSATIONS_SCHEMA)
    db.executemany("INSERT INTO conversations VALUES (?,?,?,?,?)", rows)
    db.commit()
    db.close()


def test_assembles_each_conversation_from_the_stream(tmp_path):
    raw, db_path, audio_dir = tmp_path / "raw", tmp_path / "h.sqlite", tmp_path / "audio"
    audio_dir.mkdir()
    (audio_dir / "stale.wav").write_bytes(b"old")

    # Audio for [0, 20) s after T. Burst 1 is posted 0.4 s late (jitter, still
    # contiguous); burst 2 is lost, so burst 3 must be re-anchored to receipt time.
    audio_burst(raw, 0, seconds(5))
    audio_burst(raw, 1, seconds(10.4))
    audio_burst(raw, 3, seconds(20))

    rebuild(raw, db_path)
    # One conversation over the audio, one where no audio arrived.
    add_conversations(db_path, [("c-audio", T.timestamp(), seconds(20).timestamp(), 15.0, 0),
                                ("c-none", seconds(500).timestamp(), seconds(505).timestamp(), 5.0, 0)])
    assemble(raw, db_path, audio_dir)

    db = sqlite3.connect(db_path)
    rows = db.execute(
        "SELECT conversation_id, zero_at, duration, coverage, wav_file FROM conversation_audio ORDER BY conversation_id"
    ).fetchall()
    db.close()
    assert rows == [
        ("c-audio", T.isoformat(), 20.0, 0.75, "c-audio.wav"),
        ("c-none", seconds(500).isoformat(), 5.0, 0.0, None),
    ]

    with wave.open(str(audio_dir / "c-audio.wav")) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
        pcm = w.readframes(w.getnframes())
    per_second = [int.from_bytes(pcm[s * CHUNK : s * CHUNK + 2], "little") for s in range(20)]
    assert per_second == [1, 2, 3, 4, 5, 11, 12, 13, 14, 15, 0, 0, 0, 0, 0, 31, 32, 33, 34, 35]
    assert sorted(p.name for p in audio_dir.iterdir()) == ["c-audio.wav"]

    first = (audio_dir / "c-audio.wav").read_bytes()
    assemble(raw, db_path, audio_dir)
    assert (audio_dir / "c-audio.wav").read_bytes() == first
