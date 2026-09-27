"""Speaker embeddings per segment, and owner / not-owner labels.

Reads the database (after assemble), the raw audio stream, and the operator's
durable inputs in the labels dir. Segment audio is cut from the stream with
the conversation's zero point, so segment coverage is exact.

Tuned for precision: a segment is labeled only when its similarity to the
owner's enrolled voice is clearly high or clearly low. Everything in between,
and anything too short or missing audio, stays unlabeled.
"""

import json
import sqlite3
from array import array
from pathlib import Path

from hearsay.assemble import cut, place_bursts, timestamp

# Embeddings from shorter audio are unreliable.
MIN_SEGMENT = 1.0
# Fraction of a segment that must be received audio, not filled silence.
MIN_COVERAGE = 0.8
# Enrollment audio is embedded in pieces this long, then averaged.
ENROLLMENT_PIECE = 3.0
# Cosine similarity to the owner's voice. Provisional until tuned against
# operator labels with `python -m hearsay.label report`.
OWNER_THRESHOLD = 0.6
NOT_OWNER_THRESHOLD = 0.3

SCHEMA = """
DROP TABLE IF EXISTS segment_speakers;
DROP TABLE IF EXISTS owner_enrollment;
-- One row per segment of each conversation's latest memory payload.
CREATE TABLE segment_speakers (
    payload_path TEXT NOT NULL,
    idx INTEGER NOT NULL,
    segment_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    coverage REAL NOT NULL,          -- fraction of the segment with received audio
    embedding BLOB,                  -- float32 x 192, NULL if too short or too little audio
    owner_similarity REAL,           -- cosine similarity to the enrolled owner voice
    label TEXT,                      -- owner | not_owner | NULL (ambiguous or unknown)
    PRIMARY KEY (payload_path, idx),
    FOREIGN KEY (payload_path, idx) REFERENCES segments(payload_path, idx)
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


def embed(model, pcm: bytes) -> list[float]:
    import torch

    samples = torch.frombuffer(bytearray(pcm), dtype=torch.int16).float() / 32768.0
    vector = model.encode_batch(samples.unsqueeze(0)).squeeze()
    return torch.nn.functional.normalize(vector, dim=0).tolist()


def cosine(a: list[float], b: list[float]) -> float:
    # Both are unit length.
    return sum(x * y for x, y in zip(a, b))


def owner_voice(model, raw_dir: Path, bursts: list, labels_dir: Path, db: sqlite3.Connection):
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
                vectors.append(embed(model, pcm))
                used += 1
        db.execute("INSERT INTO owner_enrollment VALUES (?,?,?,?)", (window["start"], window["end"], coverage, used))
    if not vectors:
        raise ValueError(f"{path}: no usable audio in the enrollment windows")
    mean = [sum(column) / len(vectors) for column in zip(*vectors)]
    norm = sum(x * x for x in mean) ** 0.5
    return [x / norm for x in mean]


def label_for(similarity: float) -> str | None:
    if similarity >= OWNER_THRESHOLD:
        return "owner"
    if similarity <= NOT_OWNER_THRESHOLD:
        return "not_owner"
    return None


def label_speakers(raw_dir: Path, db_path: Path, labels_dir: Path, model_dir: Path) -> dict:
    model = load_model(model_dir)
    db = sqlite3.connect(db_path)
    try:
        db.executescript(SCHEMA)
        bursts = place_bursts(db)
        owner = owner_voice(model, raw_dir, bursts, labels_dir, db)

        segments = db.execute(
            "SELECT s.payload_path, s.idx, s.segment_id, ca.conversation_id, ca.zero_at, s.start, s.end"
            " FROM conversation_audio ca JOIN segments s ON s.payload_path = ca.memory_path"
            " ORDER BY ca.zero_at, s.idx"
        ).fetchall()
        counts = {"segments": len(segments), "embedded": 0, "owner": 0, "not_owner": 0}
        for payload_path, idx, segment_id, conversation_id, zero_at, start, end in segments:
            duration = max(0.0, end - start)
            pcm, coverage = cut(raw_dir, bursts, timestamp(zero_at) + start, duration) if duration else (b"", 0.0)
            embedding = similarity = label = None
            if duration >= MIN_SEGMENT and coverage >= MIN_COVERAGE:
                vector = embed(model, pcm)
                embedding = array("f", vector).tobytes()
                counts["embedded"] += 1
                if owner is not None:
                    similarity = cosine(vector, owner)
                    label = label_for(similarity)
                    if label:
                        counts[label] += 1
            db.execute(
                "INSERT INTO segment_speakers VALUES (?,?,?,?,?,?,?,?)",
                (payload_path, idx, segment_id, conversation_id, coverage, embedding, similarity, label),
            )
        db.commit()
    finally:
        db.close()
    counts["enrolled"] = owner is not None
    return counts
