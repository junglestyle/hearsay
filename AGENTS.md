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
- Audio never leaves Hearsay. Hearsay's boundary is the operator's own
  machines: the NAS and the dev box (which has the GPU). Audio may move
  between them; it never goes to third-party services or downstream consumers.
- Non-owner audio is deleted after embedding and transcription, per retention
  policy. Owner audio may be kept.
- Speaker embeddings are retained indefinitely; tagging and merging must be
  retroactive.
- All processing must be re-runnable from raw payloads.
- Data directories and secrets are never committed.

## Current slice

Slice 4: me vs. not-me. `hearsay/speakers.py` embeds each segment with
SpeechBrain ECAPA (CPU, pinned model baked into the worker image, offline) and
labels it owner / not_owner by cosine similarity to the owner's enrolled voice.
It runs as the last step of `python -m hearsay.reprocess`, after the database
(slice 2) and conversation audio (slice 3).

- Tuned for precision: between the two thresholds, a segment stays unlabeled,
  and so does anything under 2.5 s or with under 80% received audio.
- Operator input (enrollment windows, ear labels) lives in the labels dir on
  the NAS. It is durable, not derived: reprocessing reads it and never writes.
  `hearsay/label.py` writes it from the dev box.
- Thresholds are tuned against ear labels (`python -m hearsay.label report`);
  re-check them as more labels come in. Omi's is_user labels are for
  comparison only, never ground truth (they agreed with ear labels 79% of
  the time).

Test fixtures are synthetic and committed; real captures never enter the repo.
Done when owner speech is labeled reliably on real conversations, and misses
are rare enough to ignore.

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
