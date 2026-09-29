import os

from fastapi.testclient import TestClient

from hearsay.receiver import create_app


def all_files(root):
    return sorted(p for p in root.rglob("*") if p.is_file())


def test_authorized_body_stored_verbatim_unauthorized_writes_nothing(tmp_path):
    client = TestClient(create_app(tmp_path, "s3cret"))
    body = b"\x00\xff\xfe\x80" + os.urandom(32000) + b"\x00"

    resp = client.post(
        "/omi/audio?token=s3cret&uid=u&sample_rate=16000",
        content=body,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 200
    bodies = list(tmp_path.rglob("*.body"))
    assert len(bodies) == 1
    assert bodies[0].read_bytes() == body
    sidecar = bodies[0].with_suffix(".json")
    assert sidecar.exists()
    assert "s3cret" not in sidecar.read_text()

    before = all_files(tmp_path)
    for url in ["/omi/audio?uid=u", "/omi/audio?token=wrong&uid=u"]:
        assert client.post(url, content=body).status_code == 401
    assert all_files(tmp_path) == before


def test_rejections_are_logged_without_the_token(tmp_path, caplog):
    client = TestClient(create_app(tmp_path, "s3cret"))
    caplog.set_level("WARNING", logger="hearsay.receiver")
    client.post("/omi/audio?token=not-the-secret&uid=u", content=b"x")
    assert len(caplog.records) == 1
    assert "not-the-secret" not in caplog.text and "s3cret" not in caplog.text
