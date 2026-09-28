# hearsay

Who said what, when. Turns always-on audio into attributed transcripts.

See [docs/roadmap.md](docs/roadmap.md).

## Install

Each script is idempotent and walks you through any step that needs you.
Secrets are kept in env files outside the repo.

1. **Dev box (optional):** [`install/dev.sh`](install/dev.sh) sets up a venv, a dev secret in
   `~/.config/hearsay/dev.env`, and runs the tests.
2. **NAS (TrueNAS SCALE 25.04+, as root):** clone the repo and run
   [`install/nas.sh`](install/nas.sh):
   ```sh
   git clone <repo-url> /mnt/storage/hearsay/repo
   sudo /mnt/storage/hearsay/repo/install/nas.sh
   ```
   This creates the datasets and secrets, builds the images, starts the
   receiver on `127.0.0.1:8787` and the speaker-naming portal on the LAN and
   tailnet, and checks that every captured payload reprocesses. The NAS must
   already be on your tailnet (the Tailscale app in the TrueNAS UI). Before you run it, the Apps pool must be set in the
   TrueNAS UI. The script checks this and tells you if it isn't.
3. **NAS, as root:** [`install/tunnel.sh`](install/tunnel.sh) connects a Cloudflare Tunnel,
   shows you the webhook URLs to enter in the Omi app, and waits until real
   payloads of every type arrive.
4. **NAS, as root:** reprocess once (below), so there are conversation WAVs to
   transcribe.
5. **Dev box with the GPU, as your user:** [`install/gpu.sh`](install/gpu.sh)
   sets up transcription (WhisperX) and an hourly timer that pulls new WAVs
   from the NAS over `ssh nas` and writes transcripts back. It asks for a
   Hugging Face token for the gated diarization model. Then reprocess again.

To update, run `git pull` on both hosts, then re-run `install/nas.sh` on the
NAS and `install/gpu.sh` on the dev box.

## Data

Webhooks land in `/mnt/storage/hearsay/raw/<type>/<date>/`:

- `.body` holds the request body, byte for byte.
- `.json` is the sidecar: timestamp, query params without the secret, all
  headers, size, and sha256.

Omi's cloud backend sends every webhook, not the phone. The `cf-connecting-ip`
header in the sidecars records where each request came from.

## Reprocessing

Rebuild everything derived from raw, on the NAS as root. Transcripts come from
the dev box, so new audio takes two passes: a reprocess makes the WAVs, the
dev box's hourly timer transcribes them (or run
`systemctl --user start hearsay-transcribe` there), and the next reprocess
turns the transcripts into turns.

```sh
docker compose -f /mnt/storage/hearsay/repo/install/compose.yaml run --rm reprocess
```

- `/mnt/storage/hearsay/db/hearsay.sqlite`: parsed payloads. Replaced only if
  every payload parses; otherwise the failures are listed and the old database
  and audio are left in place.
- `/mnt/storage/hearsay/audio/<conversation_id>.wav`: each conversation's audio,
  aligned so that segment times are offsets into the file. Missing audio is
  silence; the `conversation_audio` table records how each file was aligned
  and what fraction of it is real audio.

- Speaker embeddings and owner / not-owner labels per segment, in the
  `segment_speakers` table.

Raw is mounted read-only. Members of the `apps` group can read the outputs.

## Owner voice (dev box, then NAS)

Speaker labels need your voice enrolled, and thresholds tuned against segments
you've labeled by ear. Your input lives in `/mnt/storage/hearsay/labels/`,
which reprocessing reads but never writes. The labeling tool runs on any
machine of yours with `ssh nas` and an audio player (`paplay` or macOS
`afplay`): from a checkout as `.venv/bin/python -m hearsay.label`, or installed
as a command with
`uv tool install git+ssh://git@github.com/nathancurry/hearsay.git`, then
`hearsay-label` in place of `.venv/bin/python -m hearsay.label` below.

1. Record yourself reading aloud for about 3 minutes, alone, with the pendant
   streaming. Then register the window (local time):
   `.venv/bin/python -m hearsay.label enroll 2026-09-27T10:05 2026-09-27T10:08`
2. Reprocess on the NAS (above).
3. Label segments by ear: `.venv/bin/python -m hearsay.label` (plays audio
   locally, over `ssh nas`).
4. Re-hear the labels the model disagrees with, answering fresh:
   `.venv/bin/python -m hearsay.label review`. Answer `u` whenever you can't
   tell; a guess does more harm than no label.
5. See precision per threshold: `.venv/bin/python -m hearsay.label report`,
   then set `OWNER_THRESHOLD` / `NOT_OWNER_THRESHOLD` in
   [`hearsay/speakers.py`](hearsay/speakers.py), deploy, and reprocess.

## Naming other speakers (phone or any browser)

Reprocessing groups not-owner segments into anonymous speaker clusters. Name
them in the web portal that `nas.sh` starts, at the addresses it prints. It
listens on the LAN and tailnet only, so from your phone use the tailnet
address. Each cluster plays a few samples. Type a name (reusing a name merges
into that person), mark it as more than one person, or skip it for now: skipped
clusters move to their own list and come round again after the rest. If you
can't tell who it is (noise, several voices), skip rather than guess. A named
cluster can be renamed, or its name forgotten, from its page; a person can be
renamed everywhere at once from theirs (renaming onto an existing name merges
the two). Names starting with `_` (`_noise`, `_media`, `_stranger`) are for
categories, not people.

Names are stored on the segments you heard, in `/mnt/storage/hearsay/labels/`,
so they survive reclustering and apply to every past conversation. The portal
shows them immediately; the database picks them up on the next reprocess.
`.venv/bin/python -m hearsay.label report` shows cluster health, for tuning
`CLUSTER_THRESHOLD` in [`hearsay/people.py`](hearsay/people.py).

To re-run the test suite against the real captures (also run by `nas.sh`):

```sh
docker compose -f /mnt/storage/hearsay/repo/install/compose.yaml run --rm check
```

After a `git pull`, re-run `install/nas.sh` first so both commands use the new code.
