#!/usr/bin/env bash
# TrueNAS SCALE: datasets on the fast pool (moved from storage once), secrets,
# images, portal and capture containers, the hourly reprocess.
# Run as root from the clone in /mnt/fast/repos/hearsay. Safe to re-run; also
# how updates are deployed.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

require_root
COMPOSE=(docker compose -f "$REPO_DIR/install/compose.yaml")
CHILDREN=(config raw db audio labels transcripts imports stream)

say "Checking Docker"
if ! docker info >/dev/null 2>&1; then
    # Manual: picking the Apps pool is a storage decision for the operator.
    die "Docker isn't running. In the TrueNAS UI: Apps -> Configuration -> Choose Pool. Then re-run."
fi

say "Checking the repo is at $NAS_REPO_DIR"
# The cron job and compose run from this clone, and the old one under
# $NAS_OLD_DATASET goes away with it.
[ "$REPO_DIR" = "$NAS_REPO_DIR" ] || die "this is $REPO_DIR. As admin (not root):
  git clone https://github.com/junglestyle/hearsay.git $NAS_REPO_DIR
then: sudo $NAS_REPO_DIR/install/nas.sh"
echo "ok"

say "Retiring Omi's webhook receiver and its Cloudflare tunnel"
# Omi isn't used any more (last payload 2026-09-30); its raw payloads stay.
for container in hearsay-tunnel hearsay; do
    if docker inspect "$container" >/dev/null 2>&1; then
        docker rm -f "$container" >/dev/null
        echo "removed container: $container"
    fi
done
echo "ok"

cron_id="$(midclt call cronjob.query '[["description", "=", "hearsay reprocess"]]' \
    | python3 -c 'import json, sys; jobs = json.load(sys.stdin); print(jobs[0]["id"] if jobs else "")')"

say "Datasets under $NAS_POOL_DATASET"
migrate=false
for ds in "${CHILDREN[@]}"; do
    if ! zfs list -H -o name "$NAS_POOL_DATASET/$ds" >/dev/null 2>&1 \
        && zfs list -H -o name "$NAS_OLD_DATASET/$ds" >/dev/null 2>&1; then
        migrate=true
    fi
done
if [ "$migrate" = true ]; then
    # Nothing may write to the old datasets between snapshot and switch-over.
    echo "Stopping Hearsay to move it from $NAS_OLD_DATASET"
    [ -z "$cron_id" ] || midclt call cronjob.update "$cron_id" '{"enabled": false}' >/dev/null
    # By project label, not `compose down`: the compose file reads env files
    # from the new datasets, which don't exist yet.
    docker ps -aq --filter label=com.docker.compose.project=hearsay | xargs -r docker rm -f
    echo "waiting for any reprocess run in progress..."
    flock /run/hearsay-reprocess.lock true
fi
# midclt (not raw zfs) so the TrueNAS UI knows about the ones made here.
if ! zfs list -H -o name "$NAS_POOL_DATASET" >/dev/null 2>&1; then
    midclt call pool.dataset.create "{\"name\": \"$NAS_POOL_DATASET\"}" >/dev/null
    echo "created: $NAS_POOL_DATASET"
fi
for ds in "${CHILDREN[@]}"; do
    new="$NAS_POOL_DATASET/$ds" old="$NAS_OLD_DATASET/$ds"
    if zfs list -H -o name "$new" >/dev/null 2>&1; then
        echo "exists: $new"
    elif zfs list -H -o name "$old" >/dev/null 2>&1; then
        # send -p keeps the dataset's properties; recv is all or nothing, so
        # a re-run after an interruption sends it again.
        snap="$old@moved-to-fast"
        zfs list -H -o name "$snap" >/dev/null 2>&1 || zfs snapshot "$snap"
        zfs send -p "$snap" | zfs recv "$new"
        zfs destroy "$new@moved-to-fast"
        echo "moved: $old -> $new"
    else
        midclt call pool.dataset.create "{\"name\": \"$new\"}" >/dev/null
        echo "created: $new"
    fi
done
for ds in "${CHILDREN[@]}"; do
    [ -d "$NAS_ROOT/$ds" ] || die "$NAS_POOL_DATASET/$ds isn't mounted at $NAS_ROOT/$ds"
done
if zfs list -H -o name "$NAS_OLD_DATASET" >/dev/null 2>&1 \
    && [ "$(zfs get -H -o value readonly "$NAS_OLD_DATASET")" != on ]; then
    # Kept until the operator destroys it, read-only so that anything still
    # pointed at it (an old clone, a stale timer) fails instead of writing
    # where nobody reads.
    zfs set readonly=on "$NAS_OLD_DATASET"
    echo "read-only now: $NAS_OLD_DATASET"
fi

# Each dataset below guards itself; members of the apps group (the dev box's
# ssh login, consumers) need to get through this one.
chown root:root "$NAS_ROOT"
chmod 755 "$NAS_ROOT"
chown root:root "$NAS_CONFIG"
chmod 700 "$NAS_CONFIG"
chown "$APPS_UID:$APPS_GID" "$NAS_RAW"
chmod 750 "$NAS_RAW"
# Parsed transcripts, assembled audio and the utterance stream: same
# sensitivity as raw. The apps group (consumers included) can read them.
for dir in "$NAS_DB" "$NAS_AUDIO" "$NAS_STREAM"; do
    chown "$APPS_UID:$APPS_GID" "$dir"
    chmod 750 "$dir"
