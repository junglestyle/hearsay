"""How the portal's one-tap hints are doing (roadmap slices 14 and 15), from
the operator's tags alone.

A hint is taken when the operator names a cluster with it (the tag says
"via": "voice", "place" or "media"). Since name tags also carry what each
hint "offered", a name typed while the hint offered it counts as a missed
tap, and a name that wasn't among those it offered as the hint being wrong. A taken hint is
undone when a later tag gives any of its stretches another name.

Standard library only, so it runs on the NAS as is:

    python3 -m hearsay.hints /mnt/storage/hearsay/labels/tags.jsonl
"""

import json
import statistics
import sys
from datetime import datetime
from itertools import pairwise
from pathlib import Path

# When each hint was first offered; earlier tags say nothing about them.
SHIPPED = {"voice": datetime.fromisoformat("2026-10-13T19:00:00+00:00"),
           "place": datetime.fromisoformat("2026-10-07T21:06:52+00:00"),
           "media": datetime.fromisoformat("2026-10-07T21:20:11+00:00")}
# A gap longer than this between tags is a new sitting, not time spent naming.
SITTING = 600


def report(tags: list[dict]) -> dict[str, dict]:
    for tag in tags:
        tag["when"] = datetime.fromisoformat(tag["tagged_at"])
    person = lambda name: not name.startswith("_")
    return {"voice": measure(tags, "voice", person),
            "place": measure(tags, "place", person),
            "media": measure(tags, "media", lambda name: name == "_media")}


def measure(tags: list[dict], via: str, counts) -> dict:
    """Namings the hint could have made (a person for place, _media for media)
    since it shipped, how they were made, and how long each took."""
    named = [t for t in tags if t["type"] == "name" and t.get("name") and counts(t["name"])
             and t["when"] >= SHIPPED[via]]
    taken = [t for t in named if t.get("via") == via]
    typed = [t for t in named if t.get("via") != via]
    # What this hint offered, where the tag says (it has since 2026-10-13).
    known = [(t, t["offered"].get(via, [])) for t in named if isinstance(t.get("offered"), dict)]
    return {
        "named": len(named),
        "taken": len(taken),
        "typed": len(typed),
        "typed_while_offered": sum(t["name"] in offered for t, offered in known if t.get("via") != via),
        "not_offered": sum(t["name"] not in offered for t, offered in known),
        "with_offers": len(known),
        "undone": sum(undone(t, tags) for t in taken),
        "seconds_taken": median_gap(taken, tags),
        "seconds_typed": median_gap(typed, tags),
    }


def undone(taken: dict, tags: list[dict]) -> bool:
    """A later name or clip tag that puts another name (or none) on any of its turns."""
    later = [t for t in tags if t["when"] > taken["when"] and t["type"] in ("name", "clip")]
    return any(t.get("name") != taken["name"] and same_turns(t, taken) for t in later)


def same_turns(a: dict, b: dict) -> bool:
    """By turn id where both have them: overlapping times alone also catch
    people talking over each other. Ids are renumbered by re-transcription,
    which a correction made within days rarely straddles."""
    if a.get("turn_ids") and b.get("turn_ids"):
        return bool(set(a["turn_ids"]) & set(b["turn_ids"]))
    return any(s1 < e2 and s2 < e1 for s1, e1 in a["at"] for s2, e2 in b["at"])


def median_gap(chosen: list[dict], tags: list[dict]) -> float | None:
    """Seconds since the operator's previous tag, within a sitting: the time
    from one cluster's answer to the next, listening included."""
    order = sorted(tags, key=lambda t: t["when"])
    previous = {id(b): a for a, b in pairwise(order)}
    gaps = [(t["when"] - previous[id(t)]["when"]).total_seconds() for t in chosen if id(t) in previous]
    gaps = [g for g in gaps if g <= SITTING]
    return statistics.median(gaps) if gaps else None


def seconds(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}"


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "/mnt/storage/hearsay/labels/tags.jsonl")
    tags = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    for via, found in report(tags).items():
        print(f"{via} hint (since {SHIPPED[via]:%Y-%m-%d}): {found['named']} named,"
              f" {found['taken']} with the hint, {found['typed']} typed")
        if found["with_offers"]:
            print(f"  of {found['with_offers']} named with offers recorded: {found['typed_while_offered']}"
                  f" typed though offered, {found['not_offered']} not among those offered")
        print(f"  undone later: {found['undone']} of {found['taken']} taken")
        if found["seconds_taken"] is not None or found["seconds_typed"] is not None:
            print(f"  median seconds per naming: with the hint {seconds(found['seconds_taken'])},"
                  f" typed {seconds(found['seconds_typed'])}")


if __name__ == "__main__":
    main()
