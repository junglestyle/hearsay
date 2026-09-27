"""Group not-owner segments into anonymous speakers, and apply the operator's names.

Clusters are derived and recomputed on every reprocess, so their ids are not
stable. Names are not stored on clusters: they are the operator's durable
input, recorded on specific segments (labels/tags.jsonl). A cluster takes the
name of its tagged segments, which makes naming retroactive, and giving two
clusters the same name merges them into one person.
"""

import json
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
DROP TABLE IF EXISTS segment_people;
-- One row per segment labeled not_owner.
CREATE TABLE segment_people (
    payload_path TEXT NOT NULL,
    idx INTEGER NOT NULL,
    segment_id TEXT NOT NULL,
    cluster TEXT NOT NULL,           -- smallest segment_id in the cluster; changes as data grows
    person TEXT,                     -- operator's name for the cluster, NULL if unnamed or conflicting
    name_conflict INTEGER NOT NULL,  -- 1 if the cluster's tagged segments carry different names
    PRIMARY KEY (payload_path, idx)
);
"""


def parse_tags(text: str) -> tuple[dict[str, str], list[list[str]]]:
    """Names by segment_id (latest wins), and the segment groups marked mixed."""
    names, mixed = {}, []
    for line in text.splitlines():
        record = json.loads(line)
        if record["type"] == "name":
            for segment_id in record["segment_ids"]:
                names[segment_id] = record["name"]
        elif record["type"] == "mixed":
            mixed.append(record["segment_ids"])
    return names, mixed


def cluster_name(segment_ids, names: dict[str, str]) -> tuple[str | None, bool]:
    """A cluster's person, and whether its tagged segments disagree."""
    tagged = {names[s] for s in segment_ids if s in names}
    return (next(iter(tagged)) if len(tagged) == 1 else None), len(tagged) > 1


def cluster(vectors) -> list[int]:
    # Imported here: only the worker image and dev extras have scipy.
    import numpy as np
    from scipy.cluster.hierarchy import fcluster, linkage

    if len(vectors) < 2:
        return [1] * len(vectors)
    tree = linkage(np.array(vectors), method="average", metric="cosine")
    return list(fcluster(tree, t=1 - CLUSTER_THRESHOLD, criterion="distance"))


def group_people(db_path: Path, labels_dir: Path) -> dict:
    tags_path = labels_dir / "tags.jsonl"
    names, _ = parse_tags(tags_path.read_text() if tags_path.exists() else "")
    db = sqlite3.connect(db_path)
    try:
        rows = db.execute(
            "SELECT payload_path, idx, segment_id, embedding FROM segment_speakers"
            " WHERE label = 'not_owner' ORDER BY segment_id, payload_path, idx"
        ).fetchall()
        assignments = cluster([list(array("f", r[3])) for r in rows])

        members = {}
        for row, c in zip(rows, assignments):
            members.setdefault(c, []).append(row)

        out = []
        for group in members.values():
            cluster_id = min(r[2] for r in group)
            person, conflict = cluster_name([r[2] for r in group], names)
            out += [(r[0], r[1], r[2], cluster_id, person, int(conflict)) for r in group]

        db.executescript(SCHEMA)
        db.executemany("INSERT INTO segment_people VALUES (?,?,?,?,?,?)", sorted(out))
        db.commit()
    finally:
        db.close()

    clusters = {r[3] for r in out}
    return {
        "segments": len(out),
        "clusters": len(clusters),
        "named_clusters": len({r[3] for r in out if r[4]}),
        "people": len({r[4] for r in out if r[4]}),
        "conflicts": len({r[3] for r in out if r[5]}),
    }
