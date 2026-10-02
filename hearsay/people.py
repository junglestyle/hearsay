"""Group not-owner turns into anonymous speakers, and apply the operator's names.

Clusters are derived and recomputed on every reprocess, so their ids are not
stable. Names are not stored on clusters: they are the operator's durable
input (labels/tags.jsonl), matched to turns by time (hearsay/turns.py). A
cluster takes the name of its tagged turns, which makes naming retroactive,
and giving two clusters the same name merges them into one person. Different
names keep speakers apart, and marking a cluster mixed splits the diarized
speakers under the marked turns in two.
"""

import sqlite3
from array import array
from pathlib import Path

from hearsay.speakers import mean_unit

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
-- One row per turn labeled not_owner, by voice, by channel or inherited by diarization.
CREATE TABLE turn_people (
    turn_id TEXT PRIMARY KEY REFERENCES turns(turn_id),
    cluster TEXT NOT NULL,           -- smallest turn_id in the cluster; changes as data grows
    person TEXT,                     -- operator's name for the cluster, NULL if unnamed or conflicting
    name_conflict INTEGER NOT NULL,  -- 1 if the cluster's tagged turns carry different names
    split INTEGER NOT NULL           -- 1 if its diarized speaker was split in two voices (marked mixed)
);
"""


def cluster_name(turn_ids, names: dict[str, str]) -> tuple[str | None, bool]:
    """A cluster's person, and whether its tagged turns disagree."""
    tagged = {names[t] for t in turn_ids if t in names}
    return (next(iter(tagged)) if len(tagged) == 1 else None), len(tagged) > 1


def cluster(vectors, names: list[set[str]]) -> list[int]:
    """Average-linkage clusters of unit vectors, as one label per vector.

    The operator's names are constraints: vectors whose turns carry different
    names never end up in one cluster, however alike they sound. Without
    them, two people a few untagged speakers bridge get merged (on real data,
    2026-10-01, two named women joined through an hour of untagged speech).
    """
    # Imported here: only the worker image and dev extras have numpy.
    import numpy as np

    n = len(vectors)
    v = np.array(vectors)
    sim = v @ v.T
    size = np.ones(n)
    names = [set(x) for x in names]
    label = list(range(n))
    np.fill_diagonal(sim, -np.inf)

    def block(i):
        for j in range(n):
            if names[i] and names[j] and names[i] != names[j]:
                sim[i, j] = sim[j, i] = -np.inf

    for i in range(n):
        block(i)
    while n > 1:
        i, j = np.unravel_index(np.argmax(sim), sim.shape)
        if sim[i, j] < CLUSTER_THRESHOLD:
            break
        # Average linkage: the merged cluster's similarity to each other one
        # is the size-weighted mean of its halves'. A blocked or merged-away
        # pair stays -inf.
        sim[i] = sim[:, i] = (sim[i] * size[i] + sim[j] * size[j]) / (size[i] + size[j])
        sim[i, i] = -np.inf
        sim[j] = sim[:, j] = -np.inf
        size[i] += size[j]
        names[i] |= names[j]
        label = [i if c == j else c for c in label]
        block(i)
    return label


def two_voices(vectors) -> list[int]:
    """Each turn's half when one diarized speaker is split in two by voice.

    Two-means on cosine, started from the turn least like the speaker's
    average and the turn least like that one, so it is deterministic.
    """
    import numpy as np

    v = np.array(vectors)
    first = int(np.argmin(v @ v.mean(axis=0)))
    centers = v[[first, int(np.argmin(v @ v[first]))]]
    side = None
    for _ in range(50):
        new = list(np.argmax(v @ centers.T, axis=1))
        if new == side or len(set(new)) < 2:
            break
        side = new
        centers = np.array([v[np.array(side) == k].mean(axis=0) for k in (0, 1)])
        centers /= np.linalg.norm(centers, axis=1, keepdims=True)
    return [int(k) for k in side] if side else [0] * len(v)