done
# Written over ssh by members of the apps group: operator input (enrollment
# windows, labels, imported audio) and transcripts (install/gpu.sh).
# setgid keeps new files in the apps group, which the containers run as.
for dir in "$NAS_LABELS" "$NAS_TRANSCRIPTS" "$NAS_IMPORTS"; do
    chown "$APPS_UID:$APPS_GID" "$dir"
    chmod 2770 "$dir"
done

say "Idea Machine's vocab folder"
# The portal mounts it read-only. Idea Machine moves it to the fast pool on
# its own schedule; re-running this script after that switches the portal over.
# Made here only if Idea Machine hasn't yet, owned like the stream (the apps
# group), so Docker doesn't make it root's and lock Idea Machine out.
if [ -d "$IDEAMACHINE_VOCAB_FAST" ]; then
    IDEAMACHINE_VOCAB_DIR="$IDEAMACHINE_VOCAB_FAST"
    echo "using: $IDEAMACHINE_VOCAB_DIR"
elif [ -d "$IDEAMACHINE_VOCAB_OLD" ]; then
    IDEAMACHINE_VOCAB_DIR="$IDEAMACHINE_VOCAB_OLD"
    echo "using: $IDEAMACHINE_VOCAB_DIR ($IDEAMACHINE_VOCAB_FAST doesn't exist yet)"
else
    IDEAMACHINE_VOCAB_DIR="$IDEAMACHINE_VOCAB_OLD"
    install -d -o "$APPS_UID" -g "$APPS_GID" -m 2770 "$IDEAMACHINE_VOCAB_DIR"
    echo "created: $IDEAMACHINE_VOCAB_DIR (Idea Machine writes vocab.json there)"
fi
export IDEAMACHINE_VOCAB_DIR

tunnel_env="$NAS_CONFIG/tunnel.env"
if [ -f "$tunnel_env" ]; then
    host="$(env_get "$tunnel_env" HEARSAY_PUBLIC_HOST)"
    say "Delete the Cloudflare tunnel"
    # Manual: needs the operator's Cloudflare account.
    cat <<EOF
