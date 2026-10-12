"""How the owner delivered each turn: speaking rate, pauses, and stutters.

From Parakeet's word timings, so no audio is needed and nothing is judged:
these are measurements of the owner's speech, not of what they meant. Owner
turns only, labeled by voice or channel, at least MIN_TURN long, like affect.

Stutters are the same word twice in a row, a fragment the next word starts
with ("wh what"), and fillers ("uh", "um"). Each carries the speaking rate in
the STUTTER_LEAD seconds before it, since the owner's stutters come from
speaking quickly. Checked blind 2026-10-12: of 24 detections the owner judged
by ear, 20 were real stutters (repeats 11/13, restarts 5/6, fillers 4/5); one
miss was "yeah, yeah, yeah", so repeats of short answer words are left out.
On 1,102 owner turns, above about 3.3 words/s the owner repeated and
restarted 30-50% more per word than below it, and used fewer fillers.

Speech can't be judged assertive or tentative from these: on the owner's
blind ratings of calm turns, ending pitch, rate, pauses and trailing off all
agreed with them no better than chance.
"""

import json
import re
import sqlite3
from pathlib import Path

from hearsay.speakers import MIN_TURN

# A gap between words this long is a pause.
PAUSE = 0.25
# The same word again within this long is a repeat, not a new start.
REPEAT_GAP = 0.5
# Seconds before a stutter over which its lead-in speaking rate is measured.
STUTTER_LEAD = 3.0
FILLERS = {"uh", "um", "uhm", "er", "erm"}
# Doubled on purpose ("yeah, yeah", "no, no"), not stutters.
ANSWERS = {"yeah", "yes", "yep", "no", "nope", "okay", "ok", "right", "bye"}

SCHEMA = """
DROP TABLE IF EXISTS turn_delivery;
DROP TABLE IF EXISTS turn_stutters;
-- How the owner delivered each of their turns (hearsay/delivery.py).
CREATE TABLE turn_delivery (
    turn_id TEXT PRIMARY KEY REFERENCES turns(turn_id),
    words_per_second REAL NOT NULL,  -- per second of speaking, pauses left out
    pauses INTEGER NOT NULL,         -- gaps between words over PAUSE
    pause_seconds REAL               -- their mean length, NULL if none
);
CREATE TABLE turn_stutters (
    turn_id TEXT NOT NULL REFERENCES turns(turn_id),
    at REAL NOT NULL,                -- seconds into the conversation WAV
    kind TEXT NOT NULL,              -- repeat | restart | filler
    words_per_second_before REAL     -- over the STUTTER_LEAD s before, NULL if too few words
);
"""


def norm(word: str) -> str:
    return re.sub(r"[^\w']", "", word.lower())


def speaking_rate(words: list[tuple[float, float, str]]) -> float | None:
    """Words per second of speaking time: the span of the words, pauses left out."""
    if len(words) < 2:
        return None
    pauses = sum(b[0] - a[1] for a, b in zip(words, words[1:]) if b[0] - a[1] > PAUSE)
    return len(words) / max(words[-1][1] - words[0][0] - pauses, 0.1)


def stutters(words: list[tuple[float, float, str]]) -> list[tuple[float, str, float | None]]:
    """(at, kind, words per second before) for each stutter in a turn's words, in order."""
    found = []
    for i, (start, end, word) in enumerate(words):
        this = norm(word)
        following = norm(words[i + 1][2]) if i + 1 < len(words) else ""
        close = i + 1 < len(words) and words[i + 1][0] - end < REPEAT_GAP
        if this in FILLERS:
            kind = "filler"
        elif this and close and this == following and this not in ANSWERS:
            kind = "repeat"
        elif this and close and len(this) < len(following) and following.startswith(this):
            kind = "restart"
        else:
            continue
        before = [w for w in words if start - STUTTER_LEAD <= w[0] < start]
        found.append((start, kind, speaking_rate(before)))
    return found


def measure_delivery(db_path: Path, transcripts_dir: Path) -> dict:
    db = sqlite3.connect(db_path)
    try:
        turns = db.execute(
            "SELECT t.turn_id, t.conversation_id, t.start, t.end, ca.wav_sha256 FROM turns t"
            " JOIN turn_speakers ts ON ts.turn_id = t.turn_id"
            " JOIN conversation_audio ca ON ca.conversation_id = t.conversation_id"
            " WHERE ts.label = 'owner' AND ts.basis IN ('voice', 'channel') AND t.end - t.start >= ?"
            " ORDER BY t.conversation_id, t.start",
            (MIN_TURN,),
        ).fetchall()
        delivery, stutter_rows = [], []
        words, loaded = [], None
        for turn_id, conversation_id, start, end, wav_sha256 in turns:
            if conversation_id != loaded:
                # The transcript the turns were built from (hearsay/turns.py). If the
                # dev box replaced it since, this conversation waits a run.
                transcript = json.loads((transcripts_dir / f"{conversation_id}.json").read_text())
                words = [] if transcript["wav_sha256"] != wav_sha256 else sorted(
                    (w["start"], w["end"], w["word"]) for s in transcript["segments"] for w in s.get("words", []) if "start" in w)
                loaded = conversation_id
            # A turn is consecutive words of one speaker (split_turns), so these are its words.
            mine = [w for w in words if start - 0.01 <= w[0] and w[1] <= end + 0.01]
            rate = speaking_rate(mine)
            if rate is None:
                continue
            pauses = [b[0] - a[1] for a, b in zip(mine, mine[1:]) if b[0] - a[1] > PAUSE]
            delivery.append((turn_id, round(rate, 2), len(pauses), round(sum(pauses) / len(pauses), 2) if pauses else None))
            stutter_rows += [(turn_id, at, kind, round(before, 2) if before else None) for at, kind, before in stutters(mine)]
        db.executescript(SCHEMA)
        db.executemany("INSERT INTO turn_delivery VALUES (?,?,?,?)", delivery)
        db.executemany("INSERT INTO turn_stutters VALUES (?,?,?,?)", stutter_rows)
        db.commit()
    finally:
        db.close()
    return {"owner_turns": len(delivery), "stutters": len(stutter_rows)}
