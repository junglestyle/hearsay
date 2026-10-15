# Running Hearsay

The operator's guide: which script runs on which host, in what order, and what
to do between them. For what Hearsay is, see the [README](../README.md).

## Install

Each script is idempotent and walks you through any step that needs you.
Secrets are kept in env files outside the repo.

1. **Dev box (optional):** [`install/dev.sh`](../install/dev.sh) sets up a venv, a dev secret in
   `~/.config/hearsay/dev.env`, and runs the tests.
2. **NAS (TrueNAS SCALE 25.04+):** clone the repo as `admin` into the
   `fast/repos` dataset and run [`install/nas.sh`](../install/nas.sh) with sudo:
   ```sh
   git clone https://github.com/junglestyle/hearsay.git /mnt/fast/repos/hearsay
   sudo /mnt/fast/repos/hearsay/install/nas.sh
   ```
   This creates the datasets under `fast/hearsay` (the NVMe pool) and the
   secrets, builds the images, starts the speaker-naming portal on the LAN
   and tailnet and the capture receiver on the tailnet, checks that every
   captured payload reprocesses, reprocesses once, and waits for a real
   upload from the phone or Mac (on a new install it stops there; re-run it
   after step 4). The NAS must already be on your tailnet (the Tailscale
   app in the TrueNAS UI). Before you run it, the Apps pool must be set in
   the TrueNAS UI. The script checks this and tells you if it isn't.
3. **Dev box with the GPU, as your user:** [`install/gpu.sh`](../install/gpu.sh)
   sets up transcription (Parakeet, pyannote) and an hourly timer that pulls new WAVs
   from the NAS over `ssh nas` and writes transcripts back. It asks for a
   Hugging Face token for the gated diarization model. Then reprocess again.

4. **iPhone, from the Mac, as your user (replaces Omi's app and cloud as
   the audio path):** [`install/ios.sh`](../install/ios.sh) builds the pendant
   app with Xcode and installs it on the phone. It asks for the capture URL
   that `nas.sh` prints; the token is entered in the app. The phone needs
   Tailscale. The pendant takes one connection at a time, so disconnect it
   from Omi's app if it was ever paired there.
5. **The operator's personal Mac, as your user (optional; never a work
   machine):** [`install/mac.sh`](../install/mac.sh) builds the menu-bar
   recorder and starts it at login. It records the mic and system audio as
   two channels, from the menu or, after asking you, when a Zoom call starts,
   and uploads to the same capture URL and token as the phone. Recording a
   call can need everyone's consent where you are. Needs macOS 14.4+.

To update, run `git pull` on every host, then re-run `install/nas.sh` on the
NAS, `install/gpu.sh` on the dev box, and `install/ios.sh` and
`install/mac.sh` on the Mac, in that order: the NAS must read a new kind of
upload record before the phone or Mac sends it. The dev box runs
`hearsay/transcribe.py` from its checkout, so its `git pull` takes effect at
the next timer run. With a
free Apple ID the phone's install expires after 7 days; re-running
`install/ios.sh` renews it.

### Moving from the storage pool (October 2026)

Hearsay used to live in `storage/hearsay` on the spinning pool, with the
clone in `/mnt/fast/hearsay/repo`. `nas.sh` moves it: when a dataset is
missing under `fast/hearsay` and present under `storage/hearsay`, it stops
Hearsay and its cron job, copies each dataset with `zfs send | zfs recv`,
sets `storage/hearsay` read-only, and starts everything from the new paths.
Before running it:

1. On the dev box, `git pull` (its transcription timer then uses the new
   paths; runs fail until the NAS has moved, and catch up after).
2. On the NAS, as admin, clone into `/mnt/fast/repos/hearsay` (above), then
   `sudo /mnt/fast/repos/hearsay/install/nas.sh`.

It also retires Omi's webhook receiver: it removes the receiver and
Cloudflare tunnel containers, asks you to delete the tunnel and its DNS
record in Cloudflare, waits until the hostname stops resolving, and deletes
the tunnel token and webhook secret. Idea Machine reads
`/mnt/fast/hearsay/stream` once it exists. When you've checked Hearsay works
from `fast`, destroy the old copy with the command `nas.sh` prints
(`sudo zfs destroy -r storage/hearsay`).

## Data

Uploads land in `/mnt/fast/hearsay/raw/capture/<date>/`; Omi's webhook
payloads (until 2026-09-30) stay in `raw/{transcript,audio,memory}/` and are
still parsed:

