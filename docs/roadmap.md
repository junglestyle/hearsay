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

Amended for Idea Machine (after slice 10's first commits): `format_version`
in the index, a per-conversation `transcript_revision` that only a new
transcript changes, an append-only `forgotten.json` (empty until slice 12),
and atomic writes and `revision` stated as guarantees.

## 9. iPhone pendant app

A pared-down replacement for Omi's app: pendant over BLE, straight to
Hearsay's capture receiver over the tailnet. No Omi cloud, no upsells.
Replaces the webhook path. Native Swift (`ios/`), starting from a Mac
pendant recorder that proved the protocol, the spool and the upload format;
it was removed once the phone took over (the pendant belongs to the phone).
Protocol details come from Omi's open-source firmware (MIT).

- Nothing lost offline: the phone spools everything verbatim with arrival
  times and uploads whenever the NAS is reachable; audio the pendant stored
  while the phone was out of range is downloaded from it afterwards.
- Pause/resume monitoring. Paused audio never reaches the NAS. A switch
  decides whether it is dropped at once or kept on the phone only, marked
  paused, for a retention period (30 days to start) during which a paused
  window can be kept (uploaded like normal audio) before it is deleted.
  A window can be played on the phone and cropped to one selection before
  upload; the rest of it is deleted.
- Mute: the pendant's mic gain set to 0, which mutes it in hardware and
  holds out of range and across app crashes until unmuted (which restores
  the previous level).
- Optional conversation markers: start (keep what follows even if short,
  e.g. self-notes) and end (force a split).
- Configurable actions for the pendant's single and double tap (the
  consumer firmware reports no other gesture: holds go unreported, and 3 s
  powers it off), each action with its own haptic pattern. The firmware's
  haptic characteristic plays 100/300/500 ms buzzes; patterns are sequences
  of them.
- Server URL and token are settings, not hardwired, so the app isn't tied to
  one Hearsay install.
- Signing: a free Apple ID while building (installs expire after 7 days),
  the paid developer program once it's in daily use. Personal installs need
  no App Review; publishing is a separate decision for later.

Done when: a day of capture reaches Hearsay from the phone with Omi's cloud
out of the loop, including a stretch out of range and a paused stretch.

## 10. Every turn attributed

Turns too short to embed (under 2.5 s), or scoring between the owner and
not-owner thresholds, have no speaker: 18% of speech time on 2026-10-01.
Within a conversation the diarizer already groups each turn with a speaker,
and that speaker's voice-labeled turns agree almost always (51 of 53 at 90%
or more). An unlabeled turn takes its diarized speaker's label (owner or not,
and through the speaker's cluster, the person's name) when that speaker's
labeled turns agree, unless the turn sounds more like the owner than like
that speaker (the diarizer's usual slip is filing a short "yeah" under the
other person); the stream says the basis was diarization. Checked by ear on
a random sample of inherited turns (`hearsay-label check`). The first 155
checks were 86% right, 91% with the veto; the misses are nearly all
half-second interjections, which rarely carry anything.

Done when: under 5% of speech time has no speaker, and inherited labels hold
up by ear at 90% precision or better on a sample checked after the veto.

Done 2026-10-01: 3.2% of speech time has no speaker (voice 82%,
diarization 15%), and 278 inherited turns checked by ear after the veto are
96.4% right (96.2% by speech time).

Follow-up 2026-10-01, clusters: two clusters mixed named people (two women
bridged by untagged speakers; a man joined to a speaker the diarizer had
lumped several people into), and the diarizer gave two men one label in two
conversations, which clustering can't undo. Names are now constraints
(different names never share a cluster), and "More than one person" splits
the diarized speakers under the marked turns in two voices; a half not yet
named joins no named person, so it comes back to be named. Replayed on the
live data: no conflicts, and the second man came out as his own cluster.

## 11. Mac audio capture

A menu-bar app on the operator's personal Mac (no pendant) that captures the
mic (including AirPods) and system audio as separate channels: run/stop from
the menu bar, and a prompt to record when a Zoom call starts. Uploaded like pendant
audio. Speech on the mic channel is the owner's, by channel; during a call
the call recording wins over the pendant's. Recording calls can require
everyone's consent depending on jurisdiction, so the app asks rather than
starting on its own: a call is recorded only once the operator says yes.

Done when: a Zoom call's utterances reach the stream with the owner
attributed by channel, recorded after the app's prompt.

## 12. Retention

Delete non-owner audio after embedding and transcription. Starts only
once tagging is reliable: until then all audio is kept, because deleted
audio can't be re-embedded or re-tagged. First resolve how this fits
"raw payloads are never deleted" and "everything re-runs from raw".

Forgetting goes here too: the operator forgets a span, which is left out
of the stream on every reprocess and appended to `forgotten.json`. Whether
its raw payloads and audio are deleted is the same question as above.

Done when: non-owner audio is actually gone from disk, a forgotten span is
gone from the stream and listed in `forgotten.json`, and nothing that
depends on either breaks.

## 13. Where a conversation happened

The phone app records where the owner is while it captures, and each
conversation in the stream says where it happened, for downstream context.
Like taps, a fact about the capture, not an interpretation of it. The phone
is the source: it is with the owner wherever the pendant is, and the Mac's
location says little. Uploaded with the capture to the same tailnet-only
receiver, kept with the raw payloads, and re-run from raw like everything
else.

Location is as personal as the audio, but unlike audio it reaches
consumers. First resolve how precise it is in the stream (coordinates, or
a place the operator has named such as "Chill Room"), how often the phone
samples it (battery), and whether a conversation that moves gets one place
or several.

Done when: a day of conversations reaches the stream each with where it
happened, and the phone's battery use for it is acceptable.

## 14. Place as a hint for who is speaking

People are tied to places (the bartender at the Chill Room, a coworker at
the office), and operator names already carry them ("Robert (Chill Room)").
Use where a conversation happened to suggest who an unnamed speaker is: in
the portal first, ranking the people heard at that place ahead of
others, then perhaps as weak evidence in clustering. Voice stays the
judge: a place never names anyone on its own, and never overrides a voice
label or the operator's names.

Done when: naming a cluster in the portal offers the people heard at that
place first, and that measurably saves the operator time.
