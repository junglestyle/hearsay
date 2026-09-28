import io
import json
import random
import sqlite3
import wave
from array import array

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("scipy")

from hearsay.assemble import SCHEMA as ASSEMBLE_SCHEMA  # noqa: E402
from hearsay.parse import SCHEMA as PARSE_SCHEMA  # noqa: E402
from hearsay.people import group_people  # noqa: E402
from hearsay.portal import create_app  # noqa: E402
from hearsay.speakers import SCHEMA as SPEAKERS_SCHEMA  # noqa: E402
from hearsay.turns import SCHEMA as TURNS_SCHEMA, record_operator_input  # noqa: E402


def unit(vector):
    norm = sum(x * x for x in vector) ** 0.5
    return [x / norm for x in vector]


@pytest.fixture
def portal(tmp_path):
    """Two not-owner voices, four 4 s turns each, in one conversation WAV."""
    db_path, audio_dir, labels_dir = tmp_path / "h.sqlite", tmp_path / "audio", tmp_path / "labels"
    audio_dir.mkdir()
    labels_dir.mkdir()
    # Each second of audio holds its own index as the sample value.
    with wave.open(str(audio_dir / "c1.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(sec.to_bytes(2, "little") * 16000 for sec in range(40)))

    rng = random.Random(0)
    voices = [unit([rng.gauss(0, 1) for _ in range(192)]) for _ in range(2)]
    db = sqlite3.connect(db_path)
    db.executescript(PARSE_SCHEMA + ASSEMBLE_SCHEMA + TURNS_SCHEMA + SPEAKERS_SCHEMA)
    db.execute("INSERT INTO conversation_audio VALUES"
               " ('c1', 'memory/p.body', '2026-01-01T00:00:00+00:00', 'live', 40, 1, 'c1.wav', 'sha')")
    for idx in range(8):
        turn_id = f"{'ab'[idx % 2]}{idx}"
        db.execute("INSERT INTO turns VALUES (?,?,?,?,?,?,?,?)",
                   (turn_id, "c1", idx, 5.0 * idx, 5.0 * idx + 4, "synthetic", None, None))
        vector = unit([x + rng.gauss(0, 0.04) for x in voices[idx % 2]])
        db.execute("INSERT INTO turn_speakers VALUES (?,?,?,?,?,?)",
                   (turn_id, "c1", 1.0, array("f", vector).tobytes(), 0.0, "not_owner"))
    db.commit()
    db.close()
    reprocess_people(db_path, labels_dir)

    app = create_app(db_path, audio_dir, labels_dir, "me", "correct horse", "k" * 64)
    return TestClient(app), db_path, labels_dir


def reprocess_people(db_path, labels_dir):
    """The reprocess steps that apply the operator's tags."""
    record_operator_input(db_path, labels_dir)
    group_people(db_path)


def log_in(client):
    return client.post("/login", data={"username": "me", "password": "correct horse"}, follow_redirects=False)


def test_nothing_is_reachable_without_logging_in(portal):
    client, _, _ = portal
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/audio/a0").status_code == 401

    wrong = client.post("/login", data={"username": "me", "password": "nope"}, follow_redirects=False)
    assert wrong.status_code == 200 and "set-cookie" not in wrong.headers
    client.cookies.set("hearsay_session", "9999999999.forged")
    assert client.get("/", follow_redirects=False).status_code == 303

    client.cookies.clear()
    assert log_in(client).status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 200


def test_naming_in_the_portal_names_the_whole_cluster_after_reprocess(portal):
    client, db_path, labels_dir = portal
    log_in(client)
    db = sqlite3.connect(db_path)
    cluster_a = db.execute("SELECT cluster FROM turn_people WHERE turn_id = 'a0'").fetchone()[0]
    db.close()

    response = client.post(f"/cluster/{cluster_a}", data={"action": "name", "name": "  Alice  "}, follow_redirects=False)
    assert response.status_code == 303
    [record] = [json.loads(line) for line in (labels_dir / "tags.jsonl").read_text().splitlines()]
    assert record["name"] == "Alice" and len(record["turns"]) == 3
    assert "Alice" in client.get("/").text  # applied immediately, before any reprocess

    reprocess_people(db_path, labels_dir)
    db = sqlite3.connect(db_path)
    people = dict(db.execute("SELECT turn_id, person FROM turn_people"))
    db.close()
    assert {people[s] for s in ["a0", "a2", "a4", "a6"]} == {"Alice"}
    assert {people[s] for s in ["b1", "b3", "b5", "b7"]} == {None}


def test_turn_audio_supports_byte_ranges_for_ios(portal):
    client, _, _ = portal
    log_in(client)
    full = client.get("/audio/a2")  # turn [10, 14) s
    assert full.status_code == 200 and full.headers["accept-ranges"] == "bytes"
    with wave.open(io.BytesIO(full.content)) as w:
        assert w.getnframes() == 4 * 16000
        assert int.from_bytes(w.readframes(1), "little") == 10

    probe = client.get("/audio/a2", headers={"Range": "bytes=0-1"})
    assert probe.status_code == 206
    assert probe.content == full.content[:2]
    assert probe.headers["content-range"] == f"bytes 0-1/{len(full.content)}"


def test_skipped_clusters_go_to_the_back_of_the_queue(portal):
    client, db_path, labels_dir = portal
    log_in(client)
    db = sqlite3.connect(db_path)
    cluster = dict(db.execute("SELECT turn_id, cluster FROM turn_people"))
    db.close()
    first, second = cluster["a0"], cluster["b1"]

    skipped = client.post(f"/cluster/{first}", data={"action": "skip"}, follow_redirects=False)
    assert skipped.headers["location"] == f"/cluster/{second}"
    index = client.get("/").text
    assert index.index("Skipped (1)") < index.index(f"/cluster/{first}")

    named = client.post(f"/cluster/{second}", data={"action": "name", "name": "Bob"}, follow_redirects=False)
    assert named.headers["location"] == f"/cluster/{first}"  # comes round again once the rest is done


def test_forgetting_a_name_unnames_the_cluster(portal):
    client, db_path, labels_dir = portal
    log_in(client)
    db = sqlite3.connect(db_path)
    cluster_a = db.execute("SELECT cluster FROM turn_people WHERE turn_id = 'a0'").fetchone()[0]
    db.close()

    client.post(f"/cluster/{cluster_a}", data={"action": "name", "name": "Alice"})
    client.post(f"/cluster/{cluster_a}", data={"action": "forget"})
    assert "To name (2)" in client.get("/").text

    reprocess_people(db_path, labels_dir)
    db = sqlite3.connect(db_path)
    assert db.execute("SELECT DISTINCT person FROM turn_people").fetchall() == [(None,)]
    db.close()
