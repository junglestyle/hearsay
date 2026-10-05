"""Transcribe and diarize conversation WAVs on the dev box GPU, and write the
transcripts back to the NAS.

Runs on the dev box, not the NAS, because only the dev box has a GPU
(install/gpu.sh sets up the environment and an hourly systemd timer). Audio
moves from the NAS to the dev box, both inside Hearsay's boundary, and is only
kept in a temp dir while it's transcribed.

A transcript records the sha256 of the WAV it was made from and the settings
used. Reprocess only uses it for that exact WAV. When either changes, the
conversation is transcribed again on the next run.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

NAS = "nas"
DB = "/mnt/storage/hearsay/db/hearsay.sqlite"
AUDIO_DIR = "/mnt/storage/hearsay/audio"
TRANSCRIPTS_DIR = "/mnt/storage/hearsay/transcripts"
DEVICE = "cuda"
SETTINGS = {
    "asr_model": "large-v3",
    "compute_type": "float16",
    "language": "en",
    "batch_size": 16,
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


def pending() -> list[dict]:
    sql = ("SELECT conversation_id, wav_file, wav_sha256 FROM conversation_audio"
           " WHERE wav_file IS NOT NULL AND wav_sha256 IS NOT NULL ORDER BY conversation_id")
    rows = json.loads(ssh(["sqlite3", "-json", DB, f'"{sql}"']) or b"[]")
    existing = json.loads(ssh(["python3", "-", TRANSCRIPTS_DIR], EXISTING_SCRIPT.encode()))
    return [r for r in rows if existing.get(r["conversation_id"]) != [r["wav_sha256"], SETTINGS]]


def load_models(hf_token: str):
    # Imported here: these are only installed in the GPU environment.
    import whisperx
    from whisperx.diarize import DiarizationPipeline

    asr = whisperx.load_model(SETTINGS["asr_model"], DEVICE, compute_type=SETTINGS["compute_type"],
                              language=SETTINGS["language"])
    align = whisperx.load_align_model(language_code=SETTINGS["language"], device=DEVICE)
    diarize = DiarizationPipeline(model_name=SETTINGS["diarization_model"], token=hf_token, device=DEVICE)
    diarize.model.clustering.Fb = SETTINGS["diarization_Fb"]
    return asr, align, diarize


def transcribe(models, wav_path: Path) -> list[dict]:
    import whisperx

    asr, (align_model, align_meta), diarize = models
    audio = whisperx.load_audio(str(wav_path))
    result = asr.transcribe(audio, batch_size=SETTINGS["batch_size"])
    aligned = whisperx.align(result["segments"], align_model, align_meta, audio, DEVICE)
    labeled = whisperx.assign_word_speakers(diarize(audio), aligned)
    word_keys = ("word", "start", "end", "score", "speaker")
    return [
        {
            "start": s["start"],
            "end": s["end"],
            "text": s["text"],
            "speaker": s.get("speaker"),
            "words": [{k: w[k] for k in word_keys if k in w} for w in s.get("words", [])],
        }
        for s in labeled["segments"]
    ]


def main() -> None:
    hf_token = os.environ.get("HF_TOKEN")
    if not hf_token:
        sys.exit("HF_TOKEN is not set (it lives in ~/.config/hearsay/dev.env)")
    todo = pending()
    print(f"{len(todo)} conversation(s) to transcribe.")
    if not todo:
        return

    models = load_models(hf_token)
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
                      "settings": SETTINGS, "segments": segments}
        target = f"{TRANSCRIPTS_DIR}/{conversation_id}.json"
        ssh(["sh", "-c", f"'cat > {target}.tmp && mv {target}.tmp {target}'"], json.dumps(transcript).encode())
        print(f"  {conversation_id}: {len(segments)} segments")
    if failed:
        sys.exit(f"{failed} conversation(s) failed")


if __name__ == "__main__":
    main()
