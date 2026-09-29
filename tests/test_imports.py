import hashlib
import json
import shutil
import sqlite3
import wave

import pytest

from hearsay.assemble import assemble
from hearsay.imports import load_imports
from hearsay.parse import rebuild
from test_assemble import CHUNK, T, add_conversations, audio_burst, seconds

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def test_imported_audio_fills_the_gap_and_live_audio_wins_the_overlap(tmp_path):
    raw, db_path, audio_dir = tmp_path / "raw", tmp_path / "h.sqlite", tmp_path / "audio"
    imports, cache = tmp_path / "imports", tmp_path / "imports-pcm"
    audio_dir.mkdir()
    imports.mkdir()

    # An export of [0, 20) s after T; each second holds 100 + its index.
    export = tmp_path / "export.wav"
    with wave.open(str(export), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join((100 + s).to_bytes(2, "little") * 16000 for s in range(20)))
    data = export.read_bytes()
    (imports / "a.wav").write_bytes(data)
    (imports / "a.wav.json").write_text(json.dumps({
        "file": "a.wav", "start": T.isoformat(), "sha256": hashlib.sha256(data).hexdigest()}))
    # The live stream also caught [10, 15) s, with values 11..15 (see audio_burst).
    audio_burst(raw, 1, seconds(15))

    rebuild(raw, db_path)
    assert load_imports(imports, db_path, cache) == {"imports": 1, "hours": round(20 / 3600, 2)}
    add_conversations(db_path, [("c", T.timestamp(), seconds(20).timestamp(), 20.0, 0)])
    assemble(raw, db_path, audio_dir)

    with wave.open(str(audio_dir / "c.wav")) as w:
        pcm = w.readframes(w.getnframes())
    per_second = [int.from_bytes(pcm[s * CHUNK : s * CHUNK + 2], "little") for s in range(20)]
    assert per_second == [100 + s for s in range(10)] + [11, 12, 13, 14, 15] + [100 + s for s in range(15, 20)]
    db = sqlite3.connect(db_path)
    assert db.execute("SELECT coverage FROM conversation_audio").fetchone() == (1.0,)
    db.close()

    # Removing the import drops its decoded cache too.
    (imports / "a.wav.json").unlink()
    load_imports(imports, db_path, cache)
    assert list(cache.iterdir()) == []
