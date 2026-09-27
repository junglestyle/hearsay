"""Label segments by ear, and report how well the thresholds match the labels.

Runs on the dev box, not in the container: it plays audio locally and reaches
the NAS over `ssh nas`. Labels are the operator's durable input: they are
appended to the labels dir on the NAS and never rebuilt or overwritten.

    python -m hearsay.label enroll START END   # add an enrollment window
    python -m hearsay.label                    # label segments, blind (no text, no score)
    python -m hearsay.label report             # precision/recall per threshold

START and END are ISO-8601 times, e.g. 2026-09-27T10:05; without a UTC
offset they are this machine's local time. Reprocess after enrolling.
"""

import json
import random
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

from hearsay.speakers import NOT_OWNER_THRESHOLD, OWNER_THRESHOLD

NAS = "nas"
DB = "/mnt/storage/hearsay/db/hearsay.sqlite"
AUDIO_DIR = "/mnt/storage/hearsay/audio"
LABELS = "/mnt/storage/hearsay/labels/labels.jsonl"
ENROLLMENT = "/mnt/storage/hearsay/labels/enrollment.json"
ANSWERS = {"m": "owner", "n": "not_owner", "u": "unsure"}

CANDIDATES_SQL = """
SELECT ss.segment_id, ss.owner_similarity, s.start, s.end, ca.wav_file
FROM segment_speakers ss
JOIN segments s ON s.payload_path = ss.payload_path AND s.idx = ss.idx
JOIN conversation_audio ca ON ca.conversation_id = ss.conversation_id
WHERE ss.embedding IS NOT NULL
"""

# Runs on the NAS: writes one segment of a conversation WAV to stdout as WAV.
SLICE_SCRIPT = """
import sys, wave
path, start, end = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
with wave.open(path) as src:
    rate = src.getframerate()
    src.setpos(max(0, int(start * rate)))
    frames = src.readframes(int((end - start) * rate))
out = wave.open(sys.stdout.buffer, "wb")
out.setnchannels(1); out.setsampwidth(2); out.setframerate(rate)
out.writeframes(frames); out.close()
"""


def ssh(command: list[str], stdin: bytes = b"") -> bytes:
    return subprocess.run(["ssh", NAS, *command], input=stdin, capture_output=True, check=True).stdout


def candidates() -> list[dict]:
    out = ssh(["sqlite3", "-json", DB, f'"{" ".join(CANDIDATES_SQL.split())}"'])
    return json.loads(out or b"[]")


def existing_labels() -> dict[str, str]:
    """segment_id -> label; the latest answer wins."""
    out = ssh(["sh", "-c", f"'cat {LABELS} 2>/dev/null || true'"])
    labels = {}
    for line in out.decode().splitlines():
        record = json.loads(line)
        labels[record["segment_id"]] = record["label"]
    return labels


def play(segment: dict) -> None:
    wav = ssh(["python3", "-", f"{AUDIO_DIR}/{segment['wav_file']}", str(segment["start"]), str(segment["end"])],
              SLICE_SCRIPT.encode())
    with tempfile.NamedTemporaryFile(suffix=".wav") as f:
        f.write(wav)
        f.flush()
        subprocess.run(["paplay", f.name], check=True)


def pick(unlabeled: list[dict]) -> dict:
    # Spread picks across the similarity range, so both ends and the
    # ambiguous middle get labels. Before enrollment there are no scores.
    scored = [s for s in unlabeled if s["owner_similarity"] is not None]
    if not scored:
        return random.choice(unlabeled)
    low = min(s["owner_similarity"] for s in scored)
    high = max(s["owner_similarity"] for s in scored)
    target = random.uniform(low, high)
    return min(scored, key=lambda s: abs(s["owner_similarity"] - target))


