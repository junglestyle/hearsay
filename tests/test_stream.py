import json
import sqlite3

from hearsay.assemble import SCHEMA as ASSEMBLE_SCHEMA
from hearsay.conversations import SCHEMA as CONVERSATIONS_SCHEMA
from hearsay.parse import SCHEMA as PARSE_SCHEMA
from hearsay.people import SCHEMA as PEOPLE_SCHEMA
from hearsay.speakers import SCHEMA as SPEAKERS_SCHEMA
from hearsay.stream import write_stream
from hearsay.turns import SCHEMA as TURNS_SCHEMA

T = 1_790_000_000.0  # unix seconds; conversation c1's WAV starts here

# (text, diarized voice, owner/not_owner label, owner similarity, person, cluster);
# a label is by voice unless the text says inherited.
TURNS = [
    ("mine", "S0", "owner", 0.71, None, None),
    ("scott's", "S1", "not_owner", 0.05, "Scott", "k1"),
    ("first anon", "S2", "not_owner", 0.08, None, "k2"),
    ("second anon", "S3", "not_owner", 0.02, None, "k3"),
    ("first anon again", "S2", "not_owner", 0.09, None, "k2"),
    ("a cashier", "S4", "not_owner", 0.11, "_stranger", "k4"),
    ("a car alarm", None, "not_owner", 0.01, "_noise", "k5"),
    ("too short to tell", "S0", None, 0.25, None, None),
    ("yeah (inherited)", "S2", "not_owner", None, None, "k2"),
]


def build(tmp_path):
    db_path = tmp_path / "h.sqlite"
    db = sqlite3.connect(db_path)
    db.executescript(PARSE_SCHEMA + ASSEMBLE_SCHEMA + CONVERSATIONS_SCHEMA + TURNS_SCHEMA + SPEAKERS_SCHEMA + PEOPLE_SCHEMA)
    db.execute("INSERT INTO conversations VALUES ('c1', ?, ?, 60, 0)", (T, T + 100))
    db.execute("INSERT INTO conversations VALUES ('c2', ?, ?, 40, 1)", (T + 500, T + 560))  # not transcribed yet
    db.execute("INSERT INTO conversation_audio VALUES ('c1', '2026-09-21T14:13:20+00:00', 100, 1, 'c1.wav', 'sha')")
    for idx, (text, voice, label, similarity, person, cluster) in enumerate(TURNS):
        turn_id = f"c1:{idx:04d}"
        db.execute("INSERT INTO turns VALUES (?, 'c1', ?, ?, ?, ?, ?, 0.8)", (turn_id, idx, 10.0 * idx, 10.0 * idx + 5, text, voice))
        basis = "diarization" if "inherited" in text else "voice" if label else None
        db.execute("INSERT INTO turn_speakers VALUES (?, 'c1', 1, NULL, ?, ?, ?)", (turn_id, similarity, label, basis))
        if cluster:
            db.execute("INSERT INTO turn_people VALUES (?, ?, ?, 0)", (turn_id, cluster, person))
    db.commit()
    db.close()
    return db_path


def read(stream_dir):
    index = json.loads((stream_dir / "index.json").read_text())["conversations"]
    lines = (stream_dir / "conversations/c1.jsonl").read_text().splitlines()
    return index, [json.loads(line) for line in lines]


def test_stream_attributes_speakers_and_leaves_out_non_speech(tmp_path):
    db_path, stream_dir = build(tmp_path), tmp_path / "stream"
    assert write_stream(db_path, stream_dir) == {"conversations": 2, "transcribed": 1, "utterances": 8}
    index, utterances = read(stream_dir)

    assert [(u["text"], u["speaker"]["kind"], u["speaker"]["name"], u["speaker"]["label"]) for u in utterances] == [
        ("mine", "owner", None, None),
        ("scott's", "person", "Scott", None),
        ("first anon", "anonymous", None, "anon A"),
        ("second anon", "anonymous", None, "anon B"),
        ("first anon again", "anonymous", None, "anon A"),
        ("a cashier", "stranger", None, "stranger A"),
        ("too short to tell", "unknown", None, None),
        ("yeah (inherited)", "anonymous", None, "anon A"),
    ]
    assert utterances[0]["start"] == "2026-09-21T14:13:20.000Z" and utterances[1]["end"] == "2026-09-21T14:13:35.000Z"
    assert utterances[0]["speaker_confidence"] == {"basis": "voice", "owner_similarity": 0.71}
    assert utterances[7]["speaker_confidence"] == {"basis": "diarization", "owner_similarity": None}
    assert [(e["conversation_id"], e["transcribed"], e["open"], e["utterances"]) for e in index] == [
        ("c1", True, False, 8), ("c2", False, True, 0)]


def test_revisions_change_only_when_a_conversation_does(tmp_path):
    db_path, stream_dir = build(tmp_path), tmp_path / "stream"
    write_stream(db_path, stream_dir)
    assert json.loads((stream_dir / "index.json").read_text())["format_version"] == 1
    index, utterances = read(stream_dir)
    first, transcript = index[0]["revision"], index[0]["transcript_revision"]
    ids = [u["utterance_id"] for u in utterances]
    write_stream(db_path, stream_dir)
    assert read(stream_dir)[0][0]["revision"] == first

    # Naming someone applies to past turns, so the conversation's revision
    # changes, but the transcript and so the utterance ids don't.
    db = sqlite3.connect(db_path)
    db.execute("UPDATE turn_people SET person = 'Ana' WHERE cluster = 'k2'")
    db.commit()
    write_stream(db_path, stream_dir)
    index, utterances = read(stream_dir)
    assert index[0]["revision"] != first
    assert index[0]["transcript_revision"] == transcript
    assert [u["utterance_id"] for u in utterances] == ids
    assert utterances[2]["speaker"] == {"kind": "person", "name": "Ana", "label": None}

    # Re-transcription: the same ids now name different speech.
    db.execute("UPDATE turns SET end = end + 2, text = 'mine, longer' WHERE idx = 0")
    db.commit()
    write_stream(db_path, stream_dir)
    assert read(stream_dir)[0][0]["transcript_revision"] != transcript

    # A conversation that no longer exists loses its file.
    db.execute("DELETE FROM conversations WHERE conversation_id = 'c1'")
    db.execute("DELETE FROM turns")
    db.commit()
    db.close()
    write_stream(db_path, stream_dir)
    assert list((stream_dir / "conversations").iterdir()) == []


def test_forgotten_list_exists_and_is_never_rewritten(tmp_path):
    db_path, stream_dir = build(tmp_path), tmp_path / "stream"
    write_stream(db_path, stream_dir)
    assert json.loads((stream_dir / "forgotten.json").read_text()) == []

    entry = {"forgotten_at": "2026-10-01T12:00:00.000Z", "start": "2026-09-21T14:13:20.000Z",
             "end": "2026-09-21T14:13:25.000Z", "conversation_id": "c1", "utterance_ids": ["c1:0000"],
             "reason": "asked"}
    (stream_dir / "forgotten.json").write_text(json.dumps([entry]))
    db = sqlite3.connect(db_path)
    db.execute("DELETE FROM conversations")
    db.execute("DELETE FROM turns")
    db.commit()
    db.close()
    write_stream(db_path, stream_dir)
    assert json.loads((stream_dir / "forgotten.json").read_text()) == [entry]
