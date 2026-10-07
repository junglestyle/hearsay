import random
import sqlite3
from array import array

import pytest

pytest.importorskip("numpy")

from hearsay.media import HINT, SCHEMA, score_media  # noqa: E402
from hearsay.speakers import SCHEMA as SPEAKERS_SCHEMA  # noqa: E402
from hearsay.turns import SCHEMA as TURNS_SCHEMA  # noqa: E402


def unit(v):
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v]


def build(tmp_path, tagged):
    """A TV's voices and room voices, `tagged` of each tagged by the operator, 10 of each untagged."""
    rng = random.Random(1)
    tv, room = unit([rng.gauss(0, 1) for _ in range(192)]), unit([rng.gauss(0, 1) for _ in range(192)])
    db_path = tmp_path / "h.sqlite"
    db = sqlite3.connect(db_path)
    db.executescript(TURNS_SCHEMA + SPEAKERS_SCHEMA + SCHEMA)
    for kind, centre in (("tv", tv), ("room", room)):
        for i in range(tagged + 10):
            turn_id = f"{kind}{i}"
            vector = unit([c + rng.gauss(0, 0.06) for c in centre])
            db.execute("INSERT INTO turn_speakers VALUES (?, 'c1', 1, ?, 0, 'not_owner', 'voice')",
                       (turn_id, array("f", vector).tobytes()))
            if i < tagged:
                db.execute("INSERT INTO operator_input VALUES (?, 'name', ?)", (turn_id, "_media" if kind == "tv" else f"Person {i % 4}"))
    db.commit()
    db.close()
    return db_path


def test_untagged_turns_that_sound_like_tagged_media_score_as_media(tmp_path):
    db_path = build(tmp_path, tagged=25)
    score_media(db_path)
    db = sqlite3.connect(db_path)
    scores = dict(db.execute("SELECT turn_id, score FROM turn_media"))
    db.close()
    assert all(scores[f"tv{i}"] >= HINT for i in range(25, 35))  # the untagged ones
    assert all(scores[f"room{i}"] < HINT for i in range(25, 35))


def test_too_few_tags_give_no_hints(tmp_path):
    db_path = build(tmp_path, tagged=5)
    assert score_media(db_path)["scored"] == 0
    db = sqlite3.connect(db_path)
    assert db.execute("SELECT count(*) FROM turn_media").fetchone()[0] == 0
    db.close()
