# Shared helpers for install scripts. Source this; don't run it.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# NAS layout (TrueNAS SCALE, pool "storage").
NAS_POOL_DATASET="storage/hearsay"
NAS_ROOT="/mnt/storage/hearsay"
NAS_CONFIG="$NAS_ROOT/config"
NAS_RAW="$NAS_ROOT/raw"
NAS_DB="$NAS_ROOT/db"
NAS_AUDIO="$NAS_ROOT/audio"
NAS_RECEIVER_ENV="$NAS_CONFIG/receiver.env"
NAS_TUNNEL_ENV="$NAS_CONFIG/tunnel.env"
NAS_RECEIVER_PORT=8787
APPS_UID=568
APPS_GID=568

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

confirm() {
    local reply
    while true; do
        read -rp "$1 [y/N] " reply
        case "$reply" in
            y|Y|yes) return 0 ;;
            *) echo "Waiting. Answer y when done (Ctrl-C to abort)." ;;
        esac
    done
}

require_root() {
    [ "$(id -u)" -eq 0 ] || die "run as root: sudo $0"
}

# Env files are plain KEY=VALUE lines, readable by both bash and compose env_file.
env_get() {
    local file="$1" key="$2"
    [ -f "$file" ] || return 0
    sed -n "s/^${key}=//p" "$file" | tail -n 1
}

# Appends KEY only if it's absent, so re-runs never rotate existing values.
env_ensure() {
    local file="$1" key="$2" value="$3"
    [ -n "$(env_get "$file" "$key")" ] && return 0
    [ -n "$value" ] || die "empty value for $key"
    (umask 077; touch "$file")
    printf '%s=%s\n' "$key" "$value" >> "$file"
    chmod 600 "$file"
}

# POSTs without a token and waits until the receiver answers 401.
# Any other code (502, 530, ...) means something in front of it isn't up yet.
wait_for_401() {
    local url="$1" seconds="$2" code=""
    for _ in $(seq "$seconds"); do
        code="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$url" || true)"
        [ "$code" = "401" ] && return 0
        sleep 1
    done
    die "expected 401 from $url, last got '$code'"
}
