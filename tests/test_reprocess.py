import sqlite3

import pytest

from hearsay import reprocess


def test_a_failed_run_leaves_the_previous_database_in_place(tmp_path, monkeypatch):
    dirs = {name: tmp_path / name for name in ("raw", "audio", "labels", "models", "transcripts", "imports", "stream")}
    for d in dirs.values():
        d.mkdir()
    db_path = tmp_path / "hearsay.sqlite"
    db_path.write_bytes(b"the database readers are using")

    def fail(*args, **kwargs):
        raise RuntimeError("voice detection crashed")

    monkeypatch.setattr(reprocess, "find_conversations", fail)
    with pytest.raises(RuntimeError):
        reprocess.run(dirs["raw"], db_path, dirs["audio"], dirs["labels"], dirs["models"],
                      dirs["transcripts"], dirs["imports"], dirs["stream"], dirs["models"])

    assert db_path.read_bytes() == b"the database readers are using"


def build(path, script):
    path.unlink(missing_ok=True)
    db = sqlite3.connect(path)
    db.executescript(script)
    db.close()


def dump(path):
    db = sqlite3.connect(path)
    try:
        return sorted(db.iterdump())
    finally:
        db.close()


def test_the_database_is_updated_in_place_and_untouched_when_nothing_changed(tmp_path):
    built, live = tmp_path / "built.sqlite", tmp_path / "hearsay.sqlite"
    first = """
        CREATE TABLE turns (turn_id TEXT PRIMARY KEY, text TEXT);
        CREATE INDEX turns_text ON turns(text);
        CREATE TABLE marks (at REAL, kind TEXT);
        INSERT INTO turns VALUES ('c1:0', 'hello'), ('c1:1', NULL);
        INSERT INTO marks VALUES (1.5, 'start'), (1.5, 'start');
        CREATE TABLE gone (x);
    """
    build(built, first)
    reprocess.update_in_place(built, live)
    assert dump(live) == dump(built)

    inode, before = live.stat().st_ino, live.read_bytes()
    build(built, first)
    assert reprocess.update_in_place(built, live) == {"tables_recreated": 0, "rows_deleted": 0, "rows_inserted": 0}
    assert live.stat().st_ino == inode and live.read_bytes() == before

    # A renamed turn, one fewer duplicate mark, a new column and a dropped table.
    build(built, """
        CREATE TABLE turns (turn_id TEXT PRIMARY KEY, text TEXT);
        CREATE INDEX turns_text ON turns(text);
        CREATE TABLE marks (at REAL, kind TEXT, payload TEXT);
        INSERT INTO turns VALUES ('c1:0', 'hello'), ('c1:1', 'there');
        INSERT INTO marks VALUES (1.5, 'start', 'p');
    """)
    reprocess.update_in_place(built, live)
    assert dump(live) == dump(built)
    assert live.stat().st_ino == inode
