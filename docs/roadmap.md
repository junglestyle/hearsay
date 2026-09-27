# Hearsay roadmap

Slices are done in order. Each one should be usable on its own before
the next one starts. Don't build ahead.

## 1. Webhook receiver

FastAPI endpoint behind Cloudflare Tunnel. Validates a shared secret and
writes every request body verbatim to disk with a timestamp and webhook
type. No parsing, no schema, no database.

Done when: real Omi webhooks (transcript, audio bytes, memory created)
are landing on the NAS, and we know which ones come from the phone vs.
Omi's cloud.

## 2. Parse from reality

Write parsers and a schema from the captured payloads, not from Omi's
docs. Captured payloads become test fixtures. Store parsed records in
SQLite. Reprocessing from raw payloads must produce the same result.

Done when: every captured payload type parses, and a full reprocess
from raw is one command.

## 3. Audio assembly

Stitch audio-byte chunks into per-conversation audio files. Handle
gaps, out-of-order chunks, and duplicate deliveries.

Done when: each conversation has one playable audio file that lines up
with its transcript timestamps.

## 4. Me vs. not-me

Enroll the owner's voice. Compute speaker embeddings per diarized
segment and label each as owner or not-owner with a tunable threshold.
Tune for precision; ambiguous segments stay unlabeled.

Done when: owner speech is labeled reliably on real conversations, and
misses are rare enough to ignore.

## 5. Anonymous speakers and tagging

Cluster non-owner embeddings into anonymous speakers. Support tagging a
cluster with a name and merging clusters. Tags and merges apply
retroactively to all past utterances.

Done when: tagging a speaker once relabels their past conversations.

## 6. Retention

Delete non-owner audio after embedding and transcription. Starts only
once tagging is reliable: until then all audio is kept, because deleted
audio can't be re-embedded or re-tagged. First resolve how this fits
"raw payloads are never deleted" and "everything re-runs from raw".

Done when: non-owner audio is actually gone from disk, and nothing that
depends on it breaks.

## After slice 6

Expose the utterance stream (conversation_id, timestamps, speaker,
text, confidence) for downstream consumers. Idea Machine is the first
one.

## Maybe later

- Re-transcribe with our own ASR if Omi's is weak
- Replace webhooks with direct BLE capture from the pendant
- Self-hosted capture path so audio never touches Omi's cloud
