"""Audio the operator imported by hand, placed on the timeline next to the live stream.

For gaps the live stream missed, e.g. Omi's exports of a day its webhooks
didn't deliver. `hearsay-label import` copies each file, unchanged, into the
imports dir with a sidecar giving the recording's start time as the operator
read it off the Omi app (to the minute). Like raw, imports are never modified.

Reprocess decodes each file once to PCM16 mono at 16 kHz (ffmpeg), caches the
PCM by the file's sha256, and records it in imported_audio. place_bursts then
lays imported audio before the live stream, so where they overlap the live
stream's exactly-timed audio wins.
"""

import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

from hearsay.assemble import BYTES_PER_SECOND, SAMPLE_RATE, timestamp

SCHEMA = """
DROP TABLE IF EXISTS imported_audio;
CREATE TABLE imported_audio (
    file TEXT PRIMARY KEY,           -- relative to the imports dir
    start REAL NOT NULL,             -- unix seconds, as the operator gave it
    duration REAL NOT NULL,          -- seconds of decoded audio
    sha256 TEXT NOT NULL,
    pcm_path TEXT NOT NULL           -- absolute path of the decoded PCM cache
);
"""


def decode(source: Path, target: Path) -> None:
    tmp = target.with_name(target.name + ".tmp")
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(source),
         "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), str(tmp)],
        check=True,
    )
    tmp.replace(target)


def load_imports(imports_dir: Path, db_path: Path, cache_dir: Path) -> dict:
    cache_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for sidecar_path in sorted(imports_dir.glob("*.json")):
        sidecar = json.loads(sidecar_path.read_text())
        source = imports_dir / sidecar["file"]
        sha = hashlib.sha256(source.read_bytes()).hexdigest()
        if sha != sidecar["sha256"]:
            raise ValueError(f"{source}: sha256 does not match its sidecar")
        pcm = cache_dir / f"{sha}.pcm"
        if not pcm.exists():
            decode(source, pcm)
        rows.append((sidecar["file"], timestamp(sidecar["start"]), pcm.stat().st_size / BYTES_PER_SECOND,
                     sha, str(pcm)))
    # The cache is derived: drop PCM for imports that no longer exist.
    current = {r[3] for r in rows}
    for pcm in cache_dir.glob("*.pcm"):
        if pcm.stem not in current:
            pcm.unlink()

    db = sqlite3.connect(db_path)
    try:
        db.executescript(SCHEMA)
        db.executemany("INSERT INTO imported_audio VALUES (?,?,?,?,?)", rows)
        db.commit()
    finally:
        db.close()
    return {"imports": len(rows), "hours": round(sum(r[2] for r in rows) / 3600, 2)}
