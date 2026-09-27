#!/usr/bin/env bash
# TrueNAS SCALE: datasets, secrets, images, receiver and portal containers.
# Run as root from the cloned repo. Safe to re-run; also how updates are deployed.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

require_root

say "Checking Docker"
if ! docker info >/dev/null 2>&1; then
    # Manual: picking the Apps pool is a storage decision for the operator.
    die "Docker isn't running. In the TrueNAS UI: Apps -> Configuration -> Choose Pool. Then re-run."
fi

say "Datasets under $NAS_POOL_DATASET"
# midclt (not raw zfs) so the TrueNAS UI knows about them.
for ds in "$NAS_POOL_DATASET" "$NAS_POOL_DATASET/config" "$NAS_POOL_DATASET/raw" "$NAS_POOL_DATASET/db" "$NAS_POOL_DATASET/audio" "$NAS_POOL_DATASET/labels"; do
    if zfs list -H -o name "$ds" >/dev/null 2>&1; then
        echo "exists: $ds"
    else
        midclt call pool.dataset.create "{\"name\": \"$ds\"}" >/dev/null
        echo "created: $ds"
    fi
done
[ -d "$NAS_RAW" ] && [ -d "$NAS_CONFIG" ] && [ -d "$NAS_DB" ] && [ -d "$NAS_AUDIO" ] && [ -d "$NAS_LABELS" ] || die "datasets not mounted under $NAS_ROOT"

chown root:root "$NAS_CONFIG"
chmod 700 "$NAS_CONFIG"
chown "$APPS_UID:$APPS_GID" "$NAS_RAW"
chmod 750 "$NAS_RAW"
# Parsed transcripts and assembled audio: same sensitivity as raw. Apps group can read them.
for dir in "$NAS_DB" "$NAS_AUDIO"; do
    chown "$APPS_UID:$APPS_GID" "$dir"
    chmod 750 "$dir"
done
# Operator input (enrollment windows, labels), written over ssh by members of
# the apps group from the dev box. setgid keeps new files in the apps group.
chown "$APPS_UID:$APPS_GID" "$NAS_LABELS"
chmod 2770 "$NAS_LABELS"

say "Receiver secret in $NAS_RECEIVER_ENV"
env_ensure "$NAS_RECEIVER_ENV" HEARSAY_SECRET "$(openssl rand -hex 32)"

say "Portal login in $NAS_PORTAL_ENV"
env_ensure "$NAS_PORTAL_ENV" HEARSAY_PORTAL_USER hearsay
env_ensure "$NAS_PORTAL_ENV" HEARSAY_PORTAL_PASSWORD "$(openssl rand -base64 24 | tr -d '/+=')"
env_ensure "$NAS_PORTAL_ENV" HEARSAY_SESSION_KEY "$(openssl rand -hex 32)"

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
# image is visible. safe.directory: root runs git in a clone owned by apps.
HEARSAY_COMMIT="$(git -c safe.directory="$REPO_DIR" -C "$REPO_DIR" describe --always --dirty)"
export HEARSAY_COMMIT
# --profile tools also builds the one-shot worker (reprocess) and check images.
docker compose -f "$REPO_DIR/install/compose.yaml" --profile tools build

say "Starting receiver and portal"
docker compose -f "$REPO_DIR/install/compose.yaml" up -d

say "Verifying receiver rejects unauthenticated requests"
wait_for_401 "http://127.0.0.1:$NAS_RECEIVER_PORT/omi/transcript" 30

say "Verifying the portal answers on both addresses"
for ip in "$HEARSAY_LAN_IP" "$HEARSAY_TAILNET_IP"; do
    wait_for_status GET "http://$ip:$NAS_PORTAL_PORT/login" 200 30
done

say "Checking that every captured payload parses"
# -T and /dev/null: never read the terminal, so typing ahead isn't swallowed.
docker compose -f "$REPO_DIR/install/compose.yaml" run --rm -T check </dev/null

say "Receiver is up on 127.0.0.1:$NAS_RECEIVER_PORT. Next: install/tunnel.sh"
# Manual: the password manager is on the operator's devices.
cat <<EOF

Portal for naming speakers (save it in your password manager):
  http://$HEARSAY_TAILNET_IP:$NAS_PORTAL_PORT   (tailnet: encrypted; use this from your phone)
  http://$HEARSAY_LAN_IP:$NAS_PORTAL_PORT   (LAN: unencrypted Wi-Fi)
  username: $(env_get "$NAS_PORTAL_ENV" HEARSAY_PORTAL_USER)
  password: sudo sed -n 's/^HEARSAY_PORTAL_PASSWORD=//p' $NAS_PORTAL_ENV
EOF
