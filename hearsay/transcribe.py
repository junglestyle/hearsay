"""Transcribe and diarize conversation WAVs on the dev box GPU, and write the
transcripts back to the NAS.

Runs on the dev box, not the NAS, because only the dev box has a GPU
(install/gpu.sh sets up the environment and an hourly systemd timer). Audio
moves from the NAS to the dev box, both inside Hearsay's boundary, and is only
kept in a temp dir while it's transcribed.

Words and their timings come from Parakeet TDT v3, speakers from pyannote's
diarization; each word goes to the speaker it overlaps most. Parakeet replaced
WhisperX's Whisper large-v3 on 2026-10-06. On 6.5 hours of real turns Whisper
put "Thank you."-type lines on noise 57 times to Parakeet's 17, once repeated
a line four times over speech it had dropped, and translated Spanish into
English where Parakeet wrote Spanish. In a blind listening check of 24 turns
Parakeet Ultra (this model post-trained; the two agreed on 80% of words) was
closer where it and Whisper disagreed most (7 to 3), but skipped some short
interjections Whisper caught (3 of 6 where one side was empty).

A transcript records the sha256 of the WAV it was made from and the settings
used, including the words Parakeet was nudged toward (hearsay/words.py).
Reprocess only uses it for that exact WAV. When either changes, the
conversation is transcribed again on the next run.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

from hearsay.words import boosted

NAS = "nas"
DB = "/mnt/storage/hearsay/db/hearsay.sqlite"
AUDIO_DIR = "/mnt/storage/hearsay/audio"
TRANSCRIPTS_DIR = "/mnt/storage/hearsay/transcripts"
WORDS = "/mnt/storage/hearsay/labels/words.json"
DEVICE = "cuda"
SETTINGS = {
    # Detects the language itself; it has no setting to force one.
    "asr_model": "nvidia/parakeet-tdt-0.6b-v3",
    "diarization_model": "pyannote/speaker-diarization-community-1",
    # VBx's speaker prior: lower keeps more speakers. The model's own 0.8 gave
    # several people one label (a dinner's four people under two). Re-run on
    # 18 real conversations (2026-10-01), 0.6 cut operator-tagged speech under
    # the wrong name from 49 s to 9 s and owner speech sharing a speaker with
    # others from 260 s to 126 s, split the dinner into the three voices
    # people.voices() hears, and left clean conversations' speaker counts
    # alone. The clustering threshold (0.5) also merged two people, and Fa
    # past 0.09 shatters speakers. Over-splitting is cheap: people.py
    # re-merges a person split across speakers.
    "diarization_Fb": 0.6,
}
# Parakeet transcribes a conversation in pieces of at most this long, cut in
# pauses: its attention's memory grows with the square of the audio's length
# (a 3-hour conversation in one pass wanted 32 GB), and NVIDIA's local-attention
# workaround lost punctuation and words on real conversations.
PIECE_SECONDS = 300
# How hard Parakeet leans toward the operator's words (NeMo's GPU phrase
# boosting; its default, 0, is off). Not measured yet: a cautious start,
# since boosting too hard hears the words where they weren't said.
BOOST_ALPHA = 0.5

# Runs on the NAS: prints {conversation_id: [wav_sha256, settings]} for the transcripts there.
EXISTING_SCRIPT = """
import glob, json, os, sys
out = {}
for path in glob.glob(os.path.join(sys.argv[1], "*.json")):
    with open(path) as f:
        t = json.load(f)
    out[t["conversation_id"]] = [t["wav_sha256"], t["settings"]]
