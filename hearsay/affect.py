"""The owner's tone of voice, per turn: how a listener would hear it.

audeering's wav2vec2 tuned on MSP-Podcast (natural speech, scored by listeners
for how the speaker sounded) gives arousal, dominance and valence, about 0-1.
Like owner_similarity, a property of the audio, not of what was said.

Owner turns only, labeled by voice or by a Mac recording's channel, at least
MIN_TURN long with MIN_COVERAGE of real audio, cut the way speaker embeddings
are: other people's emotional state is not inferred, and a turn that only
inherited the owner's label might be someone else.

Only arousal reaches the stream. Checked blind 2026-10-07: the operator rated
30 of their turns calm, neutral or heated without seeing scores, and arousal
ranked them alike (Spearman 0.74; 62 of 63 calm-heated pairs the right way).
Dominance is kept here but not streamed: it follows arousal for every voice
measured (r 0.91-0.97, the owner's and four other people's), so it adds
nothing. Valence waits for its own check (tone without words, and a blind
rating): models like this one take some of it from the words.

The model's license is CC-BY-NC-SA 4.0: fine for personal use, not commercial.
"""

import sqlite3
from pathlib import Path

from hearsay.assemble import cut, place_bursts, timestamp
from hearsay.cache import Cache, audio_key
from hearsay.speakers import MIN_COVERAGE, MIN_TURN

# Cache key prefix: the model revision that produced a cached score.
AFFECT_VERSION = "affect-msp-dim-6eba34a2"
# Turns are scored on at most their first 30 s.
MAX_SECONDS = 30

SCHEMA = """
DROP TABLE IF EXISTS turn_affect;
-- How the owner sounded in each of their turns, about 0-1 (hearsay/affect.py).
CREATE TABLE turn_affect (
    turn_id TEXT PRIMARY KEY REFERENCES turns(turn_id),
    arousal REAL NOT NULL,           -- calm to animated; checked by ear, streamed
    dominance REAL NOT NULL,         -- follows arousal; not streamed
    valence REAL NOT NULL            -- unpleasant to pleasant; not yet checked, not streamed
);
"""


def load_model(model_dir: Path):
    # Imported here: torch and transformers are heavy, and only this step needs them.
    import torch
    from torch import nn
    from transformers import Wav2Vec2FeatureExtractor
    from transformers.models.wav2vec2.modeling_wav2vec2 import Wav2Vec2Model, Wav2Vec2PreTrainedModel

    class RegressionHead(nn.Module):
        def __init__(self, config):
            super().__init__()
            self.dense = nn.Linear(config.hidden_size, config.hidden_size)
            self.dropout = nn.Dropout(config.final_dropout)
            self.out_proj = nn.Linear(config.hidden_size, config.num_labels)

        def forward(self, x):
            return self.out_proj(self.dropout(torch.tanh(self.dense(self.dropout(x)))))

    class EmotionModel(Wav2Vec2PreTrainedModel):
        """As the model card defines it: mean-pooled wav2vec2 states into a 3-way regression."""

        def __init__(self, config):
            super().__init__(config)
            self.wav2vec2 = Wav2Vec2Model(config)
            self.classifier = RegressionHead(config)
            self.init_weights()

        def forward(self, input_values):
            return self.classifier(self.wav2vec2(input_values)[0].mean(dim=1))

    return (Wav2Vec2FeatureExtractor.from_pretrained(str(model_dir)),
            EmotionModel.from_pretrained(str(model_dir)).eval())


def score(model, pcm: bytes, cache: Cache | None = None) -> tuple[float, float, float]:
    """(arousal, dominance, valence); with a cache, audio scored before isn't scored again."""
    import numpy as np
    import torch

    key = audio_key(AFFECT_VERSION, pcm)
    cached = cache.get(key) if cache else None
    if cached is not None:
        return tuple(np.frombuffer(cached, dtype="<f4").tolist())
    extractor, network = model
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
    with torch.inference_mode():
        inputs = extractor(samples, sampling_rate=16000, return_tensors="pt").input_values
        values = np.array(network(inputs)[0].tolist(), dtype="<f4")
    if cache:
        cache.put(key, values.tobytes())
    return tuple(values.tolist())


def score_affect(raw_dir: Path, db_path: Path, model_dir: Path, cache: Cache | None = None) -> dict:
    db = sqlite3.connect(db_path)
    try:
        turns = db.execute(
            "SELECT t.turn_id, ca.zero_at, t.start, t.end FROM turns t"
            " JOIN turn_speakers ts ON ts.turn_id = t.turn_id"
            " JOIN conversation_audio ca ON ca.conversation_id = t.conversation_id"
            " WHERE ts.label = 'owner' AND ts.basis IN ('voice', 'channel')"
            " AND t.end - t.start >= ? AND ts.coverage >= ? ORDER BY t.turn_id",
            (MIN_TURN, MIN_COVERAGE),
        ).fetchall()
        bursts = place_bursts(db)
        model = load_model(model_dir) if turns else None
        rows = []
        for turn_id, zero_at, start, end in turns:
            pcm, coverage = cut(raw_dir, bursts, timestamp(zero_at) + start, min(end - start, MAX_SECONDS))
            if coverage >= MIN_COVERAGE:
                rows.append((turn_id, *(round(v, 3) for v in score(model, pcm, cache))))
        db.executescript(SCHEMA)
        db.executemany("INSERT INTO turn_affect VALUES (?,?,?,?)", rows)
        db.commit()
    finally:
        db.close()
    return {"owner_turns": len(turns), "scored": len(rows)}
