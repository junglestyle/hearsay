from hearsay.hints import report


def tag(at: str, name: str, turn: str, **extra) -> dict:
    """A name tag on one 30 s turn of 2026-10-10 (all at the same time), tagged at 2026-10-12 `at`."""
    return {"type": "name", "name": name, "turn_ids": [turn],
            "at": [["2026-10-10T01:00:00+00:00", "2026-10-10T01:00:30+00:00"]],
            "tagged_at": f"2026-10-12T{at}+00:00", **extra}


def test_hints_taken_passed_over_wrong_and_undone():
    tags = [
        tag("10:00:00", "Sam", "t1", via="place", offered={"place": ["Sam", "Ana"]}),
        tag("10:00:20", "Ana", "t2", offered={"place": ["Sam", "Ana"]}),  # typed though offered; talking over Sam
        tag("10:00:50", "Lee", "t3", offered={"place": ["Sam"]}),  # not among those offered
        tag("10:01:00", "_media", "t4", via="media", offered={"media": ["_media"]}),
        tag("10:01:30", "Joe", "t1"),  # Sam's turn was someone else: undone
        {"type": "name", "name": "Old", "at": [], "tagged_at": "2026-10-01T10:00:00+00:00"},  # before the hints
    ]
    found = report(tags)
    place, media = found["place"], found["media"]
    assert (place["named"], place["taken"], place["typed_while_offered"], place["not_offered"]) == (4, 1, 1, 1)
    assert place["undone"] == 1 and media["undone"] == 0  # the media turn overlaps others in time only
    assert (media["named"], media["taken"]) == (1, 1)
