"""Rebuild everything derived from raw: database, conversations, conversation
audio, turns, speaker labels, people, and the utterance stream.

Turns come from transcripts the dev box makes of each WAV (hearsay/transcribe.py),
so new audio takes a reprocess, a transcription run, then another reprocess.
On the NAS this runs hourly (install/nas.sh), offset from the dev box's timer.

The database is built as a staging copy and swapped in only when every step
has finished, so the portal and the label tool always read a complete one,
and a failed run leaves the previous database in place. Slow steps (voice
detection, speaker embeddings) are cached by audio content next to it.
"""

import os
import sqlite3
import sys
from pathlib import Path

from hearsay import conversations, people, speakers
from hearsay.assemble import assemble, remove_stale_wavs
from hearsay.cache import Cache
from hearsay.capture import load_captures
from hearsay.conversations import find_conversations
from hearsay.imports import load_imports
from hearsay.parse import ParseFailed, rebuild
from hearsay.people import group_people
from hearsay.speakers import label_speakers
from hearsay.stream import write_stream
from hearsay.turns import build_turns, record_operator_input


def report(title: str, counts: dict) -> None:
    print(title)
    for name, n in counts.items():
        print(f"  {name}: {n}")


def run(raw_dir: Path, db_path: Path, audio_dir: Path, labels_dir: Path, model_dir: Path,
        transcripts_dir: Path, imports_dir: Path, stream_dir: Path) -> None:
    staging = db_path.with_name(db_path.name + ".building")
    cache = Cache(db_path.parent / "cache.sqlite")
    try:
        report(f"Rebuilt from {raw_dir}:", rebuild(raw_dir, staging))
        # Decoded imports are derived, so they're cached next to the database.
        report(f"Imported audio from {imports_dir}:",
               load_imports(imports_dir, staging, db_path.parent / "imports-pcm"))
        report("Audio from the recorder:", load_captures(raw_dir, staging, db_path.parent / "capture-pcm"))
        report("Conversations found in the audio stream:", find_conversations(raw_dir, staging, cache))
        report(f"Assembled conversation audio in {audio_dir}:", assemble(raw_dir, staging, audio_dir))
        report(f"Turns from transcripts in {transcripts_dir}:", build_turns(staging, transcripts_dir))
        report("Operator labels and names, matched to turns:", record_operator_input(staging, labels_dir))
        report("Speaker embeddings and labels:", label_speakers(raw_dir, staging, labels_dir, model_dir, cache))
        report("Anonymous speakers and names:", group_people(staging))
        record_run(staging)
        os.replace(staging, db_path)
    finally:
        cache.close()
        staging.unlink(missing_ok=True)
    # From the database now in place, so the stream always matches it.
    report(f"Utterance stream in {stream_dir}:", write_stream(db_path, stream_dir))
    print(f"Replaced {db_path}; removed {remove_stale_wavs(audio_dir, db_path)} stale WAV(s).")


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
            "stream_dir")}
    try:
        run(env["raw_dir"], env["db"], env["audio_dir"], env["labels_dir"], env["model_dir"],
            env["transcripts_dir"], env["imports_dir"], env["stream_dir"])
    except ParseFailed as e:
        print(e, file=sys.stderr)
        print(f"{env['db']} and {env['audio_dir']} left unchanged.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
