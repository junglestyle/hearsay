from hearsay.delivery import positions, speaking_rate, stutters


def words(spec):
    """'I@0.0 I@0.3 ...': each word 0.2 s long, starting at the given second."""
    return [(float(t), float(t) + 0.2, w) for w, t in (item.split("@") for item in spec.split())]


def test_stutters_are_found_by_timing_and_deliberate_doubles_are_not():
    turn = words("So@0.0 I@0.25 I@0.5 went@0.75 uh@1.0 wh@1.25 what@1.5 yeah,@1.75 yeah@2.0 the@3.0 the@4.0")
    found = [(at, kind) for at, kind, _, _ in stutters(turn)]
    # "the ... the" a second apart is two separate words, not a stutter.
    assert found == [(0.25, "repeat"), (1.0, "filler"), (1.25, "restart")]


def test_each_stutter_is_placed_in_the_text_so_it_can_be_dropped():
    # "5" got no timestamp, so it is in the text but not among the timed words.
    turn = words("I@0.0 I@0.25 had@0.5 uh@0.75 wh@1.0 what@1.25 friends@1.75")
    text = "I I had 5 uh wh what friends"
    where = positions(turn, text)
    dropped = {where[i] for _, _, _, i in stutters(turn)}
    assert " ".join(w for n, w in enumerate(text.split()) if n not in dropped) == "I had 5 what friends"


def test_speaking_rate_leaves_pauses_out():
    # 4 words from 0 to 2.0 s, with one pause (1.05 s, from 0.45 to 1.5): 0.95 s of speaking.
    assert round(speaking_rate(words("one@0.0 two@0.25 three@1.5 four@1.8")), 2) == round(4 / 0.95, 2)
