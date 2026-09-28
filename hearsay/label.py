"""Label turns by ear, and report how well the thresholds match the labels.

Naming other speakers happens in the web portal (hearsay/portal.py).

Runs on the dev box, not in the container: it plays audio locally and reaches
the NAS over `ssh nas`. Labels are the operator's durable input: they are
appended to the labels dir on the NAS and never rebuilt or overwritten.

    python -m hearsay.label enroll START END   # add an enrollment window
    python -m hearsay.label                    # label turns me / not me, blind
    python -m hearsay.label report             # precision/recall, cluster health

START and END are ISO-8601 times, e.g. 2026-09-27T10:05; without a UTC
offset they are this machine's local time. Reprocess after enrolling.
"""

import json
import random
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

from hearsay.people import CLUSTER_THRESHOLD
from hearsay.speakers import NOT_OWNER_THRESHOLD, OWNER_THRESHOLD

NAS = "nas"
DB = "/mnt/storage/hearsay/db/hearsay.sqlite"
AUDIO_DIR = "/mnt/storage/hearsay/audio"
LABELS = "/mnt/storage/hearsay/labels/labels.jsonl"
ENROLLMENT = "/mnt/storage/hearsay/labels/enrollment.json"
ANSWERS = {"m": "owner", "n": "not_owner", "u": "unsure"}

CANDIDATES_SQL = """
SELECT t.turn_id, t.conversation_id, t.start, t.end, ts.owner_similarity, ca.wav_file
FROM turn_speakers ts
JOIN turns t ON t.turn_id = ts.turn_id
JOIN conversation_audio ca ON ca.conversation_id = t.conversation_id
WHERE ts.embedding IS NOT NULL
"""

# The operator's labels and tags as reprocess matched them to turns.
LABELS_SQL = "SELECT turn_id, value FROM operator_input WHERE kind = 'label'"
REVIEWED_SQL = "SELECT turn_id, kind FROM operator_input WHERE kind IN ('name', 'mixed')"

CLUSTERS_SQL = """
SELECT turn_id, cluster, person, name_conflict
FROM turn_people
"""

# Runs on the NAS: writes one turn of a conversation WAV to stdout as WAV.
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


def query(sql: str) -> list[dict]:
    out = ssh(["sqlite3", "-json", DB, f'"{" ".join(sql.split())}"'])
    return json.loads(out or b"[]")


def candidates() -> list[dict]:
    return query(CANDIDATES_SQL)


def clusters() -> dict[str, list[dict]]:
    """cluster -> its turns, largest cluster first."""
    grouped = {}
    for row in query(CLUSTERS_SQL):
        grouped.setdefault(row["cluster"], []).append(row)
    return dict(sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0])))


def existing_labels() -> dict[str, str]:
    """turn_id -> label, as of the last reprocess."""
    return {row["turn_id"]: row["value"] for row in query(LABELS_SQL)}


def play(turn: dict) -> None:
    wav = ssh(["python3", "-", f"{AUDIO_DIR}/{turn['wav_file']}", str(turn["start"]), str(turn["end"])],
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
    unlabeled = [s for s in candidates() if s["turn_id"] not in labeled]
    print(f"{len(labeled)} labeled, {len(unlabeled)} to go.")
    print("m = me, n = not me, u = unsure, r = replay, q = quit")
    while unlabeled:
        turn = pick(unlabeled)
        print(f"\n{turn['end'] - turn['start']:.1f} s")
        play(turn)
        answer = ""
        while answer not in ANSWERS:
            answer = input("> ").strip().lower()
            if answer == "q":
                return
            if answer == "r":
                play(turn)
        # The span, not just the id, so the label survives re-transcription.
        record = {
            "turns": [{k: turn[k] for k in ("turn_id", "conversation_id", "start", "end")}],
            "label": ANSWERS[answer],
            "labeled_at": datetime.now(timezone.utc).isoformat(),
        }
        ssh(["sh", "-c", f"'cat >> {LABELS}'"], (json.dumps(record) + "\n").encode())
        unlabeled.remove(turn)


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
    rows = [(s["owner_similarity"], labeled[s["turn_id"]]) for s in candidates()
            if s["turn_id"] in labeled and s["owner_similarity"] is not None]
    rows = [(sim, label) for sim, label in rows if label != "unsure"]
    owners = sum(1 for _, label in rows if label == "owner")
    print(f"{len(rows)} labeled turns with scores ({owners} owner, {len(rows) - owners} not owner)")
    print(f"thresholds in this checkout: owner >= {OWNER_THRESHOLD}, not_owner <= {NOT_OWNER_THRESHOLD}\n")
    print("owner if similarity >= t        not_owner if similarity <= t")
    print("   t  labeled  precision recall     t  labeled  precision recall")
    for i in range(11):
        t_owner, t_not = 0.3 + i * 0.05, 0.1 + i * 0.04
        print(f"{line(rows, 'owner', lambda sim: sim >= t_owner, t_owner)}   "
              f"{line(rows, 'not_owner', lambda sim: sim <= t_not, t_not)}")
    cluster_report()


def cluster_report() -> None:
    try:
        grouped = clusters()
    except subprocess.CalledProcessError:
        print("\nno clusters yet (reprocess with this code first)")
        return
    reviewed_rows = query(REVIEWED_SQL)
    named = {r["turn_id"] for r in reviewed_rows if r["kind"] == "name"}
    marked_mixed = {r["turn_id"] for r in reviewed_rows if r["kind"] == "mixed"}
    people = {}
    for c, segs in grouped.items():
        if segs[0]["person"]:
            people.setdefault(segs[0]["person"], []).append(c)
    reviewed = [segs for segs in grouped.values()
                if any(s["turn_id"] in named or s["turn_id"] in marked_mixed for s in segs)]
    mixed_now = [segs for segs in reviewed if marked_mixed & {s["turn_id"] for s in segs}]
    sizes = sorted((len(s) for s in grouped.values()), reverse=True)
    print(f"\nanonymous speakers (cluster threshold in this checkout: {CLUSTER_THRESHOLD})")
    print(f"  {len(grouped)} clusters, sizes {sizes[:12]}{' ...' if len(sizes) > 12 else ''}")
    print(f"  {len(people)} named people over {sum(len(c) for c in people.values())} clusters")
    print(f"  reviewed clusters marked mixed: {len(mixed_now)}/{len(reviewed)}  (many: raise the threshold)")
    spread = {p: len(c) for p, c in people.items() if len(c) > 1}
    print(f"  people spread over several clusters: {spread or 'none'}  (many: lower the threshold)")
    conflicts = sum(1 for segs in grouped.values() if segs[0]["name_conflict"])
    print(f"  clusters with conflicting names: {conflicts}")


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
