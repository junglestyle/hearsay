import sqlite3
from array import array

from hearsay.speakers import SCHEMA as SPEAKERS_SCHEMA, inherit_labels
from hearsay.turns import SCHEMA as TURNS_SCHEMA

# Three orthogonal voices stand in for embeddings.
ME, ANA, BOB = [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]


def test_unlabeled_turns_take_their_diarized_speakers_label_only_when_it_agrees():
    db = sqlite3.connect(":memory:")
    db.executescript(TURNS_SCHEMA + SPEAKERS_SCHEMA)
    # (turn, conversation, diarized speaker, seconds, label by voice, voice)
    turns = [
        # S0 is the owner by voice; its short turn follows.
        ("t0", "c1", "S0", 10, "owner", ME), ("t1", "c1", "S0", 1, None, ME),
        # S1 is mostly Ana; one turn under 10% doesn't count against it.
        ("t2", "c1", "S1", 19, "not_owner", ANA), ("t3", "c1", "S1", 1, "owner", ME),
        ("t4", "c1", "S1", 2, None, ANA),
        # The diarizer's usual slip: the owner's "yeah" filed under Ana.
        ("t10", "c1", "S1", 1, None, ME),
        # S2 mixes the two: its short turn stays unknown.
        ("t5", "c1", "S2", 6, "owner", ME), ("t6", "c1", "S2", 4, "not_owner", BOB), ("t7", "c1", "S2", 1, None, BOB),
        # The same speaker id in another conversation is someone else entirely.
        ("t8", "c2", "S0", 1, None, ME),
        # No diarized speaker: nothing to go on.
        ("t9", "c1", None, 1, None, BOB),
    ]
    short = {}
    for idx, (turn_id, conversation, speaker, seconds, label, voice) in enumerate(turns):
        db.execute("INSERT INTO turns VALUES (?,?,?,0,?,'',?,NULL)", (turn_id, conversation, idx, seconds, speaker))
        embedding = array("f", voice).tobytes() if label else None
        if not label:
            short[turn_id] = voice
        db.execute("INSERT INTO turn_speakers VALUES (?,?,1,?,NULL,?,?)",
                   (turn_id, conversation, embedding, label, "voice" if label else None))

    assert inherit_labels(db, ME, short) == {"inherited_owner": 1, "inherited_not_owner": 1,
                                             "not_inherited_sounds_like_owner": 1}
    got = {t: (label, basis) for t, label, basis in db.execute("SELECT turn_id, label, basis FROM turn_speakers")}
    assert got["t1"] == ("owner", "diarization")
    assert got["t4"] == ("not_owner", "diarization")
    assert got["t3"] == ("owner", "voice")  # voice labels are never overridden
    assert got["t10"] == got["t7"] == got["t8"] == got["t9"] == (None, None)
