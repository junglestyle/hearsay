import ctypes
import hashlib
import json
import math
import sqlite3
import struct
from datetime import timedelta

import pytest

from hearsay import capture
from hearsay.assemble import cut, place_bursts
from hearsay.capture import ACTION, AUDIO, BUTTON, CONNECTED, MIC, STORED, SYSTEM, load_captures
from hearsay.parse import rebuild
from hearsay.speakers import by_channel, label_for, mac_channels, with_channel
from test_assemble import T

try:
    OPUS = capture.load_opus()
except OSError:
    OPUS = None
pytestmark = pytest.mark.skipif(OPUS is None, reason="libopus not installed")

FRAME = 320  # codec 21: 20 ms at 16 kHz


def opus_frames(n, amplitude=8000):
    """n frames of a 440 Hz tone, encoded as the consumer pendant does."""
    OPUS.opus_encoder_create.restype = ctypes.c_void_p
    OPUS.opus_encoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    OPUS.opus_encode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_int,
                                 ctypes.c_char_p, ctypes.c_int32]
    error = ctypes.c_int()
    encoder = OPUS.opus_encoder_create(16000, 1, 2051, ctypes.byref(error))  # RESTRICTED_LOWDELAY
    out = []
    for i in range(n):
        pcm = (ctypes.c_int16 * FRAME)(*(int(amplitude * math.sin(2 * math.pi * 440 * (i * FRAME + j) / 16000))
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
    # No ACTION records: a recorder from before configurable buttons, where a
    # single tap meant start.
    db = sqlite3.connect(db_path)
    assert db.execute("SELECT at, kind FROM marks").fetchall() == [(t0 + 7.5, "start")]
    db.close()
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


def test_paused_stretch_is_a_gap_until_kept(tmp_path):
    # The iPhone app (ios/Hearsay/Pause.swift) seals the spool at pause and
    # starts each destination with a CONNECTED record. A dropped pause leaves
    # a gap with nothing concealed in it; a paused window kept days later
    # arrives last and fills its place.
    raw, db_path, cache = tmp_path / "raw", tmp_path / "h.sqlite", tmp_path / "capture-pcm"
    t0 = T.timestamp()
    frames = opus_frames(150)

    def stretch(start, first_index):
        out = [record(start, CONNECTED, bytes([21]))]
        for i in range(50):
            index = first_index + i
            out.append(record(start + (i + 1) * 0.02, AUDIO, struct.pack("<HB", index, 0) + frames[index]))
        return b"".join(out)

    upload(raw, T + timedelta(seconds=10), "k-before", stretch(t0, 0))
    upload(raw, T + timedelta(seconds=20), "k-after", stretch(t0 + 2, 100))
    rebuild(raw, db_path)
    assert load_captures(raw, db_path, cache)["runs"] == 2

    upload(raw, T + timedelta(days=3), "k-paused", stretch(t0 + 1, 50))
    rebuild(raw, db_path)
    assert load_captures(raw, db_path, cache)["lost_frames"] == 0
    db = sqlite3.connect(db_path)
    rows = db.execute("SELECT start, duration FROM captured_audio").fetchall()
    db.close()
    assert len(rows) == 1 and abs(rows[0][0] - t0) < 0.05 and rows[0][1] == pytest.approx(3.0)


def stored_packet(stamp, frames):
    """A packet as the pendant stores it: frames, zero padding standing in
    for frames that would fill it, then the length byte of a frame that
    doesn't fit (it starts the next packet) and stale bytes."""
    audio = bytearray()
    for frame in frames:
        audio += bytes([len(frame)]) + frame
    size = len(frames[0])
    marker = 440 - size // 2
    audio += bytes(marker - len(audio)) + bytes([size])
    audio += b"\xaa" * (440 - len(audio))
    return struct.pack(">I", stamp) + bytes(audio)


def test_audio_stored_while_away_is_placed_by_the_pendants_clock(tmp_path):
    raw, db_path, cache = tmp_path / "raw", tmp_path / "h.sqlite", tmp_path / "capture-pcm"
    away = int(T.timestamp())
    frames = opus_frames(100)

    # Away from 'away': 1 s of speech 10 s in, the mic sleeps, then 1 s more
    # at 30 s. Five frames (100 ms) per packet. Then the pendant reboots and
    # its clock restarts from before the away stretch.
    packets = []
    for i in range(10):
        packets.append(stored_packet(away + 10 + (i + 1) // 10, frames[i * 5 : i * 5 + 5]))
    for i in range(10):
        packets.append(stored_packet(away + 30 + (i + 1) // 10, frames[50 + i * 5 : 55 + i * 5]))
    packets.append(stored_packet(away - 600, frames[:5]))

    def download(at, first_seq, chunk):
        return b"".join(record(at, STORED, struct.pack("<BQd", 21, first_seq + i, away) + p)
                        for i, p in enumerate(chunk))

    # The first download broke after 12 packets; the next one starts over at
    # the last packet the pendant counted as sent.
    upload(raw, T + timedelta(minutes=5), "k-1", download(away + 60, 1000, packets[:12]))
    upload(raw, T + timedelta(minutes=6), "k-2", download(away + 70, 1011, packets[11:]))
    rebuild(raw, db_path)
    report = load_captures(raw, db_path, cache)
    assert (report["stored_runs"], report["stored_unplaced"]) == (2, 1)

    db = sqlite3.connect(db_path)
    rows = db.execute("SELECT start, duration, frames, lost FROM captured_audio ORDER BY start").fetchall()
    db.close()
    (s1, d1, n1, lost1), (s2, d2, n2, _) = rows
    assert (n1, n2, lost1) == (50, 50, 0) and d1 == pytest.approx(1.0) and d2 == pytest.approx(1.0)
    assert abs(s1 - (away + 10)) < 1 and abs(s2 - (away + 30)) < 1


def test_marks_follow_what_the_app_did_with_each_tap(tmp_path):
    raw, db_path = tmp_path / "raw", tmp_path / "h.sqlite"
    t0 = T.timestamp()
    tap, double = struct.pack("<ii", 1, 0), struct.pack("<ii", 2, 0)
    body = b"".join([
        # Single tap set to do nothing, then to start, then a double tap to end.
        record(t0, BUTTON, tap), record(t0, ACTION, bytes([1, 0])),
        record(t0 + 10, BUTTON, tap), record(t0 + 10, ACTION, bytes([1, 1])),
        record(t0 + 20, BUTTON, double), record(t0 + 20, ACTION, bytes([2, 2])),
        # A pause: a fact, not a mark.
        record(t0 + 30, BUTTON, double), record(t0 + 30, ACTION, bytes([2, 3])),
    ])
    upload(raw, T + timedelta(minutes=1), "k-actions", body)
    rebuild(raw, db_path)
    db = sqlite3.connect(db_path)
    assert db.execute("SELECT at, kind FROM marks ORDER BY at").fetchall() == [(t0 + 10, "start"), (t0 + 20, "end")]
    db.close()


def test_mac_channels_are_mixed_win_over_the_pendant_and_say_who_spoke(tmp_path):
    raw, db_path, cache = tmp_path / "raw", tmp_path / "h.sqlite", tmp_path / "capture-pcm"
    t0 = T.timestamp()
    tone, quiet = opus_frames(100), opus_frames(100, amplitude=0)

    # A 2 s Mac recording: the owner speaks for the first second (mic), the
    # call for the next (system). The pendant heard the same 2 s.
    mac = [record(t0 + i * 0.02, MIC, struct.pack("<H", FRAME) + (tone if i < 50 else quiet)[i]) for i in range(100)]
    mac += [record(t0 + i * 0.02, SYSTEM, struct.pack("<H", FRAME) + (quiet if i < 50 else tone)[i])
            for i in range(100)]
    pendant = [record(t0 - 0.5, CONNECTED, bytes([21]))]
    pendant += [record(t0 + (i + 1) * 0.02, AUDIO, struct.pack("<HB", i, 0) + tone[i]) for i in range(100)]
    upload(raw, T + timedelta(seconds=70), "k-mac", b"".join(mac))
    upload(raw, T + timedelta(seconds=71), "k-pendant", b"".join(pendant))
    rebuild(raw, db_path)
    load_captures(raw, db_path, cache)

    db = sqlite3.connect(db_path)
    rows = {channel: (start, duration, path) for start, duration, channel, path
            in db.execute("SELECT start, duration, channel, pcm_path FROM mac_audio")}
    assert set(rows) == {"mic", "system", "mixed"}
    start, duration, mixed = rows["mixed"]
    assert start == pytest.approx(t0) and duration == pytest.approx(2.0)

    # The Mac's mix is what the conversation's audio is cut from.
    pcm, coverage = cut(raw, place_bursts(db), t0, 2.0)
    assert coverage == 1.0 and pcm == open(mixed, "rb").read()[: len(pcm)]

    channels = mac_channels(db)
    db.close()
    assert by_channel(raw, channels, t0 + 0.1, 0.8) == "owner"
    assert by_channel(raw, channels, t0 + 1.1, 0.8) == "not_owner"
    assert by_channel(raw, channels, t0 + 5, 1.0) is None  # outside the recording


def test_a_clear_voice_overrides_the_channel():
    # Built-in speakers: the other side, louder on the mic, sounds nothing like the owner.
    assert with_channel(label_for(0.03), "owner") == ("not_owner", "voice")
    # A voice that can't tell leaves it to the channel.
    assert with_channel(label_for(0.25), "owner") == ("owner", "channel")
