#!/usr/bin/env bash
# TrueNAS SCALE: datasets, receiver secret, receiver container.
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
for ds in "$NAS_POOL_DATASET" "$NAS_POOL_DATASET/config" "$NAS_POOL_DATASET/raw"; do
    if zfs list -H -o name "$ds" >/dev/null 2>&1; then
        echo "exists: $ds"
    else
        midclt call pool.dataset.create "{\"name\": \"$ds\"}" >/dev/null
        echo "created: $ds"
    fi
done
[ -d "$NAS_RAW" ] && [ -d "$NAS_CONFIG" ] || die "datasets not mounted under $NAS_ROOT"

chown root:root "$NAS_CONFIG"
chmod 700 "$NAS_CONFIG"
chown "$APPS_UID:$APPS_GID" "$NAS_RAW"
chmod 750 "$NAS_RAW"

say "Receiver secret in $NAS_RECEIVER_ENV"
env_ensure "$NAS_RECEIVER_ENV" HEARSAY_SECRET "$(openssl rand -hex 32)"

say "Starting receiver"
docker compose -f "$REPO_DIR/install/compose.yaml" up -d --build

say "Verifying receiver rejects unauthenticated requests"
wait_for_401 "http://127.0.0.1:$NAS_RECEIVER_PORT/omi/transcript" 30

say "Receiver is up on 127.0.0.1:$NAS_RECEIVER_PORT. Next: install/tunnel.sh"
