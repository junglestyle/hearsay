from hearsay.conversations import GAP, MIN_SPEECH, conversation_id, group_speech

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