- `.body` holds the request body, byte for byte.
- `.json` is the sidecar: timestamp, query params, headers without the
  token, size, and sha256.

## Recording from the pendant (iPhone)

The app ([`ios/`](../ios/)) connects to the Omi pendant over Bluetooth, keeps
everything it sends on the phone (in the app's own storage, excluded from
backups), and uploads it every minute to the capture receiver on the NAS over
the tailnet. It keeps recording in the background, and iOS relaunches it when
the pendant comes back in range. If the NAS is unreachable, uploads wait on
the phone. The app shows the pendant's state, battery and charging, the last
minute's packet counts, and how many uploads are waiting, and sets the
pendant's light (saved on the pendant).

While out of range the pendant records to its own storage; on reconnect the
app downloads that and uploads it like the rest, and the NAS places it by the
pendant's clock. Pause keeps audio from reaching the NAS (dropped, or kept on
the phone for 30 days, where a paused stretch can be played, cropped to one
selection and uploaded, or deleted); if the app was paused at any time
while the pendant was away, everything it stored then counts as paused. Mute
turns the pendant's mic off in hardware, and the app uploads none of its
audio while muted (nor what it stored while away, if muted then).

The pendant's single and double tap are set in the app (Pendant button): mark
to keep the conversation around the tap, from 30 s before it to 30 s after
you stop, even when it's short; mark to end the conversation there; pause or
resume; mute or unmute; or nothing. Each confirms with its own buzz. Single
tap defaults to keep, double tap to pause. Holding the button 3 s turns the
pendant off. The NAS must be updated before the
app whenever the upload format changes, as the update order above does.

## Recording on the Mac

The menu-bar recorder (`mac/`, installed by `install/mac.sh`) records the
mic and system audio as separate channels: Record and Stop in its menu tap
all system audio; when Zoom opens the mic for a call it asks first, then taps
Zoom only and stops when the call ends. Where a Mac recording and the pendant
overlap, the Mac's wins. A turn loud on the mic and quiet on the call is
yours, the reverse someone else's (basis `channel` in the stream). Log:
`~/Library/Logs/hearsay-mac.log`. The NAS must be updated before the Mac app
whenever the upload format changes.

## Reprocessing

Everything derived from raw is rebuilt hourly at :30 by a TrueNAS cron job
that `nas.sh` sets up (log: `journalctl -t hearsay-reprocess`; the portal
and capture receiver log to the journal too, e.g.
`journalctl CONTAINER_NAME=hearsay-capture`).
Conversations are found in the audio stream itself (voice detection; a
conversation ends after 3 minutes of silence), not taken from Omi. Transcripts
come from the dev box's timer at :00, so new audio flows WAV, then transcript,
then turns within about an hour, with nothing to run by hand. Each run builds
a complete new database from raw in memory, then updates the real one in a
single transaction that writes only the rows that changed, so readers never
see a half-built database and a run with nothing new writes almost nothing
(`fast` is snapshotted nightly). Slow steps are cached by audio content in
`db/cache.sqlite`.

To run it now instead of waiting, on the NAS (and on the dev box,
`systemctl --user start hearsay-transcribe`). The lock is the cron job's, so
this waits for a run in progress.

```sh
sudo flock /run/hearsay-reprocess.lock docker compose -f /mnt/fast/repos/hearsay/install/compose.yaml run --rm reprocess
```

- `/mnt/fast/hearsay/db/hearsay.sqlite`: parsed payloads. Updated only if
  every payload parses; otherwise the failures are listed and the old database
  and audio are left in place. Deleting it is safe: the next run builds it
  again from raw.
- `/mnt/fast/hearsay/audio/<conversation_id>.wav`: each conversation's audio,
  aligned so that segment times are offsets into the file. Missing audio is
  silence; the `conversation_audio` table records how each file was aligned
  and what fraction of it is real audio. A WAV is rewritten only when its
  audio changes.

- Speaker embeddings and owner / not-owner labels per segment, in the
  `segment_speakers` table.

Raw is mounted read-only. Members of the `apps` group can read the outputs.

## Owner voice (dev box, then NAS)

Speaker labels need your voice enrolled, and thresholds tuned against segments
you've labeled by ear. Your input lives in `/mnt/fast/hearsay/labels/`,
which reprocessing reads but never writes. The labeling tool runs on any
machine of yours with `ssh nas` and an audio player (`paplay` or macOS
`afplay`): from a checkout as `.venv/bin/python -m hearsay.label`, or installed
as a command with
`uv tool install git+ssh://git@github.com/junglestyle/hearsay.git`, then
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
   [`hearsay/speakers.py`](../hearsay/speakers.py), deploy, and reprocess.
