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
   This creates the datasets and the receiver secret, builds the images, starts
   the receiver on `127.0.0.1:8787`, and checks that every captured payload
   parses. Before you run it, the Apps pool must be set in the
   TrueNAS UI. The script checks this and tells you if it isn't.
3. **NAS, as root:** [`install/tunnel.sh`](install/tunnel.sh) connects a Cloudflare Tunnel,
   shows you the webhook URLs to enter in the Omi app, and waits until real
   payloads of every type arrive.

To update, run `git pull`, then re-run `install/nas.sh`.

## Data

Webhooks land in `/mnt/storage/hearsay/raw/<type>/<date>/`:

- `.body` holds the request body, byte for byte.
- `.json` is the sidecar: timestamp, query params without the secret, all
  headers, size, and sha256.

Omi's cloud backend sends every webhook, not the phone. The `cf-connecting-ip`
header in the sidecars records where each request came from.

## Parsing

Rebuild the database from raw, on the NAS as root:

```sh
docker compose -f /mnt/storage/hearsay/repo/install/compose.yaml run --rm parse
```

It writes `/mnt/storage/hearsay/db/hearsay.sqlite`. Each run is a full rebuild
and replaces the file only if every payload parses; otherwise it lists the
failures and leaves the old database in place. Raw is mounted read-only.
Members of the `apps` group can query the database with `sqlite3`.

To re-run the test suite against the real captures (also run by `nas.sh`):

```sh
docker compose -f /mnt/storage/hearsay/repo/install/compose.yaml run --rm check
```

After a `git pull`, re-run `install/nas.sh` first so both commands use the new code.
