"""A hint for the operator: clusters that sound like the media they've tagged.

Speaker embeddings carry the channel as well as the voice, and dialogue from
a TV through its speaker sounds unlike people in the room. On every reprocess
a logistic regression learns that from the operator's own tags: turns tagged
_media against turns of named people, _stranger and the owner by ear. Each
embedded turn gets a score, and the portal offers "_media" for one tap on a
cluster whose turns average HINT or more. It never tags anything itself: a
wrong hint costs a tap, a wrong tag would drop real speech from the stream.

Measured 2026-10-07, leave one conversation out: 5 of 8 media clusters at or
above HINT, none of 21 named people's (the most media-like 0.47). Off-the-shelf
sound-event models (AudioSet's Music, Television and Radio classes, CLAP)
barely beat chance on the same tags, because the media here is dialogue. All
of it was one TV at home, so the hint spreads elsewhere only as the operator
tags media elsewhere.
"""

import sqlite3
from pathlib import Path

# Mean score a cluster needs for the hint.
HINT = 0.8
# Tagged turns of each kind needed before there's anything to learn from.
MIN_TRAINING = 20

SCHEMA = """
DROP TABLE IF EXISTS turn_media;
-- How much each embedded turn sounds like the operator's _media tags, 0-1.
CREATE TABLE turn_media (
    turn_id TEXT PRIMARY KEY REFERENCES turns(turn_id),
    score REAL NOT NULL
);
"""


def train(x, y, steps: int = 300, rate: float = 0.5, l2: float = 1e-2):
    """Logistic regression by gradient descent: deterministic, numpy only."""
    import numpy as np

    w, b = np.zeros(x.shape[1]), 0.0
    for _ in range(steps):
        error = 1 / (1 + np.exp(-(x @ w + b))) - y
        w -= rate * (x.T @ error / len(y) + l2 * w)
        b -= rate * error.mean()
    return w, b


def score_media(db_path: Path) -> dict:
    import numpy as np

    db = sqlite3.connect(db_path)
    try:
        rows = db.execute(
            "SELECT ts.turn_id, ts.embedding,"
            " (SELECT value FROM operator_input oi WHERE oi.turn_id = ts.turn_id AND oi.kind IN ('name', 'clip')"
            "  ORDER BY oi.kind = 'clip' DESC LIMIT 1),"
            " EXISTS (SELECT 1 FROM operator_input oi WHERE oi.turn_id = ts.turn_id AND oi.kind = 'label' AND oi.value = 'owner')"
            " FROM turn_speakers ts WHERE ts.embedding IS NOT NULL ORDER BY ts.turn_id"
        ).fetchall()
        # 1 media, 0 someone present (a named person, a stranger, the owner), -1 not a tag to learn from.
        y = np.array([1 if name == "_media" else 0 if (name and not name.startswith("_")) or name == "_stranger" or owner
                      else -1 for _, _, name, owner in rows])
        db.executescript(SCHEMA)
        if (y == 1).sum() < MIN_TRAINING or (y == 0).sum() < MIN_TRAINING:
            db.commit()
            return {"scored": 0, "media_tags": int((y == 1).sum()), "people_tags": int((y == 0).sum())}
        x = np.array([np.frombuffer(embedding, dtype="<f4") for _, embedding, _, _ in rows], dtype=float)
        x = (x - x.mean(0)) / (x.std(0) + 1e-6)
        w, b = train(x[y >= 0], y[y >= 0].astype(float))
        scores = 1 / (1 + np.exp(-(x @ w + b)))
        db.executemany("INSERT INTO turn_media VALUES (?,?)",
                       [(turn_id, round(float(s), 3)) for (turn_id, _, _, _), s in zip(rows, scores)])
        db.commit()
    finally:
        db.close()
    return {"scored": len(rows), "media_tags": int((y == 1).sum()), "people_tags": int((y == 0).sum()),
            "media_like_turns": int((scores >= HINT).sum())}
