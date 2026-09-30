"""A key-value cache for results that are slow to compute and depend only on audio.

Keys are a hash of the input audio plus the model and settings that produced
the value, so a hit is always exactly what recomputing would give. The cache
is derived: deleting the file is always safe, the next run just repeats work.
"""

import hashlib
import sqlite3
from pathlib import Path


def audio_key(kind: str, pcm: bytes) -> str:
    return f"{kind}:{hashlib.sha256(pcm).hexdigest()}"


class Cache:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value BLOB NOT NULL)")

    def get(self, key: str) -> bytes | None:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def put(self, key: str, value: bytes) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, value))

    def close(self) -> None:
        self.db.commit()
        self.db.close()
