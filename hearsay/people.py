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

# Average-linkage cosine similarity at which two groups are the same speaker.
# Provisional starting point (2026-09-27, 307 real not-owner segments): 0.45
# left 70 singletons in 107 clusters; 0.40 gives 81 clusters, the largest
# 100/36/18. Tune with `python -m hearsay.label report` as recurring people
# appear: many "mixed" clusters, raise it; one person over many clusters, lower it.
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


def group_people(db_path: Path) -> dict:
    db = sqlite3.connect(db_path)
    try:
        names = dict(db.execute("SELECT turn_id, value FROM operator_input WHERE kind = 'name'"))
        rows = db.execute(
            "SELECT turn_id, embedding FROM turn_speakers WHERE label = 'not_owner' ORDER BY turn_id"
        ).fetchall()
        assignments = cluster([list(array("f", embedding)) for _, embedding in rows])

        members = {}
        for (turn_id, _), c in zip(rows, assignments):
            members.setdefault(c, []).append(turn_id)

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