print(json.dumps(out))
"""


def ssh(command: list[str], stdin: bytes = b"") -> bytes:
    return subprocess.run(["ssh", "-o", "BatchMode=yes", NAS, *command], input=stdin,
                          capture_output=True, check=True).stdout


def current_settings() -> dict:
    """SETTINGS, plus the words committed on the portal's Words page. Without
    any, exactly SETTINGS, so transcripts from before there were words still count."""
    committed = ssh(["sh", "-c", f"'cat {WORDS} 2>/dev/null || true'"])
    words = boosted(json.loads(committed)) if committed.strip() else []
    return SETTINGS | ({"boost_words": words, "boost_alpha": BOOST_ALPHA} if words else {})


def pending(settings: dict) -> list[dict]:
    sql = ("SELECT conversation_id, wav_file, wav_sha256 FROM conversation_audio"
           " WHERE wav_file IS NOT NULL AND wav_sha256 IS NOT NULL ORDER BY conversation_id")
    rows = json.loads(ssh(["sqlite3", "-json", DB, f'"{sql}"']) or b"[]")
    existing = json.loads(ssh(["python3", "-", TRANSCRIPTS_DIR], EXISTING_SCRIPT.encode()))
    return [r for r in rows if existing.get(r["conversation_id"]) != [r["wav_sha256"], settings]]


def load_models(hf_token: str, settings: dict):
    # Imported here: these are only installed in the GPU environment.
    import nemo.collections.asr as nemo_asr
    import torch
    from omegaconf import open_dict
    from pyannote.audio import Pipeline

    asr = nemo_asr.models.ASRModel.from_pretrained(SETTINGS["asr_model"]).to(DEVICE).eval()
    # A confidence per token, the probability of the token chosen, so turns
    # keep a text confidence. Its own scale: words average about 0.93 on real
    # conversations, where WhisperX's alignment scores ran 0.4-0.9. NeMo's
    # default measure (entropy) runs near 0.1, and its own word-level
    # aggregation fails on some transcripts.
    decoding = asr.cfg.decoding
    with open_dict(decoding):
        decoding.confidence_cfg = {"preserve_token_confidence": True, "method_cfg": {"name": "max_prob"}}
        if settings.get("boost_words"):
            # Phrase boosting needs the batched greedy decoder, Parakeet's default.
            decoding.strategy = "greedy_batch"
            decoding.greedy.boosting_tree = {"key_phrases_list": settings["boost_words"]}
            decoding.greedy.boosting_tree_alpha = settings["boost_alpha"]
    asr.change_decoding_strategy(decoding)

    diarize = Pipeline.from_pretrained(SETTINGS["diarization_model"], token=hf_token).to(torch.device(DEVICE))
    diarize.clustering.Fb = SETTINGS["diarization_Fb"]
    return asr, diarize


def load_wav(wav_path: Path):
    import numpy as np

    with wave.open(str(wav_path)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2), "assemble writes 16 kHz mono"
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768


def word_scores(hyp) -> list[float | None]:
    """Each word's confidence: its least likely token. Tokens and words share
    frame offsets in Parakeet's timestamps."""
    tokens = [(t["start_offset"], c) for t, c in zip(hyp.timestamp["char"], hyp.token_confidence or [])]
    scores, k = [], 0
    for w in hyp.timestamp["word"]:
        while k < len(tokens) and tokens[k][0] < w["start_offset"]:
            k += 1
        inside = []
        while k < len(tokens) and tokens[k][0] < max(w["end_offset"], w["start_offset"] + 1):
            inside.append(tokens[k][1])
            k += 1
        scores.append(round(float(min(inside)), 3) if inside else None)
    return scores


def speaker_of(start: float, end: float, speech: list[tuple[float, float, str]]) -> str | None:
    """The diarized speaker overlapping start-end most, as WhisperX assigned words."""
    overlap = {}
    for s, e, speaker in speech:
        shared = min(e, end) - max(s, start)
        if shared > 0:
            overlap[speaker] = overlap.get(speaker, 0.0) + shared
    return max(overlap, key=overlap.get) if overlap else None


