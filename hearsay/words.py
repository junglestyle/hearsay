"""Words Parakeet is nudged toward while it transcribes: names, jargon,
anything it keeps mishearing.

Two sources: words the operator types on the portal's Words page, and terms
the operator approved in Lattice, which Idea Machine writes to vocab.json
({format_version: 1, terms: [{term, source, heard_as?}]}) in a folder Hearsay
only reads. Neither reaches transcription until the operator presses Update:
that writes both, as they were then, to labels/words.json in one atomic
rename. Every Update changes the transcription settings, so every
conversation is transcribed again on the next run (hearsay/transcribe.py).
Typed edits wait in labels/words.draft.txt until then, for as long as it takes.
"""

import json
import os
from pathlib import Path

VOCAB_FORMAT = 1


def tidy(lines: list[str]) -> list[str]:
    """One word or phrase per entry: spaces collapsed, blanks and exact duplicates
    dropped, in alphabetical order regardless of case. Case itself is kept:
    Parakeet writes capitals, so the spelling given is the one it leans toward."""
    words = {" ".join(line.split()) for line in lines} - {""}
    return sorted(words, key=lambda w: (w.casefold(), w))


def read_committed(labels_dir: Path) -> dict[str, list[str]]:
    path = labels_dir / "words.json"
    if not path.exists():
        return {"typed": [], "lattice": []}
    return json.loads(path.read_text())


def boosted(committed: dict[str, list[str]]) -> list[str]:
    return tidy(committed["typed"] + committed["lattice"])


def read_draft(labels_dir: Path) -> list[str] | None:
    path = labels_dir / "words.draft.txt"
    return path.read_text().splitlines() if path.exists() else None


def read_lattice(vocab_path: Path | None) -> tuple[list[str], str | None]:
    """Lattice's approved terms, and why they couldn't be read if they couldn't.
    No file yet (or no folder set) is no terms, not an error."""
    if vocab_path is None or not vocab_path.exists():
        return [], None
    try:
        vocab = json.loads(vocab_path.read_text())
        if vocab.get("format_version") != VOCAB_FORMAT:
            return [], f"vocab.json has format_version {vocab.get('format_version')!r}; Hearsay reads {VOCAB_FORMAT}"
        return tidy([t["term"] for t in vocab["terms"]]), None
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        return [], f"vocab.json couldn't be read: {e!r}"


def write_atomically(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def save_draft(labels_dir: Path, typed: list[str]) -> None:
    """A draft the same as what's committed is no draft."""
    path = labels_dir / "words.draft.txt"
    if typed == read_committed(labels_dir)["typed"]:
        path.unlink(missing_ok=True)
        return
    write_atomically(path, "".join(f"{w}\n" for w in typed))


def commit(labels_dir: Path, typed: list[str], lattice: list[str]) -> None:
    write_atomically(labels_dir / "words.json", json.dumps({"typed": typed, "lattice": lattice}, indent=1) + "\n")
    (labels_dir / "words.draft.txt").unlink(missing_ok=True)
