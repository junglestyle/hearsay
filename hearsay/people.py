"""Group not-owner turns into anonymous speakers, and apply the operator's names.

Clusters are derived and recomputed on every reprocess, so their ids are not
stable. Names are not stored on clusters: they are the operator's durable
input (labels/tags.jsonl), matched to turns by time (hearsay/turns.py). A
cluster takes the name of its tagged turns, which makes naming retroactive,
and giving two clusters the same name merges them into one person.
"""

import sqlite3
from array import array
from pathlib import Path

# Average-linkage cosine similarity at which two speakers are the same person.
# Clustering whole diarized speakers (one averaged embedding per speaker per
# conversation), not single turns: on 662 real not-owner turns (2026-09-28)
# per-turn clustering at 0.40 gave 189 clusters with named people split three
# ways; per-speaker gave 21, each named person in one, and no clusters mixing
# names. Tune with `python -m hearsay.label report`: many "mixed" clusters,
# raise it; one person over many clusters, lower it.
CLUSTER_THRESHOLD = 0.40

SCHEMA = """
DROP TABLE IF EXISTS turn_people;
-- One row per turn labeled not_owner.
CREATE TABLE turn_people (
    turn_id TEXT PRIMARY KEY REFERENCES turns(turn_id),
    cluster TEXT NOT NULL,           -- smallest turn_id in the cluster; changes as data grows
    person TEXT,                     -- operator's name for the cluster, NULL if unnamed or conflicting
    name_conflict INTEGER NOT NULL   -- 1 if the cluster's tagged turns carry different names
);
"""


def cluster_name(turn_ids, names: dict[str, str]) -> tuple[str | None, bool]:
    """A cluster's person, and whether its tagged turns disagree."""
    tagged = {names[t] for t in turn_ids if t in names}
    return (next(iter(tagged)) if len(tagged) == 1 else None), len(tagged) > 1


def cluster(vectors) -> list[int]:
    # Imported here: only the worker image and dev extras have scipy.
    import numpy as np
    from scipy.cluster.hierarchy import fcluster, linkage

    if len(vectors) < 2:
        return [1] * len(vectors)
    tree = linkage(np.array(vectors), method="average", metric="cosine")
    return list(fcluster(tree, t=1 - CLUSTER_THRESHOLD, criterion="distance"))


def speaker_vectors(rows) -> tuple[list[list[str]], list[list[float]]]:
    """One averaged, unit-length embedding per diarized speaker per conversation.

    A speaker's turns together embed far more steadily than any single short
    turn. Turns whose words got no diarized speaker stand alone.
    """
    groups = {}
    for turn_id, conversation_id, diar_speaker, embedding in rows:
        key = (conversation_id, diar_speaker) if diar_speaker else (conversation_id, turn_id)
        groups.setdefault(key, []).append((turn_id, list(array("f", embedding))))
    members, vectors = [], []
    for key in sorted(groups, key=lambda k: (k[0], k[1] or "")):
        group = groups[key]
        mean = [sum(column) / len(group) for column in zip(*(v for _, v in group))]
        norm = sum(x * x for x in mean) ** 0.5
        members.append([turn_id for turn_id, _ in group])
        vectors.append([x / norm for x in mean])
    return members, vectors


def group_people(db_path: Path) -> dict:
    db = sqlite3.connect(db_path)
    try:
        names = dict(db.execute("SELECT turn_id, value FROM operator_input WHERE kind = 'name'"))
        # Only turns already labeled not_owner: the owner's voice never joins a
        # cluster, even when diarization lumps it in with someone else's.
        rows = db.execute(
            "SELECT ts.turn_id, t.conversation_id, t.diar_speaker, ts.embedding FROM turn_speakers ts"
            " JOIN turns t ON t.turn_id = ts.turn_id WHERE ts.label = 'not_owner' ORDER BY ts.turn_id"
        ).fetchall()
        speakers, vectors = speaker_vectors(rows)

        members = {}
        for turn_ids, c in zip(speakers, cluster(vectors)):
            members.setdefault(c, []).extend(turn_ids)

        out = []
        for turn_ids in members.values():
            person, conflict = cluster_name(turn_ids, names)
            out += [(t, min(turn_ids), person, int(conflict)) for t in turn_ids]

        db.executescript(SCHEMA)
        db.executemany("INSERT INTO turn_people VALUES (?,?,?,?)", sorted(out))
        db.commit()
    finally:
        db.close()

    return {
        "turns": len(out),
        "clusters": len({r[1] for r in out}),
        "named_clusters": len({r[1] for r in out if r[2]}),
        "people": len({r[2] for r in out if r[2]}),
        "conflicts": len({r[1] for r in out if r[3]}),
    }
