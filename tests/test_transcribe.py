from hearsay.transcribe import PIECE_SECONDS, label_segments, pieces
from hearsay.turns import split_turns


def test_pieces_cover_the_whole_conversation_and_cut_in_pauses():
    # Two people taking turns for 20 minutes, a 2 s pause after every 8 s of speech.
    speech = [(10.0 * i, 10.0 * i + 8, f"SPEAKER_0{i % 2}") for i in range(120)]
    spans = pieces(1200.0, speech)

    assert spans[0][0] == 0.0 and spans[-1][1] == 1200.0
    assert all(a[1] == b[0] for a, b in zip(spans, spans[1:]))  # no gap, no overlap
    assert all(end - start <= PIECE_SECONDS for start, end in spans)
    assert all(cut % 10 == 9.0 for _, cut in spans[:-1])  # in the middle of a pause, never mid-word

    # Speech with no pause at all is still cut, at the limit.
    assert pieces(700.0, [(0.0, 700.0, "SPEAKER_00")]) == [(0.0, 300.0), (300.0, 600.0), (600.0, 700.0)]


def test_words_go_to_the_speaker_they_overlap_and_become_turns():
    speech = [(0.0, 2.0, "SPEAKER_00"), (2.0, 4.0, "SPEAKER_01")]
    words = [{"word": "Hello", "start": 0.2, "end": 0.6, "score": 0.9},
             {"word": "there.", "start": 0.7, "end": 1.1, "score": 0.8},
             {"word": "Hi!", "start": 2.3, "end": 2.6, "score": 0.95},
             # Past the diarized speech: no speaker of its own, so its sentence's.
             {"word": "Bye.", "start": 4.2, "end": 4.5, "score": 0.7}]
    sentences = [{"start": 0.2, "end": 1.1, "text": "Hello there."},
                 {"start": 2.3, "end": 4.5, "text": "Hi! Bye."}]

    turns = split_turns(label_segments(words, sentences, speech))

    assert [(t["speaker"], " ".join(t["words"])) for t in turns] == [("SPEAKER_00", "Hello there."),
                                                                      ("SPEAKER_01", "Hi! Bye.")]
    assert turns[0]["scores"] == [0.9, 0.8]