def pieces(duration: float, speech: list[tuple[float, float, str]]) -> list[tuple[float, float]]:
    """Spans covering the whole conversation, at most PIECE_SECONDS each, cut in the
    middle of the pauses between diarized speech (or anywhere, if a stretch has none)."""
    pauses, reach = [], 0.0
    for start, end, _ in sorted(speech):
        if start > reach:
            pauses.append((reach + start) / 2)
        reach = max(reach, end)
    spans, start = [], 0.0
    while duration - start > PIECE_SECONDS:
        inside = [p for p in pauses if start < p <= start + PIECE_SECONDS]
        cut = inside[-1] if inside else start + PIECE_SECONDS
        spans.append((start, cut))
        start = cut
    return spans + [(start, duration)]


def label_segments(words: list[dict], sentences: list[dict], speech: list[tuple[float, float, str]]) -> list[dict]:
    """Transcript segments, one per sentence, with each word's diarized speaker."""
    segments = [{"start": s["start"], "end": s["end"], "text": s["text"], "speaker": speaker_of(s["start"], s["end"], speech),
                 "words": []} for s in sentences]
    k = 0
    for w in words:
        while k + 1 < len(segments) and w["start"] >= segments[k + 1]["start"]:
            k += 1
        speaker = speaker_of(w["start"], w["end"], speech)
        segments[k]["words"].append({**w, "speaker": speaker} if speaker else w)
    return segments


def transcribe(models, wav_path: Path) -> list[dict]:
    import torch

    asr, diarize = models
    audio = load_wav(wav_path)
    out = diarize({"waveform": torch.from_numpy(audio)[None], "sample_rate": 16000})
    speech = [(turn.start, turn.end, speaker) for turn, _, speaker in out.speaker_diarization.itertracks(yield_label=True)]
    words, sentences = [], []
    for start, end in pieces(len(audio) / 16000, speech):
        [hyp] = asr.transcribe([audio[int(start * 16000):int(end * 16000)]], timestamps=True, batch_size=1, verbose=False)
        for w, score in zip(hyp.timestamp["word"], word_scores(hyp)):
            words.append({"word": w["word"], "start": start + w["start"], "end": start + w["end"]}
                         | ({"score": score} if score is not None else {}))
        sentences += [{"start": start + s["start"], "end": start + s["end"], "text": s["segment"]} for s in hyp.timestamp["segment"]]
    if not sentences:
        return []
    return label_segments(words, sentences, speech)


def main() -> None:
    hf_token = os.environ.get("HF_TOKEN")
    if not hf_token:
        sys.exit("HF_TOKEN is not set (it lives in ~/.config/hearsay/dev.env)")
    settings = current_settings()
    todo = pending(settings)
    print(f"{len(todo)} conversation(s) to transcribe.")
    if not todo:
        return

    models = load_models(hf_token, settings)
    failed = 0
    for row in todo:
        conversation_id = row["conversation_id"]
        with tempfile.TemporaryDirectory() as tmp:
            wav_path = Path(tmp) / "audio.wav"
            wav_path.write_bytes(ssh(["cat", f"{AUDIO_DIR}/{row['wav_file']}"]))
            if hashlib.sha256(wav_path.read_bytes()).hexdigest() != row["wav_sha256"]:
                # Reprocess rewrote the WAV since we listed it; the next run picks it up.
                print(f"  {conversation_id}: WAV changed underneath us, skipped")
                continue
            try:
                segments = transcribe(models, wav_path)
            except Exception as e:  # one bad conversation shouldn't stop the rest
                print(f"  {conversation_id}: failed: {e!r}")
                failed += 1
                continue
        transcript = {"conversation_id": conversation_id, "wav_sha256": row["wav_sha256"],
                      "settings": settings, "segments": segments}
        target = f"{TRANSCRIPTS_DIR}/{conversation_id}.json"
        ssh(["sh", "-c", f"'cat > {target}.tmp && mv {target}.tmp {target}'"], json.dumps(transcript).encode())
        print(f"  {conversation_id}: {len(segments)} segments")
    if failed:
        sys.exit(f"{failed} conversation(s) failed")


if __name__ == "__main__":
    main()
