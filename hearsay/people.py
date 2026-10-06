"""Group not-owner turns into anonymous speakers, and apply the operator's names.

Clusters are derived and recomputed on every reprocess, so their ids are not
stable. Names are not stored on clusters: they are the operator's durable
input (labels/tags.jsonl), matched to turns by time (hearsay/turns.py). A
cluster takes the name of its tagged turns, which makes naming retroactive,
and giving two clusters the same name merges them into one person. Different
names keep speakers apart, a diarized speaker that holds several voices is
split into them, and marking a cluster mixed splits the diarized speakers
under the marked turns into at least two.
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

# Speech a voice needs, when a speaker marked mixed is split, to count as a
# person rather than scraps of one.
MIN_VOICE_SECONDS = 60

SCHEMA = """
DROP TABLE IF EXISTS turn_people;
-- One row per turn labeled not_owner, by voice, by channel or inherited by diarization.
CREATE TABLE turn_people (
    turn_id TEXT PRIMARY KEY REFERENCES turns(turn_id),
    cluster TEXT NOT NULL,           -- smallest turn_id in the cluster; changes as data grows
    person TEXT,                     -- operator's name for the cluster, NULL if unnamed or conflicting
    name_conflict INTEGER NOT NULL,  -- 1 if the cluster's tagged turns carry different names
    split INTEGER NOT NULL           -- 1 if its diarized speaker was split in voices
);
"""


def cluster_name(turn_ids, names: dict[str, str]) -> tuple[str | None, bool]:
    """A cluster's person, and whether its tagged turns disagree."""
    tagged = {names[t] for t in turn_ids if t in names}
    return (next(iter(tagged)) if len(tagged) == 1 else None), len(tagged) > 1


def cluster(vectors, names: list[set[str]]) -> list[int]:
    """Average-linkage clusters of unit vectors, as one label per vector.

    The operator's names are constraints: vectors whose turns carry different
    names never end up in one cluster, however alike they sound, and a
    category joins nothing untagged. Without
    them, two people a few untagged speakers bridge get merged (on real data,
    2026-10-01, two named women joined through an hour of untagged speech).
    """
    # Imported here: only the worker image and dev extras have numpy.
    import numpy as np

    n = len(vectors)
    if n == 0:
        # Nothing labeled not_owner by voice yet, e.g. before enrollment.
        return []
    v = np.array(vectors)
    sim = v @ v.T
    size = np.ones(n)
    names = [set(x) for x in names]
    label = list(range(n))
    np.fill_diagonal(sim, -np.inf)

    def category(x):
        return any(name.startswith("_") for name in x)

    def block(i):
        for j in range(n):
            # A category (_noise, _media, _stranger) is many voices, not one,
            # so its average resembles plenty of real speakers: it takes only
            # what the operator tagged. Once a two-turn _noise cluster grew to
            # 794 turns, real conversation the stream then left out.
            differ = names[i] != names[j]
            if differ and ((names[i] and names[j]) or category(names[i]) or category(names[j])):
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


def voices(vectors, seconds: list[float], marked: bool) -> list[int] | None:
    """Each turn's voice in a diarized speaker, or None if it is one voice.

    As many voices as its turns form when clustered among themselves, with at
    least MIN_VOICE_SECONDS of speech each, and at least two when the
    operator marked it mixed; then each turn goes to the nearest voice, until
    that settles. In a noisy restaurant the diarizer gave four people one
    label; split this way they came out as three voices, the fourth (a
    waiter) too brief. On real data (2026-10-01) no speaker of 3+ minutes
    split that was one person (26, the owner's and every named person's), and
    3 of 4 known lumps did, so it runs on every speaker, marked or not.
    """
    import numpy as np

    v = np.array(vectors)
    groups = {}
    for i, c in enumerate(cluster(v, [set()] * len(v))):
        groups.setdefault(c, []).append(i)
    found = [g for g in groups.values() if sum(seconds[i] for i in g) >= MIN_VOICE_SECONDS]
    if len(found) >= 2:
        centers = np.array([v[g].mean(axis=0) for g in found])
    elif not marked:
        return None
    else:
        # Started from the turn least like the speaker's average and the turn
        # least like that one, so it is deterministic.
        first = int(np.argmin(v @ v.mean(axis=0)))
        centers = v[[first, int(np.argmin(v @ v[first]))]]
    side = None
    for _ in range(50):
        new = [int(k) for k in np.argmax(v @ centers.T, axis=1)]
        if new == side:
            break
        side = new
        centers = np.array([v[np.array(side) == k].mean(axis=0) if k in side else centers[k]
                            for k in range(len(centers))])
        centers /= np.linalg.norm(centers, axis=1, keepdims=True)
    return side


