# Hearsay

Hearsay turns captured audio into attributed utterances: who said what, when.
It ingests Omi pendant data, assembles audio, transcribes, diarizes, and
identifies speakers (me / anonymous cluster / tagged person).

Hearsay does NOT categorize, summarize, extract ideas, or interpret content.
That belongs to downstream consumers (Idea Machine). If a change requires
understanding what was said, it is out of scope.

## Output contract

The product of Hearsay is a stream of utterance records (`hearsay/stream.py`,
written to `/mnt/storage/hearsay/stream/` after every reprocess): one JSONL
file per conversation plus `index.json`. Each utterance has conversation_id,
utterance_id, start/end (UTC), speaker {kind, name, label}, text,
text_confidence (Parakeet's token probabilities, each word its least
likely token) and speaker_confidence {basis,
owner_similarity}; basis is voice, named, cluster, channel (in a Mac
recording, the turn was loud on the mic, so the owner's, or on the call, so
someone else's), diarization (the turn took its label from its diarized
speaker in the conversation, not its own voice) or none. Speaker kinds: owner, person (named), anonymous (labeled
per conversation, e.g. "anon A"), stranger, unknown. `_noise` and `_media`
turns are left out; other `_` names are categories, not people. Owner
utterances may also carry `affect` {arousal}: how calm or animated the owner
sounded, about 0-1, from the audio alone (`hearsay/affect.py`); absent where
the turn wasn't scored. Other people's tone is never inferred.

Each index entry also lists `taps`: when the owner marked that conversation
with the pendant's button (a start mark, e.g. a self-note, kept even when
it's short). The owner can also end a conversation with the button, which
splits it there. Both are facts about the capture, not interpretations of it.
So is `places`: the places the operator has named that the conversation
happened at, in order, each {name, start, end}, from the phone's location
while it recorded. Never coordinates; a spot nobody named is left out, and
naming one later adds it retroactively.

Speakers and text change after the fact (naming is retroactive, growing
conversations are re-transcribed), so the unit of change is a conversation:
consumers re-read conversations whose index revision changed and replace them
wholesale. Downstream consumers read this. They never receive audio.

Guarantees consumers rely on (Idea Machine, `docs/ROADMAP.md` §3 there):

- `index.json` has `format_version` (an integer, now 1). Bump it on any change
  to the index or utterance format that would break a consumer.
- Files are written atomically (temp file, then rename), conversation files
  first and `index.json` last, and only when their content changes.
- `revision` is the first 16 hex characters of the sha256 of the
  conversation file's bytes, so a consumer can detect a read that raced a
  rewrite.
- `transcript_revision` changes when a conversation's transcript does
  (re-transcription, a growing conversation) and not when only speaker fields
  do (naming, merging, owner relabeling). Within one transcript_revision a
  given utterance_id always names the same stretch of speech; across them,
  ids are renumbered.
- `forgotten.json` is an append-only list, never rewritten or shrunk, one
  entry per thing the operator asked Hearsay to forget: {forgotten_at, start,
  end, conversation_id, utterance_ids, reason}, ids as they were. It is the
  only deletion signal: anything else that leaves the stream was
  restructured, not deleted. Forgetting is operator input applied on every
  reprocess, so re-running from raw never brings forgotten speech back into
  the stream. Raw payloads and audio of a forgotten span are kept for now:
  deleting them is slice 12's call, alongside retention. Until the operator
  can forget (slice 12), the list is empty.

## Invariants

- Raw webhook payloads are written to disk verbatim before any parsing.
  Never modify or delete raw payloads as part of processing.
- Audio never leaves Hearsay. Hearsay's boundary is the operator's home LAN
  and Tailscale tailnet: the NAS, the dev box (which has the GPU), the
  operator's Mac laptop, and the operator's phone. Audio, including non-owner
  audio, may move between them.
  It is never exposed on the public internet (the Cloudflare tunnel carries
  only the receiver) and never goes to third-party services or downstream
  consumers.
- Non-owner audio is deleted after embedding and transcription, per retention
  policy. Owner audio may be kept.
- Speaker embeddings are retained indefinitely; tagging and merging must be
  retroactive.
- All processing must be re-runnable from raw payloads.
- Data directories and secrets are never committed.

## Current slice

Slice 16: the owner's tone of voice (see the roadmap). Owner turns labeled by
voice or channel are scored on reprocess (`hearsay/affect.py`, audeering's
wav2vec2, CC-BY-NC-SA, baked into the worker image); only arousal reaches the
stream, the one dimension checked by ear. Valence waits for its own check
(tone without words, and a blind rating); dominance follows arousal for every
voice measured and stays out. Other people's tone is never inferred.

Slice 14: place as a hint for who is speaking (see the roadmap). When the
operator names a cluster in the portal, offer the people heard at the
conversation's places first. Voice stays the judge: a place never names
anyone on its own, and never overrides a voice label or the operator's names.

Slice 15 (media hint) is built and being measured alongside 14: a cluster
that sounds like the media the operator tagged offers `_media` for one tap
(`hearsay/media.py`), never tagged on its own.