6. Turns too short to judge by voice take the label of their diarized speaker
   in the same conversation. Check those by ear, blind, in random order:
   `.venv/bin/python -m hearsay.label check`. After a reprocess, `report`
   shows their precision and how much speech is still unlabeled.

## Reading the utterance stream (consumers)

`/mnt/fast/hearsay/stream/` (readable by the `apps` group) holds Hearsay's
output, rewritten after every hourly reprocess:

- `index.json`: every conversation with start, end, `open` (still going when
  the audio stopped), `transcribed`, `taps` (times the owner marked it with
  the pendant's button), utterance count, `revision`, `transcript_revision`,
  and `file`, under a top-level `format_version`.
- `conversations/<conversation_id>.jsonl`: one utterance per line, in time
  order; fields are described under Output contract in [`AGENTS.md`](../AGENTS.md).
- `forgotten.json`: append-only list of what the operator asked Hearsay to
  forget.

Treat each conversation as a unit: when its `revision` changes, re-read its
file and replace everything you had for it. Names apply retroactively and
conversations get re-transcribed as they grow, so individual utterances can
change or disappear; utterance ids keep their meaning only while
`transcript_revision` does. The guarantees are under Output contract in
[`AGENTS.md`](../AGENTS.md). No audio is ever in the stream.

## Importing audio the live stream missed

If something was recorded that never reached Hearsay (another recorder, or
Omi's app before it was retired), export the audio and import it with the
recording's start time, in your local time:

```sh
hearsay-label import ~/Downloads/omi_export.mp3 "2026-09-28 19:42"
```

It is copied unchanged to `/mnt/fast/hearsay/imports/` over `ssh nas` and
placed on the timeline at that time on the next reprocess; where it overlaps
live audio, the live audio wins. Importing the same file twice does nothing.

## Naming other speakers (phone or any browser)

Reprocessing groups not-owner segments into anonymous speaker clusters. Name
them in the web portal that `nas.sh` starts, at the addresses it prints. It
listens on the LAN and tailnet only, so from your phone use the tailnet
address, or the Hearsay app's Voices tab, which shows the same portal and
logs in with the app's capture token. Each cluster plays a few samples. Type a name (reusing a name merges
into that person), mark it as more than one person, or skip it for now: skipped
clusters move to their own list and come round again after the rest. If you
can't tell who it is (noise, several voices), skip rather than guess. A
single clip that isn't the speaker (a cough, a video, someone else) can be
tagged on its own, under its player: only that turn takes the tag. A named
cluster can be renamed, or its name forgotten, from its page; a person can be
renamed everywhere at once from theirs (renaming onto an existing name merges
the two). Names starting with `_` are
categories, not people: Media, Music, Noise and Stranger each have a button,
on the cluster and under each clip (`_media` is TV or video talk, `_music`
is songs, so the media hint learns from talk alone).

The portal's Places page lists spots where the phone recorded and no named
place is near (how long, which days, who was heard there, a map link); name
them there. Only these names reach the stream, never coordinates.
A cluster heard at a named place offers the people heard most at that place
as one-tap names above its name box; a place never names anyone on its own.
A cluster that sounds like the TV or video you've tagged `_media` before
has its Media button picked out.

The portal's Words page lists words transcription leans toward (names,
jargon). Type them one per line and save a draft; it shows what would change
and waits as long as you like. Update commits them together with the words
you approved in Lattice, which Idea Machine writes to
`vocab.json` in its vocab folder: `/mnt/fast/ideamachine/vocab` once Idea
Machine has moved it, `/mnt/storage/ideamachine/vocab` until then. `nas.sh`
picks whichever exists and says which (re-run it after Idea Machine moves),
makes the folder if Idea Machine hasn't, and the portal only reads it. Each Update re-transcribes
every conversation on the dev box's next run, so batch your edits.

Names are stored on the segments you heard, in `/mnt/fast/hearsay/labels/`,
so they survive reclustering and apply to every past conversation. The portal
shows them immediately; the database picks them up on the next reprocess.
`.venv/bin/python -m hearsay.label report` shows cluster health, for tuning
`CLUSTER_THRESHOLD` in [`hearsay/people.py`](../hearsay/people.py).

To re-run the test suite against the real captures (also run by `nas.sh`):

```sh
sudo docker compose -f /mnt/fast/repos/hearsay/install/compose.yaml run --rm check
```

After a `git pull`, re-run `install/nas.sh` first so both commands use the new code.
