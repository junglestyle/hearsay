import json
import random
import sqlite3
from array import array

import pytest

pytest.importorskip("scipy")

from hearsay.assemble import SCHEMA as ASSEMBLE_SCHEMA  # noqa: E402
from hearsay.parse import SCHEMA as PARSE_SCHEMA  # noqa: E402
from hearsay.people import group_people  # noqa: E402
from hearsay.speakers import SCHEMA as SPEAKERS_SCHEMA  # noqa: E402
from hearsay.turns import SCHEMA as TURNS_SCHEMA, record_operator_input  # noqa: E402


def unit(vector):
    norm = sum(x * x for x in vector) ** 0.5
    return [x / norm for x in vector]


def span(turn_id, conversation, start):
    return {"turn_id": turn_id, "conversation_id": conversation, "start": start, "end": start + 4}


def test_names_on_a_few_turns_apply_to_whole_clusters(tmp_path):
    rng = random.Random(0)
    voices = {v: unit([rng.gauss(0, 1) for _ in range(192)]) for v in ["alice", "bob", "carol1", "carol2", "me"]}

    def near(voice):
        return unit([x + rng.gauss(0, 0.04) for x in voices[voice]])

    # (turn_id, conversation, voice, label); Alice appears in two conversations,
    # and Carol sounds different enough in two settings to form two clusters.
    turns = [(f"a{i}", "c1" if i < 4 else "c2", "alice", "not_owner") for i in range(7)]
    turns += [(f"b{i}", "c1", "bob", "not_owner") for i in range(3)]
    turns += [(f"c{i}", "c2", "carol1", "not_owner") for i in range(3)]
    turns += [(f"d{i}", "c3", "carol2", "not_owner") for i in range(3)]
    turns += [("m0", "c1", "me", "owner")]
    # One diarized speaker in c4: a clear Alice turn and one too noisy to place alone.
    turns += [("s0", "c4", "alice", "not_owner"), ("s1", "c4", "noise", "not_owner")]
    voices["noise"] = unit([rng.gauss(0, 1) for _ in range(192)])
    diarized = {"s0": "SPEAKER_00", "s1": "SPEAKER_00"}
    start = {turn_id: 5.0 * i for i, (turn_id, *_) in enumerate(turns)}

    db_path, labels_dir = tmp_path / "h.sqlite", tmp_path / "labels"
    labels_dir.mkdir()
    db = sqlite3.connect(db_path)
    db.executescript(PARSE_SCHEMA + ASSEMBLE_SCHEMA + TURNS_SCHEMA + SPEAKERS_SCHEMA)
    for idx, (turn_id, conversation, voice, label) in enumerate(turns):
        db.execute("INSERT INTO turns VALUES (?,?,?,?,?,?,?,?)",
                   (turn_id, conversation, idx, start[turn_id], start[turn_id] + 4, "synthetic",
                    diarized.get(turn_id), None))
        db.execute("INSERT INTO turn_speakers VALUES (?,?,?,?,?,?)",
                   (turn_id, conversation, 1.0, array("f", near(voice)).tobytes(), 0.0, label))
    db.commit()
    db.close()

    tags = [
        {"type": "name", "turns": [span("a0", "c1", start["a0"])], "name": "Alice"},  # one tag, from c1 only
        {"type": "name", "turns": [span("b0", "c1", start["b0"])], "name": "Bob"},
        {"type": "name", "turns": [span("b1", "c1", start["b1"])], "name": "Robert"},  # conflicting names
        {"type": "name", "turns": [span("c0", "c2", start["c0"])], "name": "Carol"},
        {"type": "name", "turns": [span("d2", "c3", start["d2"])], "name": "Carol"},  # same name merges clusters
        {"type": "name", "turns": [span("gone", "c9", 0.0)], "name": "Nobody"},  # nothing there any more
    ]
    (labels_dir / "tags.jsonl").write_text("".join(json.dumps(t) + "\n" for t in tags))

    record_operator_input(db_path, labels_dir)
    counts = group_people(db_path)

    db = sqlite3.connect(db_path)
    rows = db.execute("SELECT turn_id, cluster, person, name_conflict FROM turn_people").fetchall()
    first = db.execute("SELECT * FROM turn_people ORDER BY turn_id").fetchall()
    db.close()
    person = {turn_id: p for turn_id, _, p, _ in rows}
    conflict = {turn_id: c for turn_id, _, _, c in rows}

    assert "m0" not in person
    assert {person[f"a{i}"] for i in range(7)} == {"Alice"}
    assert person["s0"] == person["s1"] == "Alice"  # the speaker's turns cluster together
    assert {person[f"b{i}"] for i in range(3)} == {None}
    assert {conflict[f"b{i}"] for i in range(3)} == {1}
    assert {person[t] for t in ["c0", "c1", "c2", "d0", "d1", "d2"]} == {"Carol"}
    assert counts == {"turns": 18, "clusters": 4, "named_clusters": 3, "people": 2, "conflicts": 1}

    group_people(db_path)
    db = sqlite3.connect(db_path)
    assert db.execute("SELECT * FROM turn_people ORDER BY turn_id").fetchall() == first
    db.close()