def label_loop() -> None:
    labeled = existing_labels()
    unlabeled = [s for s in candidates() if s["segment_id"] not in labeled]
    print(f"{len(labeled)} labeled, {len(unlabeled)} to go.")
    print("m = me, n = not me, u = unsure, r = replay, q = quit")
    while unlabeled:
        segment = pick(unlabeled)
        print(f"\n{segment['end'] - segment['start']:.1f} s")
        play(segment)
        answer = ""
        while answer not in ANSWERS:
            answer = input("> ").strip().lower()
            if answer == "q":
                return
            if answer == "r":
                play(segment)
        record = {
            "segment_id": segment["segment_id"],
            "label": ANSWERS[answer],
            "labeled_at": datetime.now(timezone.utc).isoformat(),
        }
        ssh(["sh", "-c", f"'cat >> {LABELS}'"], (json.dumps(record) + "\n").encode())
        unlabeled.remove(segment)


def enroll(start: str, end: str) -> None:
    window = {
        "start": datetime.fromisoformat(start).astimezone(timezone.utc).isoformat(),
        "end": datetime.fromisoformat(end).astimezone(timezone.utc).isoformat(),
    }
    if window["end"] <= window["start"]:
        sys.exit("END must be after START")
    windows = json.loads(ssh(["sh", "-c", f"'cat {ENROLLMENT} 2>/dev/null || echo []'"]))
    if window in windows:
        print("Already enrolled.")
        return
    windows.append(window)
    ssh(["sh", "-c", f"'cat > {ENROLLMENT}.tmp && mv {ENROLLMENT}.tmp {ENROLLMENT}'"],
        json.dumps(windows, indent=2).encode())
    print(f"Enrolled {window['start']} to {window['end']} ({len(windows)} window(s)). Now reprocess on the NAS.")


def report() -> None:
    try:
        built = ssh(["sqlite3", DB], b"SELECT key || '=' || value FROM reprocess_info;").decode().split()
    except subprocess.CalledProcessError:
        built = []  # the table only exists once a reprocess with this code has run
    print("database built by: " + (", ".join(built) or "unknown (reprocess predates reprocess_info)"))
    enrolled = ssh(["sqlite3", DB, '"SELECT start, end, round(coverage, 2), pieces FROM owner_enrollment"'])
    print("enrollment (start | end | coverage | 3 s pieces used):")
    print(enrolled.decode() or "  none; run `python -m hearsay.label enroll` and reprocess\n")
    labeled = existing_labels()
    rows = [(s["owner_similarity"], labeled[s["segment_id"]]) for s in candidates()
            if s["segment_id"] in labeled and s["owner_similarity"] is not None]
    rows = [(sim, label) for sim, label in rows if label != "unsure"]
    owners = sum(1 for _, label in rows if label == "owner")
    print(f"{len(rows)} labeled segments with scores ({owners} owner, {len(rows) - owners} not owner)")
    print(f"thresholds in this checkout: owner >= {OWNER_THRESHOLD}, not_owner <= {NOT_OWNER_THRESHOLD}\n")
    print("owner if similarity >= t        not_owner if similarity <= t")
    print("   t  labeled  precision recall     t  labeled  precision recall")
    for i in range(11):
        t_owner, t_not = 0.3 + i * 0.05, 0.1 + i * 0.04
        print(f"{line(rows, 'owner', lambda sim: sim >= t_owner, t_owner)}   "
              f"{line(rows, 'not_owner', lambda sim: sim <= t_not, t_not)}")


def line(rows, label, predicate, threshold) -> str:
    predicted = [actual for sim, actual in rows if predicate(sim)]
    correct = sum(1 for actual in predicted if actual == label)
    total = sum(1 for _, actual in rows if actual == label)
    precision = f"{correct / len(predicted):.2f}" if predicted else "  - "
    recall = f"{correct / total:.2f}" if total else "  - "
    return f"{threshold:.2f}  {len(predicted):7d}  {precision:>9} {recall:>6}"


if __name__ == "__main__":
    if sys.argv[1:2] == ["enroll"] and len(sys.argv) == 4:
        enroll(sys.argv[2], sys.argv[3])
    elif sys.argv[1:] == ["report"]:
        report()
    elif not sys.argv[1:]:
        label_loop()
    else:
        sys.exit(__doc__)
