"""Where each conversation happened: the phone's location readings, matched to
the places the operator has named.

The iPhone app samples the owner's location while it records (LOCATION
records, hearsay/capture.py): once when recording starts, then every minute,
at about 100 m accuracy. Only named places reach the stream, never
coordinates: a reading is at the nearest named place within RADIUS, and a
reading near no named place says nothing. Names are the operator's durable
input in labels/places.jsonl, matched by distance on every reprocess, so
naming a place applies to every past conversation there.

labels/places.jsonl, one record per line:
    {"type": "place", "name", "latitude", "longitude"}  a named spot; naming a
        second spot with the same name makes both that place (a big venue)
    {"type": "rename", "from", "to"}  renaming onto an existing name merges them
    {"type": "forget", "name"}  every spot with that name
"""

import bisect
import json
import math
import sqlite3
from pathlib import Path

# Metres from a named place within which a reading is at it: the readings'
# own accuracy.
RADIUS = 100.0
# The last reading this long before a conversation starts still says where it
# began: the phone samples every minute.
LOOKBACK = 120.0

SCHEMA = """
DROP TABLE IF EXISTS conversation_places;
-- Named places each conversation happened at, in order.
CREATE TABLE conversation_places (
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    place TEXT NOT NULL,
    start REAL NOT NULL,             -- unix seconds
    end REAL NOT NULL
);
"""


def metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance over the earth's surface (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(a))


def read_places(labels_dir: Path) -> list[tuple[str, float, float]]:
    """(name, latitude, longitude) for each named spot, with renames and forgets applied in order."""
    path = labels_dir / "places.jsonl"
    spots = []
    for line in (path.read_text().splitlines() if path.exists() else []):
        record = json.loads(line)
        if record["type"] == "place":
            spots.append((record["name"], record["latitude"], record["longitude"]))
        elif record["type"] == "rename":
            spots = [(record["to"] if name == record["from"] else name, lat, lon) for name, lat, lon in spots]
        elif record["type"] == "forget":
            spots = [s for s in spots if s[0] != record["name"]]
    return spots


def nearest(latitude: float, longitude: float, spots: list[tuple[str, float, float]]) -> str | None:
    found = [(metres(latitude, longitude, lat, lon), name) for name, lat, lon in spots]
    found = [f for f in found if f[0] <= RADIUS]
    return min(found)[1] if found else None


def visits(times: list[float], names: list[str | None], start: float, end: float) -> list[tuple[str, float, float]]:
    """The named places a conversation happened at, in order: (place, from, to).

    times are the readings' times in order, names their places. A place lasts
    until a reading says otherwise or the conversation ends.
    """
    first = bisect.bisect_left(times, start - LOOKBACK)
    last = bisect.bisect_right(times, end)
    # Of the readings before the start, only the latest says where it began.
    inside = bisect.bisect_left(times, start)
    first = max(first, inside - 1)
    out = []
    for at, name in zip(times[first:last], names[first:last]):
        if out and out[-1][0] == name:
            continue
        if out:
            out[-1][2] = max(at, start)
        out.append([name, max(at, start), end])
    return [(name, a, b) for name, a, b in out if name]


def find_places(db_path: Path, labels_dir: Path) -> dict:
    spots = read_places(labels_dir)
    db = sqlite3.connect(db_path)
    try:
        readings = db.execute("SELECT at, latitude, longitude FROM locations ORDER BY at").fetchall()
        times = [at for at, _, _ in readings]
        names = [nearest(lat, lon, spots) for _, lat, lon in readings]
        rows = []
        for conversation_id, start, end in db.execute("SELECT conversation_id, start, end FROM conversations"):
            rows += [(conversation_id, *visit) for visit in visits(times, names, start, end)]
        db.executescript(SCHEMA)
        db.executemany("INSERT INTO conversation_places VALUES (?,?,?,?)", rows)
        db.commit()
    finally:
        db.close()
    return {"readings": len(readings), "named_places": len({s[0] for s in spots}),
            "readings_at_a_named_place": sum(1 for n in names if n),
            "conversations_with_a_place": len({r[0] for r in rows})}


# Readings (about minutes) a spot needs before it's offered for naming.
MIN_SPOT_READINGS = 3


def unnamed_spots(readings: list[tuple[float, float, float]], spots: list[tuple[str, float, float]]) -> list[dict]:
    """Where the owner spent time that no named place is near, most time first.

    readings are (at, latitude, longitude). Each joins the first spot whose
    centre is within RADIUS, or starts one; a spot's centre is the mean of
    its readings.
    """
    found = []
    for at, lat, lon in readings:
        if nearest(lat, lon, spots):
            continue
        for spot in found:
            if metres(lat, lon, spot["latitude"], spot["longitude"]) <= RADIUS:
                spot["times"].append(at)
                n = len(spot["times"])
                spot["latitude"] += (lat - spot["latitude"]) / n
                spot["longitude"] += (lon - spot["longitude"]) / n
                break
        else:
            found.append({"latitude": lat, "longitude": lon, "times": [at]})
    found = [s for s in found if len(s["times"]) >= MIN_SPOT_READINGS]
    return sorted(found, key=lambda s: -len(s["times"]))
