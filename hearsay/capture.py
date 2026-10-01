"""Audio from our own recorder, placed on the timeline.

The recorder (the iPhone app, ios/; before it a Mac recorder, removed after
slice 9, whose uploads are the same format) connects to the Omi pendant over
BLE and uploads what the pendant sends, verbatim, with the time each
notification arrived. The
receiver (create_capture_app) stores each upload in raw/capture/. An upload
is a sequence of records, little-endian:

    f64 at      unix seconds on the recorder's clock when it arrived
    u8  kind    CONNECTED, AUDIO, BUTTON, STORED or ACTION
    u16 length
    bytes       CONNECTED: the codec byte read on connect (19B10002); the
                iPhone app also writes one on resuming from a pause, since
                the index jumps over audio that went elsewhere
                AUDIO: one notification from 19B10001, header included
                BUTTON: one notification from 23BA7925
                STORED: codec u8, the pendant's sequence number u64,
                start of the pendant's away stretch f64 (0 if unknown),
                then one 444-byte packet of audio it stored while away
                ACTION: the button event u8 and what the iPhone app did
                about it u8 (ACTION_* below), written after the event

An audio notification is [index u16 LE][sub u8][Opus bytes]. index counts
notifications and wraps at 65535; sub 0 starts a frame, and a frame split
over several notifications continues with sub 1, 2... (at the MTU the pendant
negotiates, a frame always fits in one). The stream carries no time of its
own, and the pendant's mic sleeps after 10 s of silence without saying so, so
frames are timed by arrival: laid end to end, and started afresh when a
frame arrives more than REANCHOR away from where that puts it.
Frames lost in transit (a gap in index) are filled by Opus loss concealment.

The pendant stores audio only while no phone is connected, and the iPhone app
downloads it on reconnect. A stored packet is [time u32 BE][440 bytes of
[len u8][Opus frame]...], the time being the pendant's clock, in whole
seconds, when the packet filled, so about when its last frame ended. Packets
are placed by that clock, which after an unclean reboot restarts from the
last time a phone set it: a packet is placed only if its time is within the
away stretch (after it began, before the download) and doesn't step back
from the packets before it. The rest stay in raw, unplaced.

Protocol facts are from Omi's firmware (BasedHardware/omi, MIT):
omi/firmware/omi/src/lib/core/transport.c, codec.c, button.c.

Reprocess decodes each run of frames once, to PCM16 mono at 16 kHz, cached by
the run's content, and records it in captured_audio for place_bursts.
"""

import ctypes
import ctypes.util
import hashlib
import sqlite3
import struct
from pathlib import Path

from hearsay.assemble import SAMPLE_RATE

CONNECTED, AUDIO, BUTTON, STORED, ACTION = 1, 2, 3, 4, 5
RECORD_HEADER = struct.Struct("<dBH")
STORED_HEADER = struct.Struct("<BQd")
STORED_PACKET = 444
STORED_AUDIO = 440

# Codec byte -> samples per Opus frame. 21 is the consumer pendant, 20 the
# DevKit; the firmware builds nothing else.
FRAME_SAMPLES = {20: 160, 21: 320}

# Button events (button.c): the consumer firmware sends SINGLE_TAP and
# DOUBLE_TAP, each followed by a RELEASE (5); holds aren't reported.
SINGLE_TAP = 1

# What the iPhone app did about a button event. Only marks matter here:
# START keeps the conversation around it however short, END splits there.
# The rest (nothing, paused, resumed, muted, unmuted) are recorded as facts.
ACTION_START, ACTION_END = 1, 2

# A frame arriving this far from where end-to-end placement puts it starts a
# new run: the mic slept, or the link dropped.
REANCHOR = 1.0

# Stored packets carry whole seconds, so their slack is wider.
STORED_REANCHOR = 2.0
CLOCK_SLACK = 2.0

SCHEMA = """
DROP TABLE IF EXISTS captured_audio;
CREATE TABLE captured_audio (
    start REAL NOT NULL,             -- unix seconds
    duration REAL NOT NULL,          -- seconds of decoded audio
    frames INTEGER NOT NULL,
    lost INTEGER NOT NULL,           -- frames lost in transit, concealed
    pcm_path TEXT NOT NULL           -- absolute path of the decoded PCM cache
);
"""


def read_records(body: bytes) -> list[tuple[float, int, bytes]]:
    """(at, kind, data) per record. Raises ValueError on a malformed upload."""
    records = []
    offset = 0
    while offset < len(body):
        if offset + RECORD_HEADER.size > len(body):
            raise ValueError(f"truncated record header at byte {offset}")
        at, kind, length = RECORD_HEADER.unpack_from(body, offset)
        offset += RECORD_HEADER.size
        if kind not in (CONNECTED, AUDIO, BUTTON, STORED, ACTION):
            raise ValueError(f"unknown record kind {kind} at byte {offset}")
        if offset + length > len(body):
            raise ValueError(f"truncated record at byte {offset}")
        records.append((at, kind, body[offset : offset + length]))
        offset += length
    return records