def speaker_vectors(rows, mixed: set[str]) -> tuple[list[list[str]], list[list[float]], list[tuple]]:
    """One averaged, unit-length embedding per diarized speaker per conversation.

    A speaker's turns together embed far more steadily than any single short
    turn. Turns whose words got no diarized speaker stand alone. A speaker
    the operator heard more than one person in (a turn marked mixed) is split
    in two voices: the diarizer can give two people one label, and clustering
    can't take a speaker apart. Halves, not single turns: on real data single
    turns of the second voice scattered over five clusters.

    Also returns each vector's speaker, (conversation_id, diar_speaker, half),
    half None for an unsplit speaker.
    """
    groups = {}
    for turn_id, conversation_id, diar_speaker, embedding in rows:
        key = (conversation_id, diar_speaker) if diar_speaker else (conversation_id, turn_id)
        groups.setdefault(key, []).append((turn_id, list(array("f", embedding))))
    members, vectors, speakers = [], [], []
    for key in sorted(groups, key=lambda k: (k[0], k[1] or "")):
        group = groups[key]
        split = len(group) > 1 and any(turn_id in mixed for turn_id, _ in group)
        sides = two_voices([v for _, v in group]) if split else [None] * len(group)
        for half in sorted(set(sides), key=lambda h: (h is None, h)):
            part = [(turn_id, v) for (turn_id, v), side in zip(group, sides) if side == half]
            members.append([turn_id for turn_id, _ in part])
            vectors.append(mean_unit([v for _, v in part]))
            speakers.append((*key, half))
    return members, vectors, speakers


def group_people(db_path: Path) -> dict:
    db = sqlite3.connect(db_path)
    try:
        names = dict(db.execute("SELECT turn_id, value FROM operator_input WHERE kind = 'name'"))
        mixed = {t for (t,) in db.execute("SELECT turn_id FROM operator_input WHERE kind = 'mixed'")}
        # Only turns labeled not_owner by voice or by a Mac recording's
        # channel: the owner's voice never joins a cluster, even when
        # diarization lumps it in with someone else's.
        rows = db.execute(
            "SELECT ts.turn_id, t.conversation_id, t.diar_speaker, ts.embedding FROM turn_speakers ts"
            " JOIN turns t ON t.turn_id = ts.turn_id WHERE ts.label = 'not_owner'"
            " AND ts.basis IN ('voice', 'channel') AND ts.embedding IS NOT NULL"
            " ORDER BY ts.turn_id"
        ).fetchall()
        groups, vectors, speakers = speaker_vectors(rows, mixed)
        # A half the operator hasn't named yet is a voice they said was
        # someone else: it may join other unnamed speakers but no named
        # person until they name it, so it comes back to be named. On real
        # data the half that was a new person otherwise joined a bartender.
        tagged = []
        for i, (turn_ids, (_, _, half)) in enumerate(zip(groups, speakers)):
            found = {names[t] for t in turn_ids if t in names}
            tagged.append(found if found or half is None else {f"unnamed half {i}"})
        labels = cluster(vectors, tagged)

        members = {}
        split = set()
        speaker_cluster = {}
        for turn_ids, (conversation_id, diar_speaker, half), c in zip(groups, speakers, labels):
            members.setdefault(c, []).extend(turn_ids)
            if half is not None:
                split.update(turn_ids)
                split.add((conversation_id, diar_speaker))
            # A split speaker's inherited turns go with its larger half: like
            # any inherited turn, a guess from the speaker, not the voice.
            best = speaker_cluster.get((conversation_id, diar_speaker))
            if best is None or len(turn_ids) > best[1]:
                speaker_cluster[(conversation_id, diar_speaker)] = (c, len(turn_ids))
        # Names come from the clustered turns only: a tag's time span can
        # catch a short inherited turn the diarizer put under someone else.
        named_by = {c: list(turn_ids) for c, turn_ids in members.items()}
        # Turns that inherited not_owner from their diarized speaker, and
        # those labeled by channel but too short to embed, join that
        # speaker's cluster, and so its name.
        for turn_id, conversation_id, diar_speaker in db.execute(
            "SELECT ts.turn_id, t.conversation_id, t.diar_speaker FROM turn_speakers ts"
            " JOIN turns t ON t.turn_id = ts.turn_id WHERE ts.label = 'not_owner'"
            " AND (ts.basis = 'diarization' OR (ts.basis = 'channel' AND ts.embedding IS NULL))"
        ):
            found = speaker_cluster.get((conversation_id, diar_speaker))
            if found is not None:
                members[found[0]].append(turn_id)
                if (conversation_id, diar_speaker) in split:
                    split.add(turn_id)

        out = []
        for c, turn_ids in members.items():
            person, conflict = cluster_name(named_by[c], names)
            out += [(t, min(turn_ids), person, int(conflict), int(t in split)) for t in turn_ids]

        db.executescript(SCHEMA)
        db.executemany("INSERT INTO turn_people VALUES (?,?,?,?,?)", sorted(out))
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
