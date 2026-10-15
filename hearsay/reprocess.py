"""Rebuild everything derived from raw: database, conversations, conversation
audio, turns, speaker labels, people, and the utterance stream.

Turns come from transcripts the dev box makes of each WAV (hearsay/transcribe.py),
so new audio takes a reprocess, a transcription run, then another reprocess.
On the NAS this runs hourly (install/nas.sh), offset from the dev box's timer.

Every run builds a complete new database from raw in a scratch directory
(a tmpfs in the container), then brings the real one up to date in a single
transaction that changes only the rows that differ. So the portal and the
label tool always read a complete database, a failed run leaves the previous
one in place, and a run with nothing new writes almost nothing to disk (the
NAS keeps snapshots, so every rewritten byte is kept many times). A schema
change replaces the changed table inside the same transaction. Deleting
the database is always safe: the next run fills a new one from raw.

Slow steps (voice detection, speaker embeddings) are cached by audio content
next to the database. A run waits for any other to finish (the hourly one and
one started by hand used to overlap and break each other).
"""

import fcntl
import os
import sqlite3
import sys
import tempfile
from collections import Counter
from pathlib import Path

from hearsay import conversations, people, speakers
from hearsay.assemble import assemble, remove_stale_wavs
from hearsay.cache import Cache
from hearsay.capture import load_captures
from hearsay.conversations import find_conversations
from hearsay.imports import load_imports
from hearsay.parse import ParseFailed, rebuild
from hearsay.affect import score_affect
from hearsay.delivery import measure_delivery
from hearsay.media import score_media
from hearsay.people import group_people
from hearsay.places import find_places
from hearsay.speakers import label_speakers
from hearsay.stream import write_stream
from hearsay.turns import build_turns, record_operator_input


def report(title: str, counts: dict) -> None:
    print(title)
    for name, n in counts.items():
        print(f"  {name}: {n}")


