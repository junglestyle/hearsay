#!/usr/bin/env bash
# Cloudflare Tunnel to the receiver, then point Omi at it and verify real deliveries.
# Run as root on the NAS after install/nas.sh. Safe to re-run.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

require_root

say "Checking receiver (install/nas.sh)"
[ -f "$NAS_RECEIVER_ENV" ] || die "no $NAS_RECEIVER_ENV. Run install/nas.sh first."
[ "$(docker inspect -f '{{.State.Running}}' hearsay 2>/dev/null)" = "true" ] \
    || die "hearsay container isn't running. Run install/nas.sh first."
SECRET="$(env_get "$NAS_RECEIVER_ENV" HEARSAY_SECRET)"

if [ -z "$(env_get "$NAS_TUNNEL_ENV" HEARSAY_PUBLIC_HOST)" ]; then
    say "Public hostname"
    read -rp "Hostname for the receiver, in your Cloudflare zone (e.g. hearsay.example.com): " host
    env_ensure "$NAS_TUNNEL_ENV" HEARSAY_PUBLIC_HOST "$host"
fi
HOST="$(env_get "$NAS_TUNNEL_ENV" HEARSAY_PUBLIC_HOST)"

if [ -z "$(env_get "$NAS_TUNNEL_ENV" TUNNEL_TOKEN)" ]; then
    # Manual: needs the operator's Cloudflare account.
    say "Create the tunnel in Cloudflare"
    cat <<EOF
In the Cloudflare dashboard:
  1. Zero Trust -> Networks -> Tunnels -> Create a tunnel -> Cloudflared.
  2. Name it: hearsay
  3. On the install step, copy the token (the long string after --token /
     in 'cloudflared service install <TOKEN>'). Don't run the install command.
  4. Public hostname: $HOST
     Service: HTTP, URL: localhost:$NAS_RECEIVER_PORT
  5. Save the tunnel.
EOF
    read -rsp "Paste the tunnel token: " token
    echo
    env_ensure "$NAS_TUNNEL_ENV" TUNNEL_TOKEN "$token"
fi

say "Starting cloudflared"
docker compose -f "$REPO_DIR/install/tunnel.compose.yaml" up -d

say "Verifying https://$HOST reaches the receiver"
wait_for_401 "https://$HOST/omi/transcript" 60

# Manual: the webhook URLs are set in the Omi app on the operator's phone.
say "Point Omi at the receiver"
cat <<EOF
In the Omi app: Settings -> Developer Settings. For each webhook below, paste
the full URL (path and token included) into its "Endpoint URL" and turn it on:

  Conversation Events:  https://$HOST/omi/memory?token=$SECRET
  Real-time Transcript: https://$HOST/omi/transcript?token=$SECRET
  Audio Bytes:          https://$HOST/omi/audio?token=$SECRET
    (if an interval field is shown, use 5)

These URLs contain the secret. Don't paste them anywhere else.
EOF
confirm "Saved all three in the Omi app?"

# Returns 0 once a sidecar newer than the marker appears under raw/<type>.
wait_for_payload() {
    local type="$1" marker="$2" seconds="$3"
    for _ in $(seq "$seconds"); do
        if [ -d "$NAS_RAW/$type" ] && [ -n "$(find "$NAS_RAW/$type" -name '*.json' -newer "$marker" -print -quit)" ]; then
            echo "received: $type"
            return 0
        fi
        sleep 1
    done
    echo "MISSING:  $type"
    return 1
}

marker="$(mktemp)"
trap 'rm -f "$marker"' EXIT
ok=true

say "Verifying live deliveries"
echo "Wear the pendant and talk for a minute (live, connected to the phone). Waiting up to 5 minutes..."
wait_for_payload transcript "$marker" 300 || ok=false
wait_for_payload audio "$marker" 60 || ok=false

echo
# Conversation events fire only after a conversation ends and Omi processes it.
echo "Now end the conversation: stop it in the Omi app, or stay silent for a few minutes."
echo "Waiting up to 10 minutes..."
wait_for_payload memory "$marker" 600 || ok=false

if [ "$ok" = true ]; then
    say "All three webhook types are landing in $NAS_RAW"
else
    die "some webhook types didn't arrive. Check the URLs and toggles in Omi, then re-run this script."
fi
