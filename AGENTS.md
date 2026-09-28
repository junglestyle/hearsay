# Hearsay

Hearsay turns captured audio into attributed utterances: who said what, when.
It ingests Omi pendant data, assembles audio, transcribes, diarizes, and
identifies speakers (me / anonymous cluster / tagged person).

Hearsay does NOT categorize, summarize, extract ideas, or interpret content.
That belongs to downstream consumers (Idea Machine). If a change requires
understanding what was said, it is out of scope.

## Output contract

The product of Hearsay is a stream of utterance records:
conversation_id, start/end timestamps, speaker, text, confidence.
Downstream consumers read this. They never receive audio.

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

Slice 6: local transcription. The dev box (GPU) transcribes and diarizes each
conversation WAV with WhisperX (`hearsay/transcribe.py`, hourly systemd user
timer set up by `install/gpu.sh`) and writes transcripts to the NAS. Reprocess
turns them into speaker turns (`hearsay/turns.py`), and speaker labeling,
clustering, the portal and the label tool all work on turns, not Omi segments.

- A transcript records the sha256 of the WAV it came from and its settings;
  reprocess uses it only for that exact WAV, so turns always line up with the
  audio. Conversations without one are pending, with no Omi fallback.
- Operator input (ear labels, names, mixed, skip) is matched to turns by time
  span, never by id, so it survives re-transcription. Old records name Omi
  segment ids and are placed via those segments' times; new records carry
  their spans.
- Omi's segments stay in the database as facts, but only conversation
  windows (memory payloads) still come from Omi, until slice 7.
- Owner / not-owner labels are per turn (diarization lumped the owner with
  someone else in 4 of 74 speakers, too often to label short turns by
  speaker). Clustering of not-owner turns is per diarized speaker: one
  averaged embedding per speaker per conversation.
- Speaker thresholds were tuned on Omi segments; re-check them on turns with
  `python -m hearsay.label report`, after `label review` settles labels the
  model disagrees with.

Test fixtures are synthetic and committed; real captures never enter the repo.
Done when every conversation's utterances come from our own transcription,
and past labels and names still apply.

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

`README.md` is the orientation doc for installation: which script runs on
which host, in what order, and anything that happens between scripts
(pre-flight checks, post-flight verification, operator actions that span
targets). It links to scripts; it does not duplicate what they do.

- Any step inside a single target belongs in that target's script. A
  within-target step that only exists in the README or a chat is a bug.
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
  the README in the same change too.
- Secrets are read from an env file outside the repo, never written into it.