def button_event(data: bytes) -> int:
    """The event code: the first of two int32 LE the pendant sends."""
    if len(data) < 4:
        raise ValueError(f"button notification of {len(data)} bytes")
    return int.from_bytes(data[:4], "little")


def marks(records: list[tuple[float, int, bytes]]) -> list[tuple[float, str]]:
    """(at, "start" or "end") for the marks the owner made in one upload.

    The iPhone app writes an ACTION record for every tap it handles. An
    upload with none comes from a recorder that didn't (the Mac recorder,
    earlier app builds), where a single tap always meant start.
    """
    actions = [(at, data[1]) for at, kind, data in records if kind == ACTION and len(data) >= 2]
    if not actions:
        return [(at, "start") for at, kind, data in records if kind == BUTTON and button_event(data) == SINGLE_TAP]
    names = {ACTION_START: "start", ACTION_END: "end"}
    return [(at, names[action]) for at, action in actions if action in names]


def frames(records: list[tuple[float, int, bytes]]) -> list[tuple[float, int, bytes | None]]:
    """(arrival, codec, Opus frame or None for a lost one) in arrival order.

    Reassembles frames from notifications. On a gap in index the frame so far
    is kept (at the pendant's MTU it is always whole) and each missing
    notification counts as a lost frame; the continuation of a frame whose
    start was lost is dropped. A new connection resets everything, since the
    pendant's index restarts.
    """
    out = []
    codec = None
    last_index = None
    pending = None  # [arrival, sub, bytes]

    def flush():
        nonlocal pending
        if pending:
            out.append((pending[0], codec, bytes(pending[2])))
        pending = None

    for at, kind, data in records:
        if kind == CONNECTED:
            flush()
            codec = data[0] if data and data[0] in FRAME_SAMPLES else None
            last_index = None
            continue
        if kind != AUDIO or codec is None or len(data) < 3:
            continue
        index, sub = int.from_bytes(data[:2], "little"), data[2]
        if last_index is not None and index != (last_index + 1) & 0xFFFF:
            flush()
            out += [(at, codec, None)] * ((index - last_index - 1) & 0xFFFF)
        last_index = index
        if sub == 0:
            flush()
            pending = [at, 0, bytearray(data[3:])]
        elif pending and sub == pending[1] + 1:
            pending[0], pending[1] = at, sub
            pending[2] += data[3:]
        else:
            pending = None
    flush()
    return out


def runs(frame_list: list[tuple[float, int, bytes | None]]) -> list[tuple[float, int, list[bytes | None]]]:
    """(start, codec, frames) per continuous run of audio.

    A frame's audio ends about when it arrives. A frame more than REANCHOR
    off from where laying it end to end puts it starts a new run. Lost frames
    keep their place inside a run, but never start one, and never push the
    run past their arrival (a reset pendant can jump its index by thousands).
    """
    out = []
    end = None
    for at, codec, frame in frame_list:
        duration = FRAME_SAMPLES[codec] / SAMPLE_RATE
        if not out or out[-1][1] != codec or abs(at - (end + duration)) > REANCHOR:
            if frame is None:
                continue
            out.append((at - duration, codec, []))
            end = at - duration
        out[-1][2].append(frame)
        end += duration
    for _, _, run in out:
        while run[-1] is None:
            run.pop()
    return out


def stored_frames(audio: bytes) -> list[bytes]:
    """Opus frames from a stored packet's 440 bytes.

    0 is padding. When a frame doesn't fit, the firmware writes its length
    byte without the frame (which starts the next packet) and leaves stale
    bytes after it, so a frame running to the end or past it ends the packet.
    """
    out = []
    offset = 0
    while offset < len(audio) - 1:
        size = audio[offset]
        if size == 0:
            offset += 1
            continue
        if offset + 1 + size >= len(audio):
            break
        out.append(audio[offset + 1 : offset + 1 + size])
        offset += 1 + size
    return out