def run(raw_dir: Path, db_path: Path, audio_dir: Path, labels_dir: Path, model_dir: Path,
        transcripts_dir: Path, imports_dir: Path, stream_dir: Path, affect_model_dir: Path) -> None:
    lock = open(db_path.with_name("reprocess.lock"), "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    scratch = tempfile.TemporaryDirectory(prefix="hearsay-reprocess-")
    staging = Path(scratch.name) / db_path.name
    cache = Cache(db_path.parent / "cache.sqlite")
    try:
        report(f"Rebuilt from {raw_dir}:", rebuild(raw_dir, staging))
        # Decoded imports are derived, so they're cached next to the database.
        report(f"Imported audio from {imports_dir}:",
               load_imports(imports_dir, staging, db_path.parent / "imports-pcm"))
        report("Audio from the recorder:", load_captures(raw_dir, staging, db_path.parent / "capture-pcm"))
        report("Conversations found in the audio stream:", find_conversations(raw_dir, staging, cache))
        report("Where conversations happened:", find_places(staging, labels_dir))
        report(f"Assembled conversation audio in {audio_dir}:", assemble(raw_dir, staging, audio_dir))
        report(f"Turns from transcripts in {transcripts_dir}:", build_turns(staging, transcripts_dir))
        report("Operator labels and names, matched to turns:", record_operator_input(staging, labels_dir))
        report("Speaker embeddings and labels:", label_speakers(raw_dir, staging, labels_dir, model_dir, cache))
        report("The owner's tone of voice:", score_affect(raw_dir, staging, affect_model_dir, cache))
        report("How the owner delivered their turns:", measure_delivery(staging, transcripts_dir))
        report("Anonymous speakers and names:", group_people(staging))
        report("Media hints, learned from the operator's tags:", score_media(staging))
        record_run(staging)
        report(f"Updated {db_path} in place:", update_in_place(staging, db_path))
    finally:
        cache.close()
        scratch.cleanup()
    # From the database now in place, so the stream always matches it.
    report(f"Utterance stream in {stream_dir}:", write_stream(db_path, stream_dir))
    print(f"Removed {remove_stale_wavs(audio_dir, db_path)} stale WAV(s).")


def schema(db: sqlite3.Connection, name: str) -> dict[str, tuple[str, str]]:
    return {row[0]: (row[1], row[2]) for row in db.execute(
        f"SELECT name, type, sql FROM {name}.sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'")}


def update_in_place(built: Path, db_path: Path) -> dict:
    """Make db_path hold exactly what built holds, in one transaction.

    Rows are compared whole, as a multiset per table (some tables have no
    key), and only the differences are written, so an unchanged database
    gets no writes at all. A table or index whose definition changed is
    dropped and recreated. Nothing reads the live database in row order.
    """
    counts = {"tables_recreated": 0, "rows_deleted": 0, "rows_inserted": 0}
    db = sqlite3.connect(db_path, isolation_level=None)
    try:
        db.execute("ATTACH DATABASE ? AS built", (str(built),))
        db.execute("BEGIN IMMEDIATE")
        new, old = schema(db, "built"), schema(db, "main")
        for name, (kind, sql) in old.items():
            if new.get(name) != (kind, sql):
                # Dropping a table drops its indexes too, hence IF EXISTS.
                db.execute(f'DROP {kind.upper()} IF EXISTS main."{name}"')
        for name, (kind, sql) in new.items():
            if kind == "table" and old.get(name) != (kind, sql):
                db.execute(sql)
                counts["tables_recreated"] += 1
        for name, (kind, _) in new.items():
            if kind != "table":
                continue
            wanted = Counter(db.execute(f'SELECT * FROM built."{name}"'))
            stale = []
            for rowid, *row in db.execute(f'SELECT rowid, * FROM main."{name}"').fetchall():
                row = tuple(row)
                if wanted[row] > 0:
                    wanted[row] -= 1
                else:
                    stale.append((rowid,))
            # Deletes first, so a changed row never collides with its old key.
            db.executemany(f'DELETE FROM main."{name}" WHERE rowid = ?', stale)
            inserts = list(wanted.elements())
            if inserts:
                marks = ",".join("?" * len(inserts[0]))
                db.executemany(f'INSERT INTO main."{name}" VALUES ({marks})', inserts)
            counts["rows_deleted"] += len(stale)
            counts["rows_inserted"] += len(inserts)
        # Indexes last: recreated if changed, or dropped with their table.
        present = schema(db, "main")
        for name, (kind, sql) in new.items():
            if kind != "table" and name not in present:
                db.execute(sql)
        db.execute("COMMIT")
    finally:
        db.close()
    return counts


def record_run(db_path: Path) -> None:
    """Record what produced this database, so a stale image is easy to spot."""
    info = {
        "commit": os.environ.get("HEARSAY_COMMIT", "unknown"),
        "conversation_gap": conversations.GAP,
        "min_speech": conversations.MIN_SPEECH,
        "min_turn": speakers.MIN_TURN,
        "owner_threshold": speakers.OWNER_THRESHOLD,
        "not_owner_threshold": speakers.NOT_OWNER_THRESHOLD,
        "diarization_agreement": speakers.DIARIZATION_AGREEMENT,
        "cluster_threshold": people.CLUSTER_THRESHOLD,
    }
    db = sqlite3.connect(db_path)
    try:
        db.executescript("DROP TABLE IF EXISTS reprocess_info; CREATE TABLE reprocess_info (key TEXT PRIMARY KEY, value TEXT)")
        db.executemany("INSERT INTO reprocess_info VALUES (?, ?)", [(k, str(v)) for k, v in info.items()])
        db.commit()
    finally:
        db.close()
    print(f"Built from commit {info['commit']}.")


def main() -> None:
    env = {name: Path(os.environ[f"HEARSAY_{name.upper()}"]) for name in
           ("raw_dir", "db", "audio_dir", "labels_dir", "model_dir", "transcripts_dir", "imports_dir",
            "stream_dir", "affect_model_dir")}
    try:
        run(env["raw_dir"], env["db"], env["audio_dir"], env["labels_dir"], env["model_dir"],
            env["transcripts_dir"], env["imports_dir"], env["stream_dir"], env["affect_model_dir"])
    except ParseFailed as e:
        print(e, file=sys.stderr)
        print(f"{env['db']} and {env['audio_dir']} left unchanged.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
