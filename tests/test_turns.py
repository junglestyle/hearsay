import json
import sqlite3
from datetime import datetime, timedelta, timezone

from hearsay.assemble import SCHEMA as ASSEMBLE_SCHEMA
from hearsay.parse import SCHEMA as PARSE_SCHEMA
from hearsay.turns import build_turns, record_operator_input, wall

T = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
# Our conversation starts 5 s before Omi's segment time 0, so a time in our
# WAV is Omi's time + 5.
SHIFT = 5.0


def word(text, start, end, speaker):
    return {"word": text, "start": start, "end": end, "score": 0.9, "speaker": speaker}


def setup(tmp_path, wav_sha256="sha-now"):
    """Our conversation c1, and the Omi conversation older labels point into."""
    db_path, transcripts, labels = tmp_path / "h.sqlite", tmp_path / "transcripts", tmp_path / "labels"
    transcripts.mkdir()
    labels.mkdir()
    db = sqlite3.connect(db_path)
    db.executescript(PARSE_SCHEMA + ASSEMBLE_SCHEMA)
    db.execute("INSERT INTO conversation_audio VALUES ('c1', ?, 40, 1, 'c1.wav', ?)",
               ((T - timedelta(seconds=SHIFT)).isoformat(), wav_sha256))
    # Omi's memory for its conversation omi1: no live transcript, so its time 0 is started_at = T.
    db.execute("INSERT INTO payloads VALUES ('memory/m.body', 'memory', ?, 'u', NULL, 1, 'x', NULL)", (T.isoformat(),))
    db.execute("INSERT INTO memories (payload_path, conversation_id, started_at) VALUES ('memory/m.body', 'omi1', ?)",
               (T.isoformat(),))
    # Omi's segments cut the same speech differently from our turns.
    for idx, (segment_id, start, end) in enumerate([("omi-a", 0.4, 6.2), ("omi-b", 6.6, 12.0)]):
        db.execute("INSERT INTO segments (payload_path, idx, segment_id, start, end, text) VALUES (?,?,?,?,?,?)",
                   ("memory/m.body", idx, segment_id, start, end, "omi text"))
    db.commit()
    db.close()
    s = SHIFT
    transcript = {
        "conversation_id": "c1", "wav_sha256": "sha-now", "settings": {},
        "segments": [
            {"start": s + 0.5, "end": s + 6.0, "text": "hello there 42", "speaker": "SPEAKER_00",
             "words": [word("hello", s + 0.5, s + 1.0, "SPEAKER_00"), word("there", s + 1.1, s + 6.0, "SPEAKER_00"),
                       {"word": "42"}]},
            # Speaker change mid-segment splits a turn.
            {"start": s + 6.5, "end": s + 12.0, "text": "hi and after a pause", "speaker": "SPEAKER_01",
             "words": [word("hi", s + 6.5, s + 7.0, "SPEAKER_01"), word("and", s + 7.1, s + 9.0, "SPEAKER_01"),
                       word("after", s + 9.1, s + 11.0, "SPEAKER_00"), word("pause", s + 11.1, s + 12.0, "SPEAKER_00")]},
            # A long pause splits a turn even with the same speaker; no aligned words keeps the segment whole.
            {"start": s + 20.0, "end": s + 25.0, "text": "unaligned", "speaker": "SPEAKER_00", "words": []},
        ],
    }
    (transcripts / "c1.json").write_text(json.dumps(transcript))
    return db_path, transcripts, labels


def turns(db_path):
    db = sqlite3.connect(db_path)
    rows = db.execute("SELECT turn_id, start, end, text, diar_speaker FROM turns ORDER BY idx").fetchall()
    db.close()
    return rows


def test_turns_split_on_speaker_change_and_pause(tmp_path):
    db_path, transcripts, _ = setup(tmp_path)
    counts = build_turns(db_path, transcripts)
    assert counts == {"conversations": 1, "transcribed": 1, "pending": 0, "turns": 4}
    s = SHIFT
    assert turns(db_path) == [
        ("c1:0000", s + 0.5, s + 6.0, "hello there 42", "SPEAKER_00"),
        ("c1:0001", s + 6.5, s + 9.0, "hi and", "SPEAKER_01"),
        ("c1:0002", s + 9.1, s + 12.0, "after pause", "SPEAKER_00"),
        ("c1:0003", s + 20.0, s + 25.0, "unaligned", "SPEAKER_00"),
    ]


def test_a_transcript_of_a_different_wav_is_never_used(tmp_path):
    db_path, transcripts, _ = setup(tmp_path, wav_sha256="sha-after-more-audio-arrived")
    counts = build_turns(db_path, transcripts)
    assert counts["pending"] == 1 and counts["turns"] == 0
    assert turns(db_path) == []


def test_every_generation_of_labels_lands_on_the_right_turns(tmp_path):
    db_path, transcripts, labels = setup(tmp_path)
    build_turns(db_path, transcripts)
    zero_at = (T - timedelta(seconds=SHIFT)).isoformat()
    (labels / "labels.jsonl").write_text(
        # Oldest: an Omi segment id, in Omi's timeline.
        json.dumps({"segment_id": "omi-a", "label": "owner"}) + "\n"
        # Slice 6: a span relative to Omi conversation omi1's time 0.
        + json.dumps({"turns": [{"turn_id": "gone", "conversation_id": "omi1", "start": 19.5, "end": 25.5}],
                      "label": "not_owner"}) + "\n"
    )
    (labels / "tags.jsonl").write_text(
        # Omi segment omi-b covers most of turn 1 and all of turn 2.
        json.dumps({"type": "name", "segment_ids": ["omi-b"], "name": "Bob"}) + "\n"
        + json.dumps({"type": "name", "segment_ids": ["no-longer-exists"], "name": "Nobody"}) + "\n"
        # Now: absolute times, which outlive any transcript or boundary change.
        + json.dumps({"type": "skip", "at": [wall(zero_at, SHIFT + 20.0, SHIFT + 25.0)]}) + "\n"
    )
    record_operator_input(db_path, labels)

    db = sqlite3.connect(db_path)
    found = db.execute("SELECT turn_id, kind, value FROM operator_input ORDER BY turn_id, kind").fetchall()
    db.close()
    assert found == [
        ("c1:0000", "label", "owner"),
        ("c1:0001", "name", "Bob"),
        ("c1:0002", "name", "Bob"),
        ("c1:0003", "label", "not_owner"),
        ("c1:0003", "skip", None),
    ]
