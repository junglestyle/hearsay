"""Speaker embeddings per turn, and owner / not-owner labels.

Reads the database (after turns are built), the raw audio stream, and the
operator's enrollment windows in the labels dir. Turn audio is cut from the
stream at the conversation's zero point, which is where its WAV starts, so
turn coverage is exact.

Tuned for precision: a turn is labeled by voice only when its similarity to
the owner's enrolled voice is clearly high or clearly low. A turn left
unlabeled (too short to embed, or in between) then takes the label of its
diarized speaker in the same conversation, when that speaker's voice-labeled
turns agree (DIARIZATION_AGREEMENT), unless it inherits not_owner yet
sounds more like the owner than like that speaker; otherwise it stays
unlabeled.
"""

import json
import sqlite3
from array import array
from pathlib import Path

from hearsay.assemble import cut, place_bursts, timestamp
from hearsay.cache import Cache, audio_key

# Thresholds tuned on 2026-09-27 against 176 ear-labeled Omi segments
# (`python -m hearsay.label report`). Nearly all misses were under 2.5 s, and
# the cutoff kept 87% of speech time. At these values, labeled segments of
# at least 2.5 s scored precision 1.00 on both sides (owner recall 0.93 over
# 64 predictions, not-owner recall 0.90 over 26). Owner sits above 0.30, the
# lowest value tested, for margin. Re-check them on WhisperX turns, whose
# boundaries differ from Omi's.
MIN_TURN = 2.5
# Fraction of a turn that must be received audio, not filled silence.
MIN_COVERAGE = 0.8
# Enrollment audio is embedded in pieces this long, then averaged.
ENROLLMENT_PIECE = 3.0
# Cosine similarity to the owner's enrolled voice.
OWNER_THRESHOLD = 0.40
NOT_OWNER_THRESHOLD = 0.14
# Share of a diarized speaker's voice-labeled seconds that must carry one
# label before its unlabeled turns take it. On real data (2026-10-01), 51 of
# 53 diarized speakers with labeled turns were at least 90% one label, and
# inheriting cut unlabeled speech from 18% to 2%. Checked by ear on 155
# inherited turns, 86% were right; nearly every error was a short interjection
# the diarizer filed under the other person, from speakers that agree 99-100%,
# so a higher bar doesn't help. Vetoing an inherited not_owner turn that
# sounds more like the owner than like its speaker raised that to 91%.
DIARIZATION_AGREEMENT = 0.9

SCHEMA = """
DROP TABLE IF EXISTS turn_speakers;
DROP TABLE IF EXISTS owner_enrollment;
-- One row per turn.
CREATE TABLE turn_speakers (
    turn_id TEXT PRIMARY KEY REFERENCES turns(turn_id),
    conversation_id TEXT NOT NULL,
    coverage REAL NOT NULL,          -- fraction of the turn with received audio
    embedding BLOB,                  -- float32 x 192, NULL if too short or too little audio
    owner_similarity REAL,           -- cosine similarity to the enrolled owner voice
    label TEXT,                      -- owner | not_owner | NULL (ambiguous or unknown)
    basis TEXT                       -- voice | diarization (inherited) | NULL when unlabeled
);
CREATE TABLE owner_enrollment (
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    coverage REAL NOT NULL,
    pieces INTEGER NOT NULL          -- ENROLLMENT_PIECE-second pieces embedded
);
"""


def load_model(model_dir: Path):
    # Imported here: torch is heavy, and only this step needs it.
    from speechbrain.inference.speaker import EncoderClassifier

    # pretrained_path defaults to the Hugging Face repo; point it at the baked
    # snapshot so loading never touches the network.
    return EncoderClassifier.from_hparams(
        source=str(model_dir),
        savedir=str(model_dir),
        overrides={"pretrained_path": str(model_dir)},
        run_opts={"device": "cpu"},
    )


# Cache key prefix: the speaker model that produced a cached embedding.
EMBED_VERSION = "ecapa-voxceleb-0f99f2d0"


def embed(model, pcm: bytes, cache: Cache | None = None) -> list[float]:
    """Unit-length embedding; with a cache, audio embedded before isn't embedded again."""
    import torch

    key = audio_key(EMBED_VERSION, pcm)
    cached = cache.get(key) if cache else None
    if cached is not None:
        return list(array("f", cached))
    samples = torch.frombuffer(bytearray(pcm), dtype=torch.int16).float() / 32768.0
    vector = model.encode_batch(samples.unsqueeze(0)).squeeze()
    # Stored as float32 either way, so a cached result equals a fresh one exactly.
    packed = array("f", torch.nn.functional.normalize(vector, dim=0).tolist())
    if cache:
        cache.put(key, packed.tobytes())
    return list(packed)


def cosine(a: list[float], b: list[float]) -> float:
    # Both are unit length.
    return sum(x * y for x, y in zip(a, b))


