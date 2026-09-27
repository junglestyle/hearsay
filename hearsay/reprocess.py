"""Rebuild everything derived from raw: database, conversation audio, speaker labels, people."""

import os
import sqlite3
import sys
from pathlib import Path

from hearsay.assemble import assemble
from hearsay.parse import ParseFailed, rebuild
from hearsay.people import group_people
from hearsay import people, speakers
from hearsay.speakers import label_speakers


def main() -> None:
    raw_dir = Path(os.environ["HEARSAY_RAW_DIR"])
    db_path = Path(os.environ["HEARSAY_DB"])
    audio_dir = Path(os.environ["HEARSAY_AUDIO_DIR"])
    labels_dir = Path(os.environ["HEARSAY_LABELS_DIR"])
    model_dir = Path(os.environ["HEARSAY_MODEL_DIR"])
    try:
        parsed = rebuild(raw_dir, db_path)
    except ParseFailed as e:
        print(e, file=sys.stderr)
        print(f"{db_path} and {audio_dir} left unchanged.", file=sys.stderr)
        sys.exit(1)
    print(f"Rebuilt {db_path} from {raw_dir}:")
    for name, n in parsed.items():
        print(f"  {name}: {n}")

    assembled = assemble(raw_dir, db_path, audio_dir)
    print(f"Assembled conversation audio in {audio_dir}:")
    for name, n in assembled.items():
        print(f"  {name}: {n}")

    labeled = label_speakers(raw_dir, db_path, labels_dir, model_dir)
    print("Speaker embeddings and labels:")
    for name, n in labeled.items():
        print(f"  {name}: {n}")

    grouped = group_people(db_path, labels_dir)
    print("Anonymous speakers and names:")
    for name, n in grouped.items():
        print(f"  {name}: {n}")

    record_run(db_path)


def record_run(db_path: Path) -> None:
    """Record what produced this database, so a stale image is easy to spot."""
    info = {
        "commit": os.environ.get("HEARSAY_COMMIT", "unknown"),
        "min_segment": speakers.MIN_SEGMENT,
        "owner_threshold": speakers.OWNER_THRESHOLD,
        "not_owner_threshold": speakers.NOT_OWNER_THRESHOLD,
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


if __name__ == "__main__":
    main()
