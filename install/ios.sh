#!/usr/bin/env bash
# The operator's iPhone, from the operator's Mac: builds the pendant app
# (ios/) and installs it on the phone over USB or Wi-Fi. Run as your user,
# from a checkout. Safe to re-run; also how updates are deployed, and how a
# free Apple ID's 7-day install is renewed.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

IOS_ENV="$HOME/.config/hearsay/ios.env"
IOS_BUILD="$HOME/Library/Caches/hearsay/ios-build"

[ "$(uname)" = Darwin ] || die "this runs on the Mac; see docs/install.md for the other hosts"
[ "$(id -u)" -ne 0 ] || die "run as your user, not root"
mkdir -p "$(dirname "$IOS_ENV")"

say "Checking Xcode"
# Manual: Xcode comes from the App Store with the operator's Apple ID, and
# selecting it needs sudo.
if ! xcodebuild -version >/dev/null 2>&1; then
    cat <<MSG
Xcode itself is needed (the command line tools can't build for iOS):
  1. Install Xcode from the App Store and open it once.
  2. sudo xcode-select -s /Applications/Xcode.app
  3. sudo xcodebuild -license accept
MSG
    die "re-run this script once xcodebuild -version works"
fi
xcodebuild -version | head -1
if ! xcodebuild -showsdks 2>/dev/null | grep -q iphoneos; then
    echo "Downloading the iOS platform (several GB)..."
    xcodebuild -downloadPlatform iOS
fi

say "Signing"
# Team IDs are the OU of the Apple Development certificates in the keychain.
signing_teams() {
    security find-certificate -a -c "Apple Development" -p 2>/dev/null \
        | awk '/BEGIN CERT/{c=""} {c=c $0 "\n"} /END CERT/{printf "%s", c | "openssl x509 -noout -subject -nameopt multiline"; close("openssl x509 -noout -subject -nameopt multiline")}' \
        | sed -n 's/^ *organizationalUnitName *= *//p' | sort -u
}
team="$(env_get "$IOS_ENV" HEARSAY_IOS_TEAM)"
if [ -n "$team" ] && ! [[ "$team" =~ ^[A-Z0-9]{10}$ ]]; then
    echo "dropping saved team '$team': a team ID is 10 letters and digits"
    sed -i '' '/^HEARSAY_IOS_TEAM=/d' "$IOS_ENV"
fi
if [ -z "$(env_get "$IOS_ENV" HEARSAY_IOS_TEAM)" ]; then
    # Manual: signing uses the operator's Apple ID, which only they can add.
    if [ -z "$(signing_teams)" ]; then
        cat <<MSG
In Xcode > Settings > Accounts, add your Apple ID (a free one works; installs
then expire after 7 days and re-running this script renews them). Then select
its team, click Manage Certificates..., and add an Apple Development
certificate with +.
MSG
        confirm "Done?"
    fi
    teams="$(signing_teams)"
    [ -n "$teams" ] || die "no Apple Development certificate in the keychain yet; see above"
    if [ "$(echo "$teams" | wc -l)" -eq 1 ]; then
        team="$teams"
    else
        echo "$teams" | nl -w2 -s') '
        read -rp "Which team? [1]: " pick
        team="$(echo "$teams" | sed -n "${pick:-1}p")"
    fi
    [[ "$team" =~ ^[A-Z0-9]{10}$ ]] || die "'$team' isn't a team ID"
    echo "team: $team"
    env_ensure "$IOS_ENV" HEARSAY_IOS_TEAM "$team"
fi
if [ -z "$(env_get "$IOS_ENV" HEARSAY_IOS_BUNDLE_ID)" ]; then
    # Free teams can't use an id another team has registered, so it's personal.
    default="com.$(id -un | tr -cd 'a-zA-Z0-9').hearsay"
    read -rp "Bundle id [$default]: " bundle
    env_ensure "$IOS_ENV" HEARSAY_IOS_BUNDLE_ID "${bundle:-$default}"
fi
team="$(env_get "$IOS_ENV" HEARSAY_IOS_TEAM)"
bundle="$(env_get "$IOS_ENV" HEARSAY_IOS_BUNDLE_ID)"

say "Finding the phone"
devices_json() {
    local out
    out="$(mktemp)"
    xcrun devicectl list devices --json-output "$out" >/dev/null 2>&1 || true
    python3 - "$out" <<'PY'
import json, sys
try:
    devices = json.load(open(sys.argv[1]))["result"]["devices"]
except Exception:
    devices = []
for d in devices:
    if d.get("hardwareProperties", {}).get("platform") == "iOS":
        print(d["identifier"], d.get("deviceProperties", {}).get("name", "?"), sep="\t")
PY
    rm -f "$out"
}
if [ -z "$(env_get "$IOS_ENV" HEARSAY_IOS_DEVICE)" ]; then
    # Manual: pairing and Developer Mode are confirmed on the phone itself.
    cat <<MSG
Connect the iPhone to this Mac with a cable, unlock it and tap Trust. Turn on
Settings > Privacy & Security > Developer Mode (the phone restarts).
MSG
    confirm "Done?"
    found="$(devices_json)"
    [ -n "$found" ] || die "no iPhone found; check the cable and that it trusts this Mac"
    echo "$found" | nl -w2 -s') '
    read -rp "Which one? [1]: " pick
    device="$(echo "$found" | sed -n "${pick:-1}p" | cut -f1)"
    [ -n "$device" ] || die "no such device"
    env_ensure "$IOS_ENV" HEARSAY_IOS_DEVICE "$device"
fi
device="$(env_get "$IOS_ENV" HEARSAY_IOS_DEVICE)"
devices_json | grep -q "^$device" \
    || die "the phone ($device) isn't reachable; connect it by cable or put it on the same Wi-Fi, unlocked"

say "Building"
xcodebuild -project "$REPO_DIR/ios/Hearsay.xcodeproj" -target Hearsay -configuration Release \
    -sdk iphoneos -allowProvisioningUpdates -allowProvisioningDeviceRegistration \
    DEVELOPMENT_TEAM="$team" PRODUCT_BUNDLE_IDENTIFIER="$bundle" SYMROOT="$IOS_BUILD" \
    -quiet build
app="$IOS_BUILD/Release-iphoneos/Hearsay.app"

say "Installing on the phone"
# Reinstalling keeps the app's data: its spool, settings and paired pendant.
xcrun devicectl device install app --device "$device" "$app"
# Manual: a free Apple ID's developer profile has to be trusted on the phone,
# once per Apple ID.
cat <<MSG
The first time, iOS refuses to open the app until you trust its developer:
Settings > General > VPN & Device Management > your Apple ID > Trust.
MSG

say "Capture receiver settings"
# Manual: the app's settings are entered on the phone.
url="$(env_get "$IOS_ENV" HEARSAY_CAPTURE_URL)"
if [ -z "$url" ]; then
    read -rp "Capture URL (printed by install/nas.sh, http://<nas tailnet ip>:$NAS_CAPTURE_PORT/capture): " url
    env_ensure "$IOS_ENV" HEARSAY_CAPTURE_URL "$url"
fi
echo "Checking the NAS answers at $url"
wait_for_401 "$url" 10
# The naming portal runs on the same NAS address (install/compose.yaml).
portal="${url%:"$NAS_CAPTURE_PORT"/*}:$NAS_PORTAL_PORT"
echo "Checking the portal answers at $portal"
wait_for_status GET "$portal/login" 200 10
cat <<MSG
The phone needs Tailscale on (the Tailscale app, connected) to reach the NAS.
In the Hearsay app, under Recorder > Server, enter:
  Capture URL:  $url
  Token:        on the NAS: sudo sed -n 's/^HEARSAY_CAPTURE_TOKEN=//p' $NAS_CAPTURE_ENV
  Portal URL:   $portal
Then tap Save. The token is kept in the phone's Keychain, not on this Mac.
The Voices tab shows the portal, logged in with the capture token.
MSG
confirm "Saved?"

say "Connecting to the pendant"
# Manual: the pendant takes one connection at a time, and Bluetooth
# permission is the operator's to grant.
cat <<MSG
The pendant accepts one connection at a time: disconnect it from Omi's app
(or delete that app). Open Hearsay and allow Bluetooth. Within a minute it
should show "connected" with audio packets counted, and within two an upload
"sent at" a time.
MSG
confirm "Is it connected and sending?"

say "Location while recording"
# Manual: location permission is the operator's to grant, on the phone.
cat <<MSG
While it records, Hearsay notes roughly where you are every 2 minutes, so
conversations can say which place you named they happened at (the portal's
Places page); coordinates never reach the stream. When iOS asks, allow
location, and later choose "Change to Always Allow": the app records in the
background. Check: Settings > Hearsay > Location says Always.
MSG
confirm "Is location set to Always?"

say "Recording from the phone. The next hourly reprocess on the NAS picks the uploads up."
