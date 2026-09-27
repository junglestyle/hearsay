import json
import random
import sqlite3
from array import array

import pytest

pytest.importorskip("scipy")

from hearsay.people import group_people  # noqa: E402
from hearsay.speakers import SCHEMA as SPEAKERS_SCHEMA  # noqa: E402


def unit(vector):
    norm = sum(x * x for x in vector) ** 0.5
    return [x / norm for x in vector]


def test_names_on_a_few_segments_apply_to_whole_clusters(tmp_path):
    rng = random.Random(0)
    voices = {v: unit([rng.gauss(0, 1) for _ in range(192)]) for v in ["alice", "bob", "carol1", "carol2", "me"]}

    def near(voice):
        return unit([x + rng.gauss(0, 0.04) for x in voices[voice]])

    # (segment_id, conversation, voice, label); Alice appears in two conversations,
    # and Carol sounds different enough in two settings to form two clusters.
    segments = [(f"a{i}", "c1" if i < 4 else "c2", "alice", "not_owner") for i in range(7)]
    segments += [(f"b{i}", "c1", "bob", "not_owner") for i in range(3)]
    segments += [(f"c{i}", "c2", "carol1", "not_owner") for i in range(3)]
    segments += [(f"d{i}", "c3", "carol2", "not_owner") for i in range(3)]
    segments += [("m0", "c1", "me", "owner")]

    db_path, labels_dir = tmp_path / "h.sqlite", tmp_path / "labels"
    labels_dir.mkdir()
    db = sqlite3.connect(db_path)
    db.executescript(SPEAKERS_SCHEMA)
    for idx, (segment_id, conversation, voice, label) in enumerate(segments):
        db.execute(
            "INSERT INTO segment_speakers VALUES (?,?,?,?,?,?,?,?)",
            ("memory/p.body", idx, segment_id, conversation, 1.0, array("f", near(voice)).tobytes(), 0.0, label),
        )
    db.commit()
    db.close()

    tags = [
        {"type": "name", "segment_ids": ["a0"], "name": "Alice"},  # one tag, from c1 only
        {"type": "name", "segment_ids": ["b0"], "name": "Bob"},
        {"type": "name", "segment_ids": ["b1"], "name": "Robert"},  # conflicting names
        {"type": "name", "segment_ids": ["c0"], "name": "Carol"},
        {"type": "name", "segment_ids": ["d2"], "name": "Carol"},  # same name merges clusters
        {"type": "name", "segment_ids": ["gone"], "name": "Nobody"},  # segment no longer exists
    ]
    (labels_dir / "tags.jsonl").write_text("".join(json.dumps(t) + "\n" for t in tags))

    counts = group_people(db_path, labels_dir)

    db = sqlite3.connect(db_path)
    rows = db.execute("SELECT segment_id, cluster, person, name_conflict FROM segment_people").fetchall()
    first = db.execute("SELECT * FROM segment_people ORDER BY payload_path, idx").fetchall()
    db.close()
    person = {segment_id: p for segment_id, _, p, _ in rows}
    conflict = {segment_id: c for segment_id, _, _, c in rows}

    assert "m0" not in person
    assert {person[f"a{i}"] for i in range(7)} == {"Alice"}
    assert {person[f"b{i}"] for i in range(3)} == {None}
    assert {conflict[f"b{i}"] for i in range(3)} == {1}
    assert {person[s] for s in ["c0", "c1", "c2", "d0", "d1", "d2"]} == {"Carol"}
    assert counts == {"segments": 16, "clusters": 4, "named_clusters": 3, "people": 2, "conflicts": 1}

    group_people(db_path, labels_dir)
    db = sqlite3.connect(db_path)
    assert db.execute("SELECT * FROM segment_people ORDER BY payload_path, idx").fetchall() == first
    db.close()