Slice 13 (where a conversation happened) is done: the iPhone app samples the
owner's location while recording (every 2 minutes, records in the capture
upload, `hearsay/capture.py`), and each conversation in the stream lists the
places the operator named that it happened at (`hearsay/places.py`,
labels/places.jsonl), never coordinates.

Slice 12 (retention) waits until the operator says the models are tuned:
then audio past a rolling window (90 days to start) is deleted, and a
forgotten span leaves the stream and is listed in `forgotten.json`.

Slice 11 (Mac audio capture) is done: a menu-bar app on the operator's
personal Mac records the mic and the call as separate channels, only after
the operator says yes to a Zoom call's prompt (`mac/`, `install/mac.sh`).
Each turn is attributed by the channel it's loud on (`with_channel` in
`hearsay/speakers.py`), unless the voice clearly says otherwise; a work
machine is never used for capture or holds audio.

Test fixtures are synthetic and committed; real captures never enter the repo.
Done when naming a cluster in the portal offers the people heard at that
place first, and that measurably saves the operator time. Next: slice 12,
retention, once the operator says the models are tuned.

## Engineering style

Prefer boring, flat, readable code.

- Apply YAGNI aggressively. Do not introduce factories, generic wrappers,
  plugin systems, configuration frameworks, extension points, or generalized
  abstractions for hypothetical future requirements.
- Implement the minimum functionality required by the current specification.
- Prefer a small concrete implementation over a generalized framework.
- Prefer duplication over a premature or incorrect abstraction. Extract an
  abstraction when repeated code represents the same stable concept, not merely
  because two blocks look similar.
- Prefer standard-library functionality when it is simple and sufficient.
  Add dependencies when they materially simplify or improve the solution.
- Keep control flow shallow. Prefer early returns and straightforward sequential
  logic over deeply nested branches.
- Keep functions cohesive and reasonably small, but do not split code merely to
  satisfy a line-count rule.
- Use clear names instead of comments that restate the code. Comments should
  explain non-obvious constraints, invariants, tradeoffs, or reasons.
- Do not create architecture for requirements that are explicitly deferred.
- Preserve existing architecture boundaries unless the task requires changing them.
- Before introducing a new abstraction, layer, dependency, or subsystem, consider
  whether a simpler concrete solution solves the current problem more robustly.

## Testing guidance

Prefer a small number of high-value tests for realistic regression risks at stable boundaries.

Do test:
- durable state transitions and recovery
- protocol parsing/validation
- deduplication and retry behavior
- persistence/crash consistency
- authority and permission boundaries
- artifact identity/integrity
- externally observable workflow behavior

Do not add tests merely to increase coverage.

Avoid tests that:
- mirror implementation details
- assert constructor signatures or trivial getters/setters
- test every enum value or branch independently
- pin exact log text, UI wording, formatting, or internal call order
- mock large parts of the system only to verify plumbing
- duplicate guarantees already provided by type checking, compilation, or framework behavior

Prefer integration tests when the important property crosses process, persistence, protocol, or filesystem boundaries.

Use compilation/type checking/linting/manual validation where those provide better confidence than unit tests.

Before adding a test, ask:
1. What realistic regression would this catch?
2. Is this boundary stable?
3. Would this test still be useful after an internal refactor?

If those answers are weak, do not add the test.

Do not create tests for behavior that is already better validated by an end-to-end slice acceptance test.

## Scope discipline

Implement the requested slice, not the imagined future product.

When several solutions satisfy the requirements, prefer the one with:
1. fewer concepts,
2. fewer moving parts,
3. fewer dependencies,
4. less hidden behavior,
5. easier failure recovery.

Do not generalize from one implementation unless the current requirements
already contain multiple real cases that require the generalization.

## Installation

All installation and deployment lives in `install/`, one script per target
(e.g. `install/dev.sh`, `install/nas.sh`, `install/tunnel.sh`). Shared helpers
go in `install/lib.sh`. No install scripts live outside `install/`.

`docs/install.md` is the orientation doc for installation (`README.md` is
the project overview and links to it): which script runs on
which host, in what order, and anything that happens between scripts
(pre-flight checks, post-flight verification, operator actions that span
targets). It links to scripts; it does not duplicate what they do.

- Any step inside a single target belongs in that target's script. A
  within-target step that only exists in docs/install.md or a chat is a bug.
- Each script is idempotent: re-running it on an installed system is safe and
  changes nothing that's already correct.
- Steps that require the operator are still owned by the script: it prints
  exact instructions, waits for confirmation, then verifies the result where
  it can. A step is manual only when it requires the operator's account,
  device, or judgment; say why in the script.
- If one target depends on another, the script checks for it and says which
  script to run, rather than silently doing the other target's work.
- If a change requires a new setup step, update the relevant script in the
  same change. If it affects order, hosts, or between-script actions, update
  docs/install.md in the same change too.
- Secrets are read from an env file outside the repo, never written into it.
