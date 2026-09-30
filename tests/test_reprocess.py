import pytest

from hearsay import reprocess


def test_a_failed_run_leaves_the_previous_database_in_place(tmp_path, monkeypatch):
    dirs = {name: tmp_path / name for name in ("raw", "audio", "labels", "models", "transcripts", "imports")}
    for d in dirs.values():
        d.mkdir()
    db_path = tmp_path / "hearsay.sqlite"
    db_path.write_bytes(b"the database readers are using")

    def fail(*args, **kwargs):
        raise RuntimeError("voice detection crashed")

    monkeypatch.setattr(reprocess, "find_conversations", fail)
    with pytest.raises(RuntimeError):
        reprocess.run(dirs["raw"], db_path, dirs["audio"], dirs["labels"], dirs["models"],
                      dirs["transcripts"], dirs["imports"])

    assert db_path.read_bytes() == b"the database readers are using"
    assert not (tmp_path / "hearsay.sqlite.building").exists()
