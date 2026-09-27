import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hearsay.assemble import assemble
from hearsay.parse import ParseFailed, rebuild
from hearsay.receiver import create_app
from hearsay.speakers import label_speakers

FIXTURES = Path(__file__).parent / "fixtures"


def post(client, webhook_type, body, idempotency_key, query="uid=test-uid"):
    content_type = "application/octet-stream" if webhook_type == "audio" else "application/json"
    resp = client.post(
        f"/omi/{webhook_type}?token=s3cret&{query}",
        content=body,
        headers={"Content-Type": content_type, "Idempotency-Key": idempotency_key},
    )
    assert resp.status_code == 200


def capture_synthetic_raw(raw_dir):
    """Write synthetic payloads through the real receiver, so the raw layout is its own."""
    client = TestClient(create_app(raw_dir, "s3cret"))
    transcript_1 = (FIXTURES / "transcript_1.json").read_bytes()
    post(client, "transcript", transcript_1, "key-t1")
    post(client, "transcript", transcript_1, "key-t1")  # Omi retry
    post(client, "transcript", (FIXTURES / "transcript_2.json").read_bytes(), "key-t2")
    post(client, "audio", b"\x00\x01" * 16000, "key-a1", "sample_rate=16000&uid=test-uid")
    post(client, "audio", b"\x00\x01" * 1600, "key-a2", "sample_rate=16000&uid=test-uid")
    post(client, "memory", (FIXTURES / "memory.json").read_bytes(), "key-m1")


def snapshot(root):
    return {p: p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def dump(db_path):
    db = sqlite3.connect(db_path)
    try:
        return list(db.iterdump())
    finally:
        db.close()


def test_rebuild_parses_every_type_is_repeatable_and_leaves_raw_untouched(tmp_path):
    raw = tmp_path / "raw"
    capture_synthetic_raw(raw)
    # Simulate a crash between body and sidecar writes.
    (raw / "audio" / "2026-01-01").mkdir(parents=True)
    (raw / "audio" / "2026-01-01" / "20260101T000000.000000Z-deadbeef.body").write_bytes(b"\x00\x00")
    before = snapshot(raw)

    counts = rebuild(raw, tmp_path / "a.sqlite")
    rebuild(raw, tmp_path / "b.sqlite")

    assert snapshot(raw) == before
    assert dump(tmp_path / "a.sqlite") == dump(tmp_path / "b.sqlite")
    assert counts == {"transcript": 2, "audio": 2, "memory": 1, "duplicate": 1, "incomplete": 1}

    db = sqlite3.connect(tmp_path / "a.sqlite")
    live = db.execute(
        "SELECT s.segment_id, s.text FROM segments s JOIN payloads p ON p.path = s.payload_path"
        " WHERE p.type = 'transcript' ORDER BY p.received_at, s.idx"
    ).fetchall()
    assert [text for _, text in live] == ["The", " quick", " brown fox."]
    final = db.execute(
        "SELECT m.conversation_id, s.segment_id, s.text, s.end FROM memories m"
        " JOIN segments s ON s.payload_path = m.payload_path"
    ).fetchall()
    assert final == [("00000000-0000-4000-8000-0000000000c1", live[0][0], "The quick brown fox.", 0.9)]
    assert db.execute("SELECT started_at, duration FROM memory_audio_files").fetchall() == [
        ("2026-01-01T12:00:00.000000+00:00", 5.0)
    ]
    assert db.execute("SELECT sample_rate FROM audio_chunks").fetchall() == [(16000,), (16000,)]
    db.close()


@pytest.mark.parametrize("damage", ["malformed_transcript", "body_changed_after_receipt"])
def test_failed_rebuild_keeps_previous_database(tmp_path, damage):
    raw = tmp_path / "raw"
    capture_synthetic_raw(raw)
    db_path = tmp_path / "hearsay.sqlite"
    rebuild(raw, db_path)
    good = dump(db_path)

    if damage == "malformed_transcript":
        post(TestClient(create_app(raw, "s3cret")), "transcript", b'[{"text": "bare array"}]', "key-bad")
    else:
        next(raw.glob("memory/*/*.body")).write_bytes(b"{}")

    with pytest.raises(ParseFailed) as failed:
        rebuild(raw, db_path)
    assert len(failed.value.errors) == 1
    assert dump(db_path) == good
    assert not list(tmp_path.glob("*.tmp"))


# Runs only where real captures are mounted (the NAS check container). Real
# payloads contain other people's speech and never enter the repo.
REAL_RAW_DIR = os.environ.get("HEARSAY_CHECK_RAW_DIR")


@pytest.mark.skipif(
    not REAL_RAW_DIR or not Path(REAL_RAW_DIR).is_dir(), reason="no real captures here"
)
def test_every_real_captured_payload_reprocesses(tmp_path):
    raw, db_path = Path(REAL_RAW_DIR), tmp_path / "check.sqlite"
    (tmp_path / "audio").mkdir()
    (tmp_path / "labels").mkdir()
    rebuild(raw, db_path)
    assemble(raw, db_path, tmp_path / "audio")
    label_speakers(raw, db_path, tmp_path / "labels", Path(os.environ["HEARSAY_MODEL_DIR"]))
