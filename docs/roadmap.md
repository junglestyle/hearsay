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

## 6. Local transcription (WhisperX)

Transcribe and diarize each conversation's assembled audio with WhisperX
on the dev box GPU (inside the boundary), replacing Omi's text and speaker
turns. Word timestamps come from our own audio, so alignment is exact by
construction. Speaker labeling and clustering run on WhisperX's turns.
Carry existing ear labels and names across (e.g. by time overlap with the
Omi segments they were made on), so no operator input is lost.

Done when: every conversation's utterances come from our own transcription,
and past labels and names still apply.

## 7. Own conversation boundaries

Split the continuous audio stream into conversations ourselves (speech
and silence), instead of waiting for Omi's memory payloads. After this,
Omi is only a pipe for audio: its transcript and memory webhooks are no
longer needed.

Done when: conversations are found without any Omi transcript or memory
payload, including ones Omi never sent.

## 8. Utterance stream

Expose the utterance stream (conversation_id, timestamps, speaker, text,
confidence) for downstream consumers. Idea Machine is the first one.
Comes after slices 6 and 7 because both change where every field comes
from, and conversation ids change with slice 7.

Done when: a downstream consumer reads utterances without touching audio
or Hearsay's internals.

## 9. Own capture app

Pendant over BLE, straight to Hearsay over the tailnet. No Omi cloud, no
upsells. Replaces the webhook path. Protocol details come from Omi's
open-source firmware and apps (check the license of anything reused).
Pressing the pendant's button marks a short self-note, which is kept even
when it's shorter than a conversation.

A Mac recorder comes first, to prove the protocol, the upload and the
stored format. The iPhone app follows and reuses both.

Done when: a day of capture reaches Hearsay with Omi's cloud out of the
loop.

## 10. Zoom calls on the Mac

Record calls on the Mac: the owner's mic and the call's audio as separate
channels, uploaded the same way as pendant audio. During a call, the call
recording wins over the pendant's, and mic-channel speech is the owner's.

Done when: a Zoom call's utterances reach the stream with the owner
attributed by channel.

## 11. Retention

Delete non-owner audio after embedding and transcription. Starts only
once tagging is reliable: until then all audio is kept, because deleted
audio can't be re-embedded or re-tagged. First resolve how this fits
"raw payloads are never deleted" and "everything re-runs from raw".

Done when: non-owner audio is actually gone from disk, and nothing that
depends on it breaks.
