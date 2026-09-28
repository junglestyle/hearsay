import json
import sqlite3

from hearsay.assemble import SCHEMA as ASSEMBLE_SCHEMA
from hearsay.parse import SCHEMA as PARSE_SCHEMA
from hearsay.turns import build_turns, record_operator_input


def word(text, start, end, speaker):
    return {"word": text, "start": start, "end": end, "score": 0.9, "speaker": speaker}


def setup(tmp_path, wav_sha256="sha-now"):
    """One conversation whose WAV is sha-now, and the Omi segments older labels name."""
    db_path, transcripts, labels = tmp_path / "h.sqlite", tmp_path / "transcripts", tmp_path / "labels"
    transcripts.mkdir()
    labels.mkdir()
    db = sqlite3.connect(db_path)
    db.executescript(PARSE_SCHEMA + ASSEMBLE_SCHEMA)
    db.execute("INSERT INTO conversation_audio VALUES ('c1', 'memory/m.body', '2026-01-01T00:00:00+00:00',"
               " 'live', 30, 1, 'c1.wav', ?)", (wav_sha256,))
    # Omi's segments cut the same speech differently from our turns.
    for idx, (segment_id, start, end) in enumerate([("omi-a", 0.4, 6.2), ("omi-b", 6.6, 12.0)]):
        db.execute("INSERT INTO segments (payload_path, idx, segment_id, start, end, text) VALUES (?,?,?,?,?,?)",
                   ("memory/m.body", idx, segment_id, start, end, "omi text"))
    db.commit()
    db.close()
    transcript = {
        "conversation_id": "c1", "wav_sha256": "sha-now", "settings": {},
        "segments": [
            {"start": 0.5, "end": 6.0, "text": "hello there 42", "speaker": "SPEAKER_00",
             "words": [word("hello", 0.5, 1.0, "SPEAKER_00"), word("there", 1.1, 6.0, "SPEAKER_00"), {"word": "42"}]},
            # Speaker change mid-segment splits a turn.
            {"start": 6.5, "end": 12.0, "text": "hi and after a pause", "speaker": "SPEAKER_01",
             "words": [word("hi", 6.5, 7.0, "SPEAKER_01"), word("and", 7.1, 9.0, "SPEAKER_01"),
                       word("after", 9.1, 11.0, "SPEAKER_00"), word("pause", 11.1, 12.0, "SPEAKER_00")]},
            # A long pause splits a turn even with the same speaker; no aligned words keeps the segment whole.
            {"start": 20.0, "end": 25.0, "text": "unaligned", "speaker": "SPEAKER_00", "words": []},
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
    assert turns(db_path) == [
        ("c1:0000", 0.5, 6.0, "hello there 42", "SPEAKER_00"),
        ("c1:0001", 6.5, 9.0, "hi and", "SPEAKER_01"),
        ("c1:0002", 9.1, 12.0, "after pause", "SPEAKER_00"),
        ("c1:0003", 20.0, 25.0, "unaligned", "SPEAKER_00"),
    ]


def test_a_transcript_of_a_different_wav_is_never_used(tmp_path):
    db_path, transcripts, _ = setup(tmp_path, wav_sha256="sha-after-more-audio-arrived")
    counts = build_turns(db_path, transcripts)
    assert counts["pending"] == 1 and counts["turns"] == 0
    assert turns(db_path) == []


def test_older_segment_labels_and_newer_span_labels_both_land_on_turns(tmp_path):
    db_path, transcripts, labels = setup(tmp_path)
    build_turns(db_path, transcripts)
    (labels / "labels.jsonl").write_text(
        json.dumps({"segment_id": "omi-a", "label": "owner"}) + "\n"
        + json.dumps({"turns": [{"turn_id": "gone", "conversation_id": "c1", "start": 19.5, "end": 25.5}],
                      "label": "not_owner"}) + "\n"
    )
    (labels / "tags.jsonl").write_text(
        # Omi segment omi-b covers most of turn 1 and all of turn 2.
        json.dumps({"type": "name", "segment_ids": ["omi-b"], "name": "Bob"}) + "\n"
        + json.dumps({"type": "name", "segment_ids": ["no-longer-exists"], "name": "Nobody"}) + "\n"
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
    ]
