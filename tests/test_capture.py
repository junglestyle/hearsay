import ctypes
import hashlib
import json
import math
import sqlite3
import struct
from datetime import timedelta

import pytest

from hearsay import capture
from hearsay.capture import AUDIO, BUTTON, CONNECTED, load_captures
from hearsay.parse import rebuild
from test_assemble import T

try:
    OPUS = capture.load_opus()
except OSError:
    OPUS = None
pytestmark = pytest.mark.skipif(OPUS is None, reason="libopus not installed")

FRAME = 320  # codec 21: 20 ms at 16 kHz


def opus_frames(n):
    """n frames of a 440 Hz tone, encoded as the consumer pendant does."""
    OPUS.opus_encoder_create.restype = ctypes.c_void_p
    OPUS.opus_encoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    OPUS.opus_encode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_int,
                                 ctypes.c_char_p, ctypes.c_int32]
    error = ctypes.c_int()
    encoder = OPUS.opus_encoder_create(16000, 1, 2051, ctypes.byref(error))  # RESTRICTED_LOWDELAY
    out = []
    for i in range(n):
        pcm = (ctypes.c_int16 * FRAME)(*(int(8000 * math.sin(2 * math.pi * 440 * (i * FRAME + j) / 16000))
                                          for j in range(FRAME)))
        buf = ctypes.create_string_buffer(160)
        length = OPUS.opus_encode(encoder, pcm, FRAME, buf, 160)
        out.append(buf.raw[:length])
    return out


def record(at, kind, data):
    return struct.pack("<dBH", at, kind, len(data)) + data


def upload(raw, received_at, key, body):
    """Raw layout as the capture receiver writes it."""
    day = raw / "capture" / received_at.strftime("%Y-%m-%d")
    day.mkdir(parents=True, exist_ok=True)
    stem = received_at.strftime("%Y%m%dT%H%M%S.%fZ") + "-" + key[-8:]
    (day / f"{stem}.body").write_bytes(body)
    (day / f"{stem}.json").write_text(json.dumps({
        "received_at": received_at.isoformat(), "webhook_type": "capture", "query": [],
        "headers": [["idempotency-key", key]], "body_sha256": hashlib.sha256(body).hexdigest(),
        "body_file": f"{stem}.body"}))


def test_recorder_uploads_become_timed_decoded_runs(tmp_path):
    raw, db_path, cache = tmp_path / "raw", tmp_path / "h.sqlite", tmp_path / "capture-pcm"
    t0 = T.timestamp()
    frames = opus_frames(150)

    # 2 s of audio arriving in real time, three frames per 60 ms connection
    # event, with one notification lost (index 40). Then the mic sleeps for
    # 5 s and 1 s more arrives. A tap comes during the second stretch.
    records = [record(t0 - 0.5, CONNECTED, bytes([21]))]
    index = 0
    for i in range(100):
        if i != 40:
            at = t0 + math.ceil((i + 1) / 3) * 0.06
            records.append(record(at, AUDIO, struct.pack("<HB", index, 0) + frames[i]))
        index += 1
    for i in range(50):
        at = t0 + 7 + (i + 1) * 0.02
        records.append(record(at, AUDIO, struct.pack("<HB", index, 0) + frames[100 + i]))
        index += 1
    records.insert(-10, record(t0 + 7.5, BUTTON, struct.pack("<ii", 1, 0)))

    # Two uploads; the second arrived first (a retry), and the first was
    # delivered twice.
    first, second = b"".join(records[:80]), b"".join(records[80:])
    upload(raw, T + timedelta(seconds=70), "k-second", second)
    upload(raw, T + timedelta(seconds=90), "k-first", first)
    upload(raw, T + timedelta(seconds=95), "k-first", first)

    counts = rebuild(raw, db_path)
    assert counts["capture"] == 2 and counts["duplicate"] == 1
    assert load_captures(raw, db_path, cache)["runs"] == 2

    db = sqlite3.connect(db_path)
    rows = db.execute("SELECT start, duration, frames, lost, pcm_path FROM captured_audio ORDER BY start").fetchall()
    taps = db.execute("SELECT at, event FROM button_events").fetchall()
    db.close()
    assert taps == [(t0 + 7.5, 1)]
    (s1, d1, n1, lost1, pcm1), (s2, d2, n2, lost2, _) = rows
    assert abs(s1 - t0) < 0.05 and d1 == pytest.approx(2.0) and (n1, lost1) == (100, 1)
    assert abs(s2 - (t0 + 7)) < 0.05 and d2 == pytest.approx(1.0) and (n2, lost2) == (50, 0)

    # Decoded to audible PCM, 2 s long.
    pcm = open(pcm1, "rb").read()
    assert len(pcm) == 2 * 16000 * 2
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    assert max(abs(s) for s in samples[16000:]) > 4000

    # The cache is keyed by content, so an unchanged run isn't decoded again,
    # and runs that no longer exist leave nothing behind.
    load_captures(raw, db_path, cache)
    assert len(list(cache.glob("*.pcm"))) == 2
    for body in raw.glob("capture/*/*"):
        body.unlink()
    rebuild(raw, db_path)
    load_captures(raw, db_path, cache)
    assert list(cache.glob("*.pcm")) == []