def speaker_vectors(rows, mixed: set[tuple[str, str]]) -> tuple[list[list[str]], list[list[float]], list[tuple]]:
    """One averaged, unit-length embedding per diarized speaker per conversation.

    A speaker's turns together embed far more steadily than any single short
    turn. Turns whose words got no diarized speaker stand alone. A speaker
    whose turns form several voices, or that the operator heard more than one
    person in (`mixed`), is split in voices: the diarizer can give several
    people one label, and clustering can't take a speaker apart. Voices, not single turns: on real
    data single turns of a second person scattered over five clusters.

    Also returns each vector's speaker, (conversation_id, diar_speaker, voice),
    voice None for an unsplit speaker.
    """
    groups = {}
    for turn_id, conversation_id, diar_speaker, seconds, embedding in rows:
        key = (conversation_id, diar_speaker) if diar_speaker else (conversation_id, turn_id)
        groups.setdefault(key, []).append((turn_id, seconds, list(array("f", embedding))))
    members, vectors, speakers = [], [], []
    for key in sorted(groups, key=lambda k: (k[0], k[1] or "")):
        group = groups[key]
        sides = voices([v for _, _, v in group], [s for _, s, _ in group], key in mixed) if len(group) > 1 else None
        sides = sides or [None] * len(group)
        for voice in sorted(set(sides), key=lambda h: (h is None, h)):
            part = [(turn_id, v) for (turn_id, _, v), side in zip(group, sides) if side == voice]
            members.append([turn_id for turn_id, _ in part])
            vectors.append(mean_unit([v for _, v in part]))
            speakers.append((*key, voice))
    return members, vectors, speakers


def group_people(db_path: Path) -> dict:
    db = sqlite3.connect(db_path)
    try:
        names = dict(db.execute("SELECT turn_id, value FROM operator_input WHERE kind = 'name'"))
        # A clip the operator tagged on its own in the portal is what was heard
        # in that one turn, whatever its diarized speaker: a cough or a video
        # in someone's speaker, or a second person. It stays out of the
        # speaker's voice and goes to the stream as tagged.
        clips = dict(db.execute("SELECT turn_id, value FROM operator_input WHERE kind = 'clip'"))
        # Any turn marked mixed, inherited ones too, marks its whole diarized speaker.
        mixed = set(db.execute(
            "SELECT t.conversation_id, t.diar_speaker FROM operator_input oi JOIN turns t ON t.turn_id = oi.turn_id"
            " WHERE oi.kind = 'mixed' AND t.diar_speaker IS NOT NULL"
        ))
        # Only turns labeled not_owner by voice or by a Mac recording's
        # channel: the owner's voice never joins a cluster, even when
        # diarization lumps it in with someone else's.
        rows = db.execute(
            "SELECT ts.turn_id, t.conversation_id, t.diar_speaker, t.end - t.start, ts.embedding FROM turn_speakers ts"
            " JOIN turns t ON t.turn_id = ts.turn_id WHERE ts.label = 'not_owner'"
            " AND ts.basis IN ('voice', 'channel') AND ts.embedding IS NOT NULL"
            " ORDER BY ts.turn_id"
        ).fetchall()
        rows = [r for r in rows if r[0] not in clips]
        groups, vectors, speakers = speaker_vectors(rows, mixed)
        # A voice split off a speaker that the operator hasn't named yet may
        # join other unnamed speakers but no named person until they name
        # it, so it comes back to be named. On real data (noisy rooms on a
        # phone, where different people score 0.4-0.6) split voices otherwise
        # joined the wrong people: a dinner's three voices all one woman, a
        # new man a bartender.
        tagged = []
        for i, (turn_ids, (_, _, voice)) in enumerate(zip(groups, speakers)):
            found = {names[t] for t in turn_ids if t in names}
            tagged.append(found if found or voice is None else {f"unnamed voice {i}"})
        labels = cluster(vectors, tagged)

        members = {}
        split = set()
        speaker_cluster = {}
        for turn_ids, (conversation_id, diar_speaker, voice), c in zip(groups, speakers, labels):
            members.setdefault(c, []).extend(turn_ids)
            if voice is not None:
                split.update(turn_ids)
                split.add((conversation_id, diar_speaker))
            # A split speaker's inherited turns go with its largest voice: like
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
            if turn_id in clips:
                continue
            found = speaker_cluster.get((conversation_id, diar_speaker))
            if found is not None:
                members[found[0]].append(turn_id)
                if (conversation_id, diar_speaker) in split:
                    split.add(turn_id)

        out = []
        for c, turn_ids in members.items():
            person, conflict = cluster_name(named_by[c], names)
            out += [(t, min(turn_ids), person, int(conflict), int(t in split)) for t in turn_ids]
        out += [(t, t, clips[t], 0, 0) for (t,) in db.execute("SELECT turn_id FROM turn_speakers WHERE label = 'not_owner'")
                if t in clips]

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
