import json
import random
import sqlite3
from array import array

import pytest

pytest.importorskip("numpy")

from hearsay.assemble import SCHEMA as ASSEMBLE_SCHEMA  # noqa: E402
from hearsay.parse import SCHEMA as PARSE_SCHEMA  # noqa: E402
from hearsay.people import group_people  # noqa: E402
from hearsay.speakers import SCHEMA as SPEAKERS_SCHEMA  # noqa: E402
from hearsay.turns import SCHEMA as TURNS_SCHEMA, record_operator_input, wall  # noqa: E402

ZERO_AT = "2026-01-01T00:00:00+00:00"


def unit(vector):
    norm = sum(x * x for x in vector) ** 0.5
    return [x / norm for x in vector]


def at(start):
    """A tag's absolute span over the 4 s turn starting at start."""
    return [wall(ZERO_AT, start, start + 4)]


def test_names_on_a_few_turns_apply_to_whole_clusters(tmp_path):
    rng = random.Random(0)
    voices = {v: unit([rng.gauss(0, 1) for _ in range(192)])
              for v in ["alice", "bob", "carol1", "carol2", "me", "dave", "erin", "fay"]}

    def near(voice):
        return unit([x + rng.gauss(0, 0.04) for x in voices[voice]])

    # (turn_id, conversation, voice, label); Alice appears in two conversations,
    # and Carol sounds different enough in two settings to form two clusters.
    turns = [(f"a{i}", "c1" if i < 4 else "c2", "alice", "not_owner") for i in range(7)]
    turns += [(f"b{i}", "c1", "bob", "not_owner") for i in range(3)]
    turns += [(f"c{i}", "c2", "carol1", "not_owner") for i in range(3)]
    turns += [(f"d{i}", "c3", "carol2", "not_owner") for i in range(3)]
    turns += [("m0", "c1", "me", "owner")]
    # One diarized speaker in c4: a clear Alice turn, one too noisy to place
    # alone, and one too short to embed that inherited not_owner from them.
    turns += [("s0", "c4", "alice", "not_owner"), ("s1", "c4", "noise", "not_owner"),
              ("s2", "c4", None, "not_owner")]
    voices["noise"] = unit([rng.gauss(0, 1) for _ in range(192)])
    # Two people the diarizer gave one label in c5, which the operator heard
    # and marked mixed: clustering alone can't take them apart.
    turns += [(f"e{i}", "c5", "dave" if i < 4 else "erin", "not_owner") for i in range(7)]
    # Fay once had a turn tagged _noise; her other conversation stays hers.
    turns += [("f0", "c2", "fay", "not_owner"), ("f1", "c3", "fay", "not_owner"), ("f2", "c3", "fay", "not_owner")]
    diarized = {"s0": "SPEAKER_00", "s1": "SPEAKER_00", "s2": "SPEAKER_00"}
    diarized |= {f"e{i}": "SPEAKER_01" for i in range(7)}
    start = {turn_id: 5.0 * i for i, (turn_id, *_) in enumerate(turns)}

    db_path, labels_dir = tmp_path / "h.sqlite", tmp_path / "labels"
    labels_dir.mkdir()
    db = sqlite3.connect(db_path)
    db.executescript(PARSE_SCHEMA + ASSEMBLE_SCHEMA + TURNS_SCHEMA + SPEAKERS_SCHEMA)
    for conversation in ("c1", "c2", "c3", "c4", "c5"):
        db.execute("INSERT INTO conversation_audio VALUES (?, ?, 600, 1, NULL, NULL)", (conversation, ZERO_AT))
    for idx, (turn_id, conversation, voice, label) in enumerate(turns):
        db.execute("INSERT INTO turns VALUES (?,?,?,?,?,?,?,?)",
                   (turn_id, conversation, idx, start[turn_id], start[turn_id] + 4, "synthetic",
                    diarized.get(turn_id), None))
        embedding = array("f", near(voice)).tobytes() if voice else None
        db.execute("INSERT INTO turn_speakers VALUES (?,?,?,?,?,?,?)",
                   (turn_id, conversation, 1.0, embedding, 0.0, label, "voice" if voice else "diarization"))
    db.commit()
    db.close()

    tags = [
        {"type": "name", "at": at(start["a0"]), "name": "Alice"},  # one tag, from c1 only
        # A tag that caught the inherited turn: it doesn't name (or conflict) the cluster.
        {"type": "name", "at": at(start["s2"]), "name": "Bob"},
        {"type": "name", "at": at(start["b0"]), "name": "Bob"},
        # Different names keep even one voice apart: the operator's ear wins.
        {"type": "name", "at": at(start["b1"]), "name": "Robert"},
        {"type": "name", "at": at(start["c0"]), "name": "Carol"},
        {"type": "name", "at": at(start["d2"]), "name": "Carol"},  # same name merges clusters
        {"type": "name", "at": at(9_000.0), "name": "Nobody"},  # nothing there any more
        {"type": "mixed", "at": at(start["e5"])},
        {"type": "name", "at": at(start["f0"]), "name": "_noise"},
        {"type": "name", "at": at(start["e0"]), "name": "Dave"},
    ]
    (labels_dir / "tags.jsonl").write_text("".join(json.dumps(t) + "\n" for t in tags))

    record_operator_input(db_path, labels_dir)
    counts = group_people(db_path)

    db = sqlite3.connect(db_path)
    rows = db.execute("SELECT turn_id, cluster, person, name_conflict FROM turn_people").fetchall()
    first = db.execute("SELECT * FROM turn_people ORDER BY turn_id").fetchall()
    db.close()
    person = {turn_id: p for turn_id, _, p, _ in rows}
    cluster = {turn_id: c for turn_id, c, _, _ in rows}
    conflict = {turn_id: c for turn_id, _, _, c in rows}

    assert "m0" not in person
    assert {person[f"a{i}"] for i in range(7)} == {"Alice"}
    assert person["s0"] == person["s1"] == "Alice"  # the speaker's turns cluster together
    assert person["s2"] == "Alice"  # and the inherited turn joins them
    assert (person["b0"], person["b1"]) == ("Bob", "Robert")
    assert not any(conflict.values())
    assert {person[t] for t in ["c0", "c1", "c2", "d0", "d1", "d2"]} == {"Carol"}
    assert {person[f"e{i}"] for i in range(4)} == {"Dave"}
    # Erin is her own, unnamed speaker, ready to be named.
    assert {person[f"e{i}"] for i in range(4, 7)} == {None}
    assert len({cluster[f"e{i}"] for i in range(4, 7)}) == 1
    # A category takes only what was tagged with it, never a speaker by resemblance.
    assert person["f0"] == "_noise"
    assert person["f1"] is person["f2"] is None
    assert counts == {"turns": 29, "clusters": 9, "named_clusters": 7, "people": 6, "conflicts": 0}

    group_people(db_path)
    db = sqlite3.connect(db_path)
    assert db.execute("SELECT * FROM turn_people ORDER BY turn_id").fetchall() == first
    db.close()
