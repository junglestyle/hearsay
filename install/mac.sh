#!/usr/bin/env bash
# The operator's Mac: builds the pendant recorder (capture/) and runs it as a
# launchd agent that uploads to the NAS's capture receiver over the tailnet.
# Run as your user, from a checkout. Safe to re-run; also how updates are deployed.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

MAC_ENV="$HOME/.config/hearsay/capture.env"
MAC_BIN="$HOME/.local/bin/hearsay-capture"
MAC_LABEL="com.hearsay.capture"
MAC_PLIST="$HOME/Library/LaunchAgents/$MAC_LABEL.plist"
MAC_LOG="$HOME/Library/Logs/hearsay-capture.log"

[ "$(uname)" = Darwin ] || die "this is for the Mac; see README.md for the other hosts"
[ "$(id -u)" -ne 0 ] || die "run as your user, not root: the recorder is a per-user launchd agent"

say "Checking the Swift toolchain"
# Manual: installing Xcode or its command line tools needs the operator's Apple ID or approval.
command -v swift >/dev/null || die "no swift. Install Xcode's command line tools (xcode-select --install), then re-run."

say "Building the recorder"
(cd "$REPO_DIR/capture" && swift build -c release)
mkdir -p "$(dirname "$MAC_BIN")"
changed=0
if ! cmp -s "$REPO_DIR/capture/.build/release/hearsay-capture" "$MAC_BIN"; then
    cp "$REPO_DIR/capture/.build/release/hearsay-capture" "$MAC_BIN.new"
    mv "$MAC_BIN.new" "$MAC_BIN"
    changed=1
    echo "installed: $MAC_BIN"
else
    echo "up to date: $MAC_BIN"
fi

say "Capture receiver settings in $MAC_ENV"
mkdir -p "$(dirname "$MAC_ENV")"
# Manual: the URL and token come from the NAS; install/nas.sh prints both.
if [ -z "$(env_get "$MAC_ENV" HEARSAY_CAPTURE_URL)" ]; then
    read -rp "Capture URL (printed by install/nas.sh, http://<nas tailnet ip>:$NAS_CAPTURE_PORT/capture): " url
    env_ensure "$MAC_ENV" HEARSAY_CAPTURE_URL "$url"
fi
if [ -z "$(env_get "$MAC_ENV" HEARSAY_CAPTURE_TOKEN)" ]; then
    read -rsp "Capture token (on the NAS: sudo sed -n 's/^HEARSAY_CAPTURE_TOKEN=//p' $NAS_CAPTURE_ENV): " token
    echo
    env_ensure "$MAC_ENV" HEARSAY_CAPTURE_TOKEN "$token"
fi
url="$(env_get "$MAC_ENV" HEARSAY_CAPTURE_URL)"

say "Checking the NAS answers at $url"
# 401 without the token: reachable over the tailnet, and nothing is stored.
wait_for_401 "$url" 10

say "launchd agent $MAC_LABEL"
mkdir -p "$(dirname "$MAC_PLIST")" "$(dirname "$MAC_LOG")"
touch "$MAC_LOG"
plist="$(cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$MAC_LABEL</string>
    <key>ProgramArguments</key><array><string>$MAC_BIN</string></array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$MAC_LOG</string>
    <key>StandardErrorPath</key><string>$MAC_LOG</string>
</dict>
</plist>
PLIST
)"
if [ "$plist" != "$(cat "$MAC_PLIST" 2>/dev/null)" ]; then
    printf '%s\n' "$plist" > "$MAC_PLIST"
    changed=1
fi
if [ "$changed" = 1 ] || ! launchctl print "gui/$(id -u)/$MAC_LABEL" >/dev/null 2>&1; then
    launchctl bootout "gui/$(id -u)/$MAC_LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$MAC_PLIST"
    echo "started: $MAC_LABEL (log: $MAC_LOG)"
else
    echo "running: $MAC_LABEL (log: $MAC_LOG)"
fi

say "Connecting to the pendant"
# Manual: the pendant takes one connection at a time, and its current one is
# on the operator's phone; Bluetooth permission is the operator's to grant.
cat <<MSG
The pendant accepts one connection at a time. If Omi's app on your phone is
connected to it, disconnect it there (or turn off the phone's Bluetooth).
If macOS asks whether hearsay-capture may use Bluetooth, allow it (System
Settings > Privacy & Security > Bluetooth).
MSG
confirm "Done?"
# The recorder logs a line a minute; wait for a new one that shows audio.
from="$(($(wc -l < "$MAC_LOG") + 1))"
echo "Waiting up to 2 minutes for audio from the pendant..."
for _ in $(seq 120); do
    tail -n "+$from" "$MAC_LOG" | grep -q "^[^ ]* connected: [1-9]" && break
    sleep 1
done
tail -n "+$from" "$MAC_LOG"
tail -n "+$from" "$MAC_LOG" | grep -q "^[^ ]* connected: [1-9]" \
    || die "no audio from the pendant yet; see $MAC_LOG"

say "Recording. Uploads go to the NAS every minute; the next hourly reprocess picks them up."
echo "Watch it: tail -f $MAC_LOG"
