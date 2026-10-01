from hearsay.conversations import GAP, MIN_SPEECH, TAP_WINDOW, conversation_id, group_speech

T = 1_790_000_000.0  # unix seconds


def test_silence_longer_than_the_gap_splits_and_short_talk_is_dropped():
    speech = [
        # One conversation: 40 s of speech with pauses just under the gap.
        (T, T + 20), (T + 20 + GAP - 1, T + 40 + GAP - 1),
        # After more than GAP of silence, a stray 10 s remark: too little speech.
        (T + 40 + 3 * GAP, T + 50 + 3 * GAP),
        # Then a second real conversation, given out of order.
        (T + 10_000 + 20, T + 10_000 + 40), (T + 10_000, T + 10_000 + 15),
    ]
    assert group_speech(speech) == [
        (T, T + 40 + GAP - 1, 40.0),
        (T + 10_000, T + 10_000 + 40, 35.0),
    ]
    assert MIN_SPEECH > 10


def test_conversation_ids_come_from_the_first_speech_so_they_are_stable_as_it_grows():
    assert conversation_id(T) == conversation_id(T + 0.9) == "c20260921T141320Z"


def test_a_tap_keeps_a_short_self_note_but_only_near_it():
    note = (T, T + 8)
    remark = (T + 5 * GAP, T + 5 * GAP + 8)
    # Tapped just before speaking, or just after finishing: kept either way.
    for tap in (T - TAP_WINDOW + 1, T + 8 + TAP_WINDOW - 1):
        assert group_speech([note, remark], [tap]) == [(T, T + 8, 8.0)]
    assert group_speech([note, remark], [T - TAP_WINDOW - 1]) == []


def test_an_end_mark_splits_however_soon_speech_resumes():
    first, second = (T, T + 40), (T + 60, T + 100)
    assert group_speech([first, second]) == [(T, T + 100, 80.0)]
    assert group_speech([first, second], ends=[T + 50]) == [(T, T + 40, 40.0), (T + 60, T + 100, 40.0)]
    # Pressed mid-sentence: the speech under way stays with what came before.
    assert group_speech([first, second], ends=[T + 20]) == [(T, T + 40, 40.0), (T + 60, T + 100, 40.0)]