The tunnel's container is gone. In the Cloudflare dashboard:
  1. Zero Trust -> Networks -> Tunnels: delete the tunnel named hearsay.
  2. DNS for your zone: delete the record for ${host:-the receiver's hostname}.
In the Omi app, you can also turn off the three webhooks.
EOF
    confirm "Deleted both in Cloudflare?"
    if [ -n "$host" ]; then
        while getent hosts "$host" >/dev/null; do
            echo "$host still resolves (DNS can take a few minutes to forget it)."
            confirm "Check again?"
        done
        echo "$host no longer resolves"
    fi
    # The tunnel token and Omi's webhook secret guard nothing now.
    rm -f "$tunnel_env" "$NAS_CONFIG/receiver.env"
    echo "removed $tunnel_env and $NAS_CONFIG/receiver.env"
fi

say "Portal login in $NAS_PORTAL_ENV"
env_ensure "$NAS_PORTAL_ENV" HEARSAY_PORTAL_USER hearsay
env_ensure "$NAS_PORTAL_ENV" HEARSAY_PORTAL_PASSWORD "$(openssl rand -base64 24 | tr -d '/+=')"
env_ensure "$NAS_PORTAL_ENV" HEARSAY_SESSION_KEY "$(openssl rand -hex 32)"

say "Capture token in $NAS_CAPTURE_ENV"
env_ensure "$NAS_CAPTURE_ENV" HEARSAY_CAPTURE_TOKEN "$(openssl rand -hex 32)"

say "Portal addresses (LAN and tailnet only)"
# Bound to these two addresses, never all interfaces: the NAS also has a
# public IPv6 address, and non-owner audio must stay on the LAN and tailnet.
HEARSAY_LAN_IP="$(ip -4 route get 1.1.1.1 | sed -n 's/.* src \([0-9.]*\).*/\1/p')"
HEARSAY_TAILNET_IP="$(ip -4 -o addr show tailscale0 2>/dev/null | awk '{print $4}' | cut -d/ -f1)"
[ -n "$HEARSAY_LAN_IP" ] || die "couldn't find the LAN address"
# Manual: joining the tailnet needs the operator's Tailscale account.
[ -n "$HEARSAY_TAILNET_IP" ] \
    || die "no tailscale0 address. Install and log in to the Tailscale app (TrueNAS UI: Apps), then re-run."
echo "LAN $HEARSAY_LAN_IP, tailnet $HEARSAY_TAILNET_IP"
export HEARSAY_LAN_IP HEARSAY_TAILNET_IP

say "Building images"
# Baked into the worker image and recorded by each reprocess run, so a stale
# image is visible. safe.directory: root runs git in a clone owned by admin.
HEARSAY_COMMIT="$(git -c safe.directory="$REPO_DIR" -C "$REPO_DIR" describe --always --dirty)"
export HEARSAY_COMMIT
# --profile tools also builds the one-shot worker (reprocess) and check images.
"${COMPOSE[@]}" --profile tools build

say "Starting portal and capture receiver"
# --remove-orphans: the old Omi receiver was a service of this project.
"${COMPOSE[@]}" up -d --remove-orphans

say "Verifying the portal answers on both addresses"
for ip in "$HEARSAY_LAN_IP" "$HEARSAY_TAILNET_IP"; do
    wait_for_status GET "http://$ip:$NAS_PORTAL_PORT/login" 200 30
done

say "Verifying the capture receiver answers on the tailnet only"
wait_for_401 "http://$HEARSAY_TAILNET_IP:$NAS_CAPTURE_PORT/capture" 30
if curl -s -o /dev/null --max-time 3 "http://$HEARSAY_LAN_IP:$NAS_CAPTURE_PORT/capture"; then
    die "the capture receiver answers on the LAN address; it must be tailnet only"
fi

say "Checking that every captured payload parses"
# -T and /dev/null: never read the terminal, so typing ahead isn't swallowed.
"${COMPOSE[@]}" run --rm -T check </dev/null

say "Reprocessing now"
# Waits for a run in progress. Its output is in the journal too:
# journalctl -t hearsay-reprocess
flock /run/hearsay-reprocess.lock "${COMPOSE[@]}" run --rm -T reprocess </dev/null

say "Hourly reprocess (TrueNAS cron job)"
# At :30, offset from the dev box's transcription timer at :00, so new audio
# goes WAV -> transcript -> turns within about an hour. flock skips a run
# while the previous one is still going. Registered through midclt so it
# shows in the TrueNAS UI (System -> Advanced -> Cron Jobs) and survives updates.
# Output goes to the journal, not a file on either pool. The middleware
# appends its own "> /dev/null 2> /dev/null" to the command, so the pipe
# sits inside an outer subshell.
REPROCESS_CMD="( flock -n /run/hearsay-reprocess.lock ${COMPOSE[*]} run --rm -T reprocess </dev/null 2>&1 | logger -t hearsay-reprocess )"
cron_payload="$(python3 -c '
import json, sys
print(json.dumps({"user": "root", "command": sys.argv[1], "description": "hearsay reprocess",
                  "schedule": {"minute": "30", "hour": "*", "dom": "*", "month": "*", "dow": "*"},
                  "enabled": True, "stdout": True, "stderr": True}))' "$REPROCESS_CMD")"
if [ -z "$cron_id" ]; then
    midclt call cronjob.create "$cron_payload" >/dev/null
    echo "created: hourly reprocess at :30"
else
    midclt call cronjob.update "$cron_id" "$cron_payload" >/dev/null
    echo "up to date: hourly reprocess at :30 (job $cron_id)"
fi
echo "log: journalctl -t hearsay-reprocess"

say "Verifying uploads land in $NAS_RAW"
# Manual: only the operator's phone (or Mac) can send a real upload.
marker="$(mktemp)"
trap 'rm -f "$marker"' EXIT
echo "Record for a minute with the iPhone app (or the Mac's recorder). Waiting up to 5 minutes..."
for _ in $(seq 300); do
    [ -z "$(find "$NAS_RAW/capture" -name '*.json' -newer "$marker" -print -quit 2>/dev/null)" ] || break
    sleep 1
done
[ -n "$(find "$NAS_RAW/capture" -name '*.json' -newer "$marker" -print -quit 2>/dev/null)" ] \
    || die "no upload arrived in $NAS_RAW/capture. Check the app's capture URL and token, then re-run."
echo "received an upload in $NAS_RAW/capture"

say "Hearsay is running from $NAS_ROOT"
# Manual: the password manager is on the operator's devices.
cat <<EOF

Portal for naming speakers (save it in your password manager):
  http://$HEARSAY_TAILNET_IP:$NAS_PORTAL_PORT   (tailnet: encrypted; use this from your phone)
  http://$HEARSAY_LAN_IP:$NAS_PORTAL_PORT   (LAN: unencrypted Wi-Fi)
  username: $(env_get "$NAS_PORTAL_ENV" HEARSAY_PORTAL_USER)
  password: sudo sed -n 's/^HEARSAY_PORTAL_PASSWORD=//p' $NAS_PORTAL_ENV

Capture receiver, for the iPhone app (install/ios.sh on the Mac):
  URL:   http://$HEARSAY_TAILNET_IP:$NAS_CAPTURE_PORT/capture
  token: sudo sed -n 's/^HEARSAY_CAPTURE_TOKEN=//p' $NAS_CAPTURE_ENV
EOF
if zfs list -H -o name "$NAS_OLD_DATASET" >/dev/null 2>&1; then
    cat <<EOF

The old copy is still on the storage pool, read-only. Once you've confirmed
Hearsay works from $NAS_ROOT, destroy it (its old repo clone and
logs/reprocess.log go with it):
  sudo zfs destroy -r $NAS_OLD_DATASET
EOF
fi