def owner_voice(model, raw_dir: Path, bursts: list, labels_dir: Path, db: sqlite3.Connection,
                cache: Cache | None = None):
    """Mean embedding of the enrollment recordings, or None if there are none."""
    path = labels_dir / "enrollment.json"
    if not path.exists():
        return None
    vectors = []
    for window in json.loads(path.read_text()):
        start, end = timestamp(window["start"]), timestamp(window["end"])
        _, coverage = cut(raw_dir, bursts, start, end - start)
        used = 0
        for k in range(int((end - start) // ENROLLMENT_PIECE)):
            pcm, piece_coverage = cut(raw_dir, bursts, start + k * ENROLLMENT_PIECE, ENROLLMENT_PIECE)
            if piece_coverage >= MIN_COVERAGE:
                vectors.append(embed(model, pcm, cache))
                used += 1
        db.execute("INSERT INTO owner_enrollment VALUES (?,?,?,?)", (window["start"], window["end"], coverage, used))
    if not vectors:
        raise ValueError(f"{path}: no usable audio in the enrollment windows")
    return mean_unit(vectors)


def label_for(similarity: float) -> str | None:
    if similarity >= OWNER_THRESHOLD:
        return "owner"
    if similarity <= NOT_OWNER_THRESHOLD:
        return "not_owner"
    return None


def label_speakers(raw_dir: Path, db_path: Path, labels_dir: Path, model_dir: Path,
                   cache: Cache | None = None) -> dict:
    model = load_model(model_dir)
    db = sqlite3.connect(db_path)
    try:
        db.executescript(SCHEMA)
        bursts = place_bursts(db)
        owner = owner_voice(model, raw_dir, bursts, labels_dir, db, cache)

        turns = db.execute(
            "SELECT t.turn_id, t.conversation_id, ca.zero_at, t.start, t.end"
            " FROM turns t JOIN conversation_audio ca ON ca.conversation_id = t.conversation_id"
            " ORDER BY ca.zero_at, t.idx"
        ).fetchall()
        counts = {"turns": len(turns), "embedded": 0, "owner": 0, "not_owner": 0}
        # Turns too short to label by voice, embedded anyway for the veto in
        # inherit_labels. Kept out of the table: a short turn's embedding is
        # too noisy to label or cluster with.
        short = {}
        for turn_id, conversation_id, zero_at, start, end in turns:
            duration = max(0.0, end - start)
            pcm, coverage = cut(raw_dir, bursts, timestamp(zero_at) + start, duration) if duration else (b"", 0.0)
            embedding = similarity = label = None
            if duration >= MIN_TURN and coverage >= MIN_COVERAGE:
                vector = embed(model, pcm, cache)
                embedding = array("f", vector).tobytes()
                counts["embedded"] += 1
                if owner is not None:
                    similarity = cosine(vector, owner)
                    label = label_for(similarity)
                    if label:
                        counts[label] += 1
            elif duration > 0 and coverage >= MIN_COVERAGE:
                short[turn_id] = embed(model, pcm, cache)
            db.execute(
                "INSERT INTO turn_speakers VALUES (?,?,?,?,?,?,?)",
                (turn_id, conversation_id, coverage, embedding, similarity, label, "voice" if label else None),
            )
        counts.update(inherit_labels(db, owner, short))
        db.commit()
    finally:
        db.close()
    counts["enrolled"] = owner is not None
    return counts


def inherit_labels(db: sqlite3.Connection, owner: list[float] | None, short: dict[str, list[float]]) -> dict:
    """Label unlabeled turns from their diarized speaker's voice-labeled turns.

    Within a conversation the diarizer tells voices apart well, even on turns
    too short to embed reliably; it's the embedding that says whose voice it
    is. A speaker whose labeled turns disagree passes nothing on.

    The diarizer's usual slip is filing a short interjection ("yeah") under
    the other person. So a turn that would inherit not_owner but sounds more
    like the owner than like its own speaker (`short` holds the embeddings of
    turns too short for the table) keeps no label.
    """
    rows = db.execute(
        "SELECT ts.turn_id, t.conversation_id, t.diar_speaker, t.end - t.start, ts.label, ts.embedding"
        " FROM turn_speakers ts JOIN turns t ON t.turn_id = ts.turn_id WHERE t.diar_speaker IS NOT NULL"
    ).fetchall()
    seconds, vectors = {}, {}
    for _, conversation_id, speaker, duration, label, embedding in rows:
        if label:
            by_label = seconds.setdefault((conversation_id, speaker), {})
            by_label[label] = by_label.get(label, 0.0) + duration
            vectors.setdefault((conversation_id, speaker), []).append(list(array("f", embedding)))
    agreed = {}
    for key, by_label in seconds.items():
        label, top = max(by_label.items(), key=lambda item: item[1])
        if top >= DIARIZATION_AGREEMENT * sum(by_label.values()):
            agreed[key] = label
    centroids = {key: mean_unit(vs) for key, vs in vectors.items()}

    inherited, vetoed = [], 0
    for turn_id, c, s, _, label, embedding in rows:
        if label or (c, s) not in agreed:
            continue
        vector = short.get(turn_id) or (list(array("f", embedding)) if embedding else None)
        if agreed[(c, s)] == "not_owner" and owner is not None and vector is not None \
                and cosine(vector, owner) > cosine(vector, centroids[(c, s)]):
            vetoed += 1
            continue
        inherited.append((agreed[(c, s)], turn_id))
    db.executemany("UPDATE turn_speakers SET label = ?, basis = 'diarization' WHERE turn_id = ?", inherited)
    return {"inherited_owner": sum(1 for label, _ in inherited if label == "owner"),
            "inherited_not_owner": sum(1 for label, _ in inherited if label == "not_owner"),
            "not_inherited_sounds_like_owner": vetoed}


def mean_unit(vectors: list[list[float]]) -> list[float]:
    mean = [sum(column) / len(vectors) for column in zip(*vectors)]
    norm = sum(x * x for x in mean) ** 0.5
    return [x / norm for x in mean]
