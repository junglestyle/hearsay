import os

from fastapi.testclient import TestClient

from hearsay.receiver import create_capture_app


def all_files(root):
    return sorted(p for p in root.rglob("*") if p.is_file())


def test_capture_upload_needs_the_bearer_token_which_is_never_stored(tmp_path):
    client = TestClient(create_capture_app(tmp_path, "c4pture"))
    body = os.urandom(5000)

    for headers in [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "c4pture-but-not-bearer"}]:
        assert client.post("/capture", content=body, headers=headers).status_code == 401
    assert all_files(tmp_path) == []

    resp = client.post("/capture", content=body,
                       headers={"Authorization": "Bearer c4pture", "Idempotency-Key": "upload-1"})
    assert resp.status_code == 200
    [stored] = list(tmp_path.glob("capture/*/*.body"))
    assert stored.read_bytes() == body
    sidecar = stored.with_suffix(".json").read_text()
    assert "c4pture" not in sidecar and "upload-1" in sidecar


def test_rejections_are_logged_without_the_token(tmp_path, caplog):
    client = TestClient(create_capture_app(tmp_path, "c4pture"))
    caplog.set_level("WARNING", logger="hearsay.receiver")
    client.post("/capture", content=b"x", headers={"Authorization": "Bearer not-the-token"})
    assert len(caplog.records) == 1
    assert "not-the-token" not in caplog.text and "c4pture" not in caplog.text
