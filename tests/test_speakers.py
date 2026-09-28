import json
import math
import random
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from hearsay.assemble import assemble
from hearsay.parse import rebuild
from hearsay.turns import build_turns
from test_assemble import T, add_conversations, seconds, write_payload

pytest.importorskip("speechbrain")
MODEL_DIR = Path("/models/ecapa")
if not MODEL_DIR.is_dir():
    pytest.skip("speaker model not installed", allow_module_level=True)

from hearsay.speakers import label_speakers  # noqa: E402


def voice(f0, formants, seconds_long, seed):
    """Deterministic synthetic voice: a harmonic buzz shaped by formants.

    ECAPA separates these like different speakers (same voice ~0.85,
    different ~0.3), which plain tones don't do.
    """
    period = [0.0] * round(16000 / f0)
    for k in range(1, int(4000 / f0)):
        gain = max(0.05, sum(math.exp(-((f0 * k - f) / 150) ** 2) for f in formants))
        for n in range(len(period)):
            period[n] += gain * math.sin(2 * math.pi * k * n / len(period))
    rng = random.Random(seed)
    out = bytearray()
    for n in range(int(seconds_long * 16000)):
        t = n / 16000
        s = period[n % len(period)] * (0.6 + 0.4 * math.sin(2 * math.pi * 3 * t)) + 0.05 * rng.uniform(-1, 1)
        out += int(max(-1, min(1, s * 0.2)) * 32767).to_bytes(2, "little", signed=True)
    return bytes(out)


def stream(raw, pcm, start_s):
    """Post pcm as the receiver would get it: 5 s bursts of 1 s chunks."""
    burst = 5 * 32000
    for b in range(0, len(pcm), burst):
        chunks = [pcm[c : c + 32000] for c in range(b, min(b + burst, len(pcm)), 32000)]
        posted = seconds(start_s + (b + sum(map(len, chunks))) / 32000)
        for j, chunk in enumerate(chunks):
            write_payload(raw, "audio", posted + timedelta(seconds=0.09 * j), chunk,
                          [["sample_rate", "16000"], ["uid", "u"]])


def test_segments_of_the_enrolled_voice_score_higher(tmp_path):
    raw, db_path = tmp_path / "raw", tmp_path / "h.sqlite"
    audio_dir, labels_dir, transcripts_dir = tmp_path / "audio", tmp_path / "labels", tmp_path / "transcripts"
    for d in (audio_dir, labels_dir, transcripts_dir):
        d.mkdir()
    owner, other = (110, (700, 1200, 2500)), (230, (400, 2200, 3000))

    # [0, 30) s: the owner alone, for enrollment. [40, 60) s: a conversation
    # alternating owner and other every 5 s, plus one segment too short to embed.
    timeline = voice(*owner, 30, 1) + bytes(10 * 32000)
    for i in range(4):
        timeline += voice(*(owner if i % 2 == 0 else other), 5, 10 + i)
    stream(raw, timeline, 0)
    (labels_dir / "enrollment.json").write_text(json.dumps([{"start": T.isoformat(), "end": seconds(30).isoformat()}]))

    rebuild(raw, db_path)
    # The conversation voice detection would find over [40, 60) s.
    add_conversations(db_path, [("c", seconds(40).timestamp(), seconds(60).timestamp(), 20.0, 0)])
    assemble(raw, db_path, audio_dir)
    # The transcript the dev box would make: one turn per 5 s voice sample
    # (named by its word), plus a short turn from a third voice.
    db = sqlite3.connect(db_path)
    wav_sha256 = db.execute("SELECT wav_sha256 FROM conversation_audio WHERE conversation_id = 'c'").fetchone()[0]
    db.close()
    spans = [("s0", 0, 5, "A"), ("s1", 5, 10, "B"), ("s2", 10, 15, "A"), ("s3", 15, 19, "B"), ("short", 19.2, 19.7, "C")]
    transcript_segments = [
        {"start": s, "end": e, "text": w, "speaker": spk,
         "words": [{"word": w, "start": s, "end": e, "score": 1.0, "speaker": spk}]}
        for w, s, e, spk in spans
    ]
    (transcripts_dir / "c.json").write_text(json.dumps(
        {"conversation_id": "c", "wav_sha256": wav_sha256, "settings": {}, "segments": transcript_segments}))
    build_turns(db_path, transcripts_dir)
    counts = label_speakers(raw, db_path, labels_dir, MODEL_DIR)

    db = sqlite3.connect(db_path)
    similarity = dict(db.execute(
        "SELECT t.text, ts.owner_similarity FROM turn_speakers ts JOIN turns t ON t.turn_id = ts.turn_id"))
    enrollment = db.execute("SELECT coverage, pieces FROM owner_enrollment").fetchall()
    db.close()
    assert enrollment == [(1.0, 10)]
    assert counts["embedded"] == 4
    assert similarity["short"] is None
    assert min(similarity["s0"], similarity["s2"]) > max(similarity["s1"], similarity["s3"])
