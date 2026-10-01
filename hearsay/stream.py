"""The utterance stream: Hearsay's output, for downstream consumers.

Written after every reprocess from the database, one JSONL file per
conversation plus index.json. Consumers never receive audio.

Everything here can change after the fact (a name given to a speaker applies
to past turns, a growing conversation is re-transcribed), so the unit of
change is a conversation, not an utterance. index.json gives each
conversation a revision (a hash of its file); consumers re-read the
conversations whose revision changed and replace them wholesale. utterance_id
is stable only while a conversation's transcript is, which transcript_revision
tracks: it hashes the turns as transcribed (times, words, diarized voices),
never who they were attributed to, so naming leaves it alone.

Files are written atomically and the index last, so a reader never sees a
half-written file, and a file is only rewritten when its content changes.

forgotten.json is the consumers' only deletion signal, so it is append-only:
nothing here ever rewrites or shrinks it.
"""

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Bump on any change to the index or utterance format that would break a consumer.
FORMAT_VERSION = 1

UTTERANCES_SQL = """
SELECT t.conversation_id, t.idx, t.start, t.end, t.text, t.diar_speaker, t.confidence,
       ts.label, ts.basis, ts.owner_similarity, tp.person, tp.cluster, ca.zero_at
FROM turns t
JOIN turn_speakers ts ON ts.turn_id = t.turn_id
LEFT JOIN turn_people tp ON tp.turn_id = t.turn_id
JOIN conversation_audio ca ON ca.conversation_id = t.conversation_id
ORDER BY t.conversation_id, t.start, t.idx
"""


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def speaker_kind(label: str | None, person: str | None, basis: str | None) -> tuple[str, str | None, str] | None:
    """(kind, name, basis), or None for turns left out of the stream.

    Names starting with "_" are the operator's categories, not people.
    _stranger is someone present, so it stays; every other category (_noise,
    _media) is not speech from anyone present and is left out.

    A turn labeled by its diarized speaker rather than its own voice
    (hearsay/speakers.py) says so: its basis is "diarization", whatever the
    kind. Likewise "channel" for one labeled by which channel of a Mac
    recording it was loud on.
    """
    found = speaker_from_label(label, person)
    if found and basis in ("diarization", "channel"):
        return found[0], found[1], basis
    return found


def speaker_from_label(label: str | None, person: str | None) -> tuple[str, str | None, str] | None:
    if label == "owner":
        return "owner", None, "voice"
    if person and person.startswith("_"):
        return ("stranger", None, "named") if person == "_stranger" else None
    if person:
        return "person", person, "named"
    if label == "not_owner":
        return "anonymous", None, "cluster"
    return "unknown", None, "none"


def letters(n: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    out = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def utterances(db: sqlite3.Connection) -> dict[str, list[dict]]:
    by_conversation = {}
    labels = {}  # (conversation, kind, voice) -> "anon A" / "stranger A"
    for (conversation_id, idx, start, end, text, diar_speaker, confidence,
         label, basis, similarity, person, cluster, zero_at) in db.execute(UTTERANCES_SQL):
        speaker = speaker_kind(label, person, basis)
        if speaker is None:
            continue
        kind, name, basis = speaker
        speaker_label = None
        if kind in ("anonymous", "stranger"):
            # Diarization tells voices apart within a conversation; turns it
            # didn't assign fall back to their cluster.
            voice = diar_speaker or cluster
            key = (conversation_id, kind, voice)
            if key not in labels:
                count = sum(1 for c, k, _ in labels if c == conversation_id and k == kind)
                labels[key] = f"{'anon' if kind == 'anonymous' else 'stranger'} {letters(count)}"
            speaker_label = labels[key]
        zero = datetime.fromisoformat(zero_at).timestamp()
        by_conversation.setdefault(conversation_id, []).append({
            "conversation_id": conversation_id,
            "utterance_id": f"{conversation_id}:{idx:04d}",
            "start": iso(zero + start),
            "end": iso(zero + end),
            "speaker": {"kind": kind, "name": name, "label": speaker_label},
            "text": text,
            "text_confidence": round(confidence, 3) if confidence is not None else None,
            "speaker_confidence": {"basis": basis,
                                   "owner_similarity": round(similarity, 3) if similarity is not None else None},
        })
    return by_conversation


def transcript_revisions(db: sqlite3.Connection) -> dict[str, str]:
    """A hash per conversation of what its utterance ids name, and nothing else."""
    rows = {}
    for conversation_id, zero_at, *turn in db.execute(
        "SELECT t.conversation_id, ca.zero_at, t.idx, t.start, t.end, t.text, t.diar_speaker, t.confidence"
        " FROM turns t JOIN conversation_audio ca ON ca.conversation_id = t.conversation_id"
        " ORDER BY t.conversation_id, t.idx"
    ):
        rows.setdefault(conversation_id, [zero_at]).append(turn)
    return {c: hashlib.sha256(json.dumps(r).encode()).hexdigest()[:16] for c, r in rows.items()}


def write_if_changed(path: Path, data: bytes) -> None:
    if path.exists() and path.read_bytes() == data:
        return
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def write_stream(db_path: Path, stream_dir: Path) -> dict:
    db = sqlite3.connect(db_path)
    try:
        found = utterances(db)
        transcripts = transcript_revisions(db)
        conversations = db.execute("SELECT conversation_id, start, end, open FROM conversations ORDER BY start").fetchall()
        taps = {}
        for conversation_id, at in db.execute("SELECT conversation_id, at FROM conversation_taps ORDER BY at"):
            taps.setdefault(conversation_id, []).append(iso(at))
    finally:
        db.close()

    conversation_dir = stream_dir / "conversations"
    conversation_dir.mkdir(parents=True, exist_ok=True)
    index = []
    for conversation_id, start, end, is_open in conversations:
        entry = {"conversation_id": conversation_id, "start": iso(start), "end": iso(end),
                 "open": bool(is_open), "transcribed": conversation_id in found,
                 "taps": taps.get(conversation_id, []),
                 "utterances": 0, "revision": None, "transcript_revision": None, "file": None}
        if conversation_id in found:
            data = "".join(json.dumps(u, ensure_ascii=False) + "\n" for u in found[conversation_id]).encode()
            name = f"conversations/{conversation_id}.jsonl"
            write_if_changed(stream_dir / name, data)
            entry.update(utterances=len(found[conversation_id]), revision=hashlib.sha256(data).hexdigest()[:16],
                         transcript_revision=transcripts[conversation_id], file=name)
        index.append(entry)
    forgotten = stream_dir / "forgotten.json"
    if not forgotten.exists():
        write_if_changed(forgotten, b"[]\n")
    write_if_changed(stream_dir / "index.json",
                     json.dumps({"format_version": FORMAT_VERSION, "conversations": index}, indent=2).encode())

    current = {e["file"] for e in index if e["file"]}
    for path in conversation_dir.glob("*.jsonl"):
        if f"conversations/{path.name}" not in current:
            path.unlink()
    return {"conversations": len(index), "transcribed": len(current),
            "utterances": sum(e["utterances"] for e in index)}
