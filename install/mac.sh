#!/usr/bin/env bash
# The operator's personal Mac: builds the menu-bar recorder (mac/) into an
# app bundle and runs it at login as a launchd agent that uploads to the
# NAS's capture receiver over the tailnet. Run as your user, from a checkout.
# Safe to re-run; also how updates are deployed. Never on a work machine.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

MAC_ENV="$HOME/.config/hearsay/mac.env"
MAC_APP="$HOME/Applications/Hearsay Recorder.app"
MAC_BIN="$MAC_APP/Contents/MacOS/hearsay-mac"
MAC_LABEL="com.hearsay.mac"
MAC_PLIST="$HOME/Library/LaunchAgents/$MAC_LABEL.plist"
MAC_LOG="$HOME/Library/Logs/hearsay-mac.log"
# The last build installed, to tell an unchanged one (signing alters the
# bundle's copy, so that can't be compared).
MAC_BUILT="$HOME/Library/Caches/hearsay/hearsay-mac.built"

[ "$(uname)" = Darwin ] || die "this is for the Mac; see README.md for the other hosts"
[ "$(id -u)" -ne 0 ] || die "run as your user, not root: the recorder is a per-user launchd agent"
major="$(sw_vers -productVersion | cut -d. -f1)"
minor="$(sw_vers -productVersion | cut -d. -f2)"
{ [ "$major" -gt 14 ] || { [ "$major" -eq 14 ] && [ "${minor:-0}" -ge 4 ]; }; } \
    || die "macOS 14.4 or later is needed (process audio taps)"

say "Checking the Swift toolchain"
# Manual: installing Xcode or its command line tools needs the operator's Apple ID or approval.
command -v swift >/dev/null || die "no swift. Install Xcode's command line tools (xcode-select --install), then re-run."

say "Building the recorder"
(cd "$REPO_DIR/mac" && swift build -c release)
built="$REPO_DIR/mac/.build/release/hearsay-mac"
changed=0
mkdir -p "$MAC_APP/Contents/MacOS" "$(dirname "$MAC_BUILT")"
if ! cmp -s "$REPO_DIR/mac/Info.plist" "$MAC_APP/Contents/Info.plist" || ! cmp -s "$built" "$MAC_BUILT"; then
    cp "$REPO_DIR/mac/Info.plist" "$MAC_APP/Contents/Info.plist"
    cp "$built" "$MAC_BIN"
    cp "$built" "$MAC_BUILT"
    # macOS keeps mic and system-audio permission per signing identity. With
    # an Apple Development certificate (install/ios.sh sets one up) it holds
    # across updates; signed ad hoc, macOS asks again after every update.
    identity="$(security find-identity -v -p codesigning 2>/dev/null | awk '/Apple Development/ {print $2; exit}')"
    codesign --force --sign "${identity:--}" "$MAC_APP"
    echo "installed: $MAC_APP (signed ${identity:+with Apple Development}${identity:-ad hoc})"
    changed=1
else
    echo "up to date: $MAC_APP"
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
chmod 600 "$MAC_ENV"
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
    <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
    <key>LimitLoadToSessionType</key><string>Aqua</string>
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
    # bootout returns before the old agent is gone; bootstrapping over it
    # fails with "Bootstrap failed: 5".
    for _ in $(seq 10); do
        launchctl print "gui/$(id -u)/$MAC_LABEL" >/dev/null 2>&1 || break
        sleep 1
    done
    launchctl bootstrap "gui/$(id -u)" "$MAC_PLIST"
    echo "started: $MAC_LABEL (log: $MAC_LOG)"
else
    echo "running: $MAC_LABEL (log: $MAC_LOG)"
    say "Up to date. Watch it: tail -f $MAC_LOG"
    exit 0
fi

say "A test recording"
# Manual: macOS asks the operator in person for mic and system audio access.
from="$(($(wc -l < "$MAC_LOG") + 1))"
cat <<MSG
In the menu bar, click the mic icon, then Record. Allow microphone and
system audio recording when macOS asks (System Settings > Privacy & Security
lists both, if you missed them). Say something and play a few seconds of any
sound, then click the icon and Stop.
MSG
confirm "Done?"
echo "Waiting up to a minute for the recording to reach the NAS..."
for _ in $(seq 60); do
    tail -n "+$from" "$MAC_LOG" | grep -q "uploaded" && break
    sleep 1
done
tail -n "+$from" "$MAC_LOG"
tail -n "+$from" "$MAC_LOG" | grep -Eq "stopped: mic [1-9][0-9.]*s, system [1-9][0-9.]*s" \
    || die "the test recording is missing a channel; see $MAC_LOG"
tail -n "+$from" "$MAC_LOG" | grep -q "uploaded" || die "nothing uploaded yet; see $MAC_LOG"

say "Recording works. When a Zoom call starts, the recorder asks before recording it."