def stored_runs(records: list[tuple[float, int, bytes]]) -> tuple[list[tuple[float, int, list[bytes | None]]], int]:
    """(runs as runs() gives them, packets left unplaced) from STORED records.

    A packet downloaded twice (a transfer broken and resumed) is kept once.
    Packets go in the pendant's order; each one's frames end about half a
    second after its whole-second time, and a run is laid end to end until a
    packet lands more than STORED_REANCHOR away.
    """
    packets = {}
    for at, kind, data in records:
        if kind != STORED or len(data) != STORED_HEADER.size + STORED_PACKET:
            continue
        codec, seq, away_since = STORED_HEADER.unpack_from(data)
        packet = data[STORED_HEADER.size :]
        packets.setdefault((seq, packet), (at, codec, away_since))

    out = []
    unplaced = 0
    end = None
    latest = None  # the pendant's time on the packet before, if placed
    for (seq, packet), (at, codec, away_since) in sorted(packets.items(), key=lambda p: p[0][0]):
        if codec not in FRAME_SAMPLES:
            unplaced += 1
            continue
        stamp = int.from_bytes(packet[:4], "big") + 0.5
        if stamp < away_since - CLOCK_SLACK or stamp > at + CLOCK_SLACK or (latest and stamp < latest - CLOCK_SLACK):
            unplaced += 1
            continue
        latest = max(latest or stamp, stamp)
        frame_list = stored_frames(packet[4:])
        if not frame_list:
            continue
        duration = len(frame_list) * FRAME_SAMPLES[codec] / SAMPLE_RATE
        if not out or out[-1][1] != codec or abs(stamp - (end + duration)) > STORED_REANCHOR:
            out.append((stamp - duration, codec, []))
            end = stamp - duration
        out[-1][2].extend(frame_list)
        end += duration
    return out, unplaced


def load_opus() -> ctypes.CDLL:
    for name in (ctypes.util.find_library("opus"), "libopus.so.0", "/opt/homebrew/lib/libopus.dylib"):
        if not name:
            continue
        try:
            lib = ctypes.CDLL(name)
        except OSError:
            continue
        lib.opus_decoder_create.restype = ctypes.c_void_p
        lib.opus_decoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        lib.opus_decode.restype = ctypes.c_int
        lib.opus_decode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32,
                                    ctypes.POINTER(ctypes.c_int16), ctypes.c_int, ctypes.c_int]
        lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
        return lib
    raise OSError("libopus not found")


def decode(opus: ctypes.CDLL, codec: int, run: list[bytes | None]) -> bytes:
    samples = FRAME_SAMPLES[codec]
    error = ctypes.c_int()
    decoder = opus.opus_decoder_create(SAMPLE_RATE, 1, ctypes.byref(error))
    if error.value:
        raise RuntimeError(f"opus_decoder_create failed: {error.value}")
    try:
        pcm = (ctypes.c_int16 * samples)()
        out = bytearray()
        for frame in run:
            n = opus.opus_decode(decoder, frame, len(frame), pcm, samples, 0) if frame else -1
            if n != samples:
                # Lost or undecodable: the decoder conceals it from what came before.
                n = opus.opus_decode(decoder, None, 0, pcm, samples, 0)
            if n != samples:
                raise RuntimeError(f"Opus loss concealment gave {n} samples, expected {samples}")
            out += bytes(pcm)
        return bytes(out)
    finally:
        opus.opus_decoder_destroy(decoder)


def run_key(codec: int, run: list[bytes | None]) -> str:
    h = hashlib.sha256(bytes([codec]))
    for frame in run:
        h.update(len(frame).to_bytes(2, "little") + frame if frame else b"\xff\xff")
    return h.hexdigest()


def load_captures(raw_dir: Path, db_path: Path, cache_dir: Path) -> dict:
    db = sqlite3.connect(db_path)
    try:
        paths = [p for (p,) in db.execute(
            "SELECT path FROM payloads WHERE type = 'capture' AND duplicate_of IS NULL")]
        # Records within an upload are in the order they arrived. Uploads can
        # reach the NAS out of order after a retry, so they're put back in
        # order by their first record.
        uploads = [read_records((raw_dir / path).read_bytes()) for path in paths]
        uploads.sort(key=lambda records: records[0][0] if records else 0.0)
        records = [r for records in uploads for r in records]
        stored, unplaced = stored_runs(records)
        found = runs(frames(records)) + stored

        cache_dir.mkdir(parents=True, exist_ok=True)
        opus = load_opus() if found else None
        rows = []
        for start, codec, run in found:
            pcm = cache_dir / f"{run_key(codec, run)}.pcm"
            if not pcm.exists():
                tmp = pcm.with_name(pcm.name + ".tmp")
                tmp.write_bytes(decode(opus, codec, run))
                tmp.replace(pcm)
            rows.append((start, len(run) * FRAME_SAMPLES[codec] / SAMPLE_RATE, len(run),
                         sum(1 for f in run if f is None), str(pcm)))
        # The cache is derived: drop PCM for runs that no longer exist.
        current = {r[4] for r in rows}
        for pcm in cache_dir.glob("*.pcm"):
            if str(pcm) not in current:
                pcm.unlink()

        db.executescript(SCHEMA)
        db.executemany("INSERT INTO captured_audio VALUES (?,?,?,?,?)", rows)
        db.commit()
    finally:
        db.close()
    return {"uploads": len(paths), "runs": len(rows), "hours": round(sum(r[1] for r in rows) / 3600, 2),
            "lost_frames": sum(r[3] for r in rows), "stored_runs": len(stored), "stored_unplaced": unplaced}
