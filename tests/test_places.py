import json
import sqlite3
import struct
from datetime import datetime, timedelta, timezone

from hearsay.affect import SCHEMA as AFFECT_SCHEMA
from hearsay.assemble import SCHEMA as ASSEMBLE_SCHEMA
from hearsay.capture import LOCATION
from hearsay.conversations import SCHEMA as CONVERSATIONS_SCHEMA
from hearsay.parse import rebuild
from hearsay.people import SCHEMA as PEOPLE_SCHEMA
from hearsay.places import find_places
from hearsay.speakers import SCHEMA as SPEAKERS_SCHEMA
from hearsay.stream import write_stream
from hearsay.turns import SCHEMA as TURNS_SCHEMA
from test_capture import record, upload

T = datetime(2026, 10, 6, 20, 0, tzinfo=timezone.utc)
BAR = (26.12000, -80.14000)
HOME = (26.13000, -80.15000)  # about 1.5 km from the bar


def fix(at, lat, lon, accuracy=65.0):
    return record(at, LOCATION, struct.pack("<ddf", lat, lon, accuracy))


def test_a_conversation_lists_the_named_places_it_happened_at(tmp_path):
    raw, db_path, labels, stream = tmp_path / "raw", tmp_path / "h.sqlite", tmp_path / "labels", tmp_path / "stream"
    labels.mkdir()
    t0 = T.timestamp()
    # The phone's readings: at the bar from before the conversation starts,
    # 40 m off on one reading, then somewhere unnamed, then home.
    readings = [fix(t0 - 60, *BAR), fix(t0 + 60, BAR[0] + 0.00036, BAR[1]), fix(t0 + 600, 26.2, -80.2),
                fix(t0 + 900, *HOME)]
    upload(raw, T + timedelta(minutes=20), "k-places", b"".join(readings))
    rebuild(raw, db_path)
    db = sqlite3.connect(db_path)
    db.executescript(ASSEMBLE_SCHEMA + CONVERSATIONS_SCHEMA + TURNS_SCHEMA + SPEAKERS_SCHEMA + PEOPLE_SCHEMA + AFFECT_SCHEMA)
    db.execute("INSERT INTO conversations VALUES ('c1', ?, ?, 600, 0)", (t0, t0 + 1200))
    db.execute("INSERT INTO conversations VALUES ('c2', ?, ?, 60, 0)", (t0 + 7200, t0 + 7300))  # no readings
    db.commit()
    db.close()
    (labels / "places.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
        {"type": "place", "name": "Chill", "latitude": BAR[0], "longitude": BAR[1]},
        {"type": "place", "name": "home", "latitude": HOME[0], "longitude": HOME[1]},
        {"type": "rename", "from": "Chill", "to": "Chill Room"},  # retroactive, like people's names
    ]))

    find_places(db_path, labels)
    write_stream(db_path, stream)

    index = {e["conversation_id"]: e for e in json.loads((stream / "index.json").read_text())["conversations"]}
    assert index["c1"]["places"] == [
        {"name": "Chill Room", "start": "2026-10-06T20:00:00.000Z", "end": "2026-10-06T20:10:00.000Z"},
        {"name": "home", "start": "2026-10-06T20:15:00.000Z", "end": "2026-10-06T20:20:00.000Z"},
    ]
    assert index["c2"]["places"] == []
    assert "26.1" not in (stream / "index.json").read_text()  # never coordinates
