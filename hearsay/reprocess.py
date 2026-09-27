"""Rebuild everything derived from raw: the database, then per-conversation audio."""

import os
import sys
from pathlib import Path

from hearsay.assemble import assemble
from hearsay.parse import ParseFailed, rebuild


def main() -> None:
    raw_dir = Path(os.environ["HEARSAY_RAW_DIR"])
    db_path = Path(os.environ["HEARSAY_DB"])
    audio_dir = Path(os.environ["HEARSAY_AUDIO_DIR"])
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


if __name__ == "__main__":
    main()
