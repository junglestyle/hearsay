#!/usr/bin/env bash
# Local development setup: venv, dev secret outside the repo, tests.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

DEV_ENV="$HOME/.config/hearsay/dev.env"
DEV_RAW="$HOME/.local/share/hearsay/raw"

say "Checking Python >= 3.12"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' \
    || die "need python3 >= 3.12, have $(python3 --version)"

say "Installing into $REPO_DIR/.venv"
[ -d "$REPO_DIR/.venv" ] || python3 -m venv "$REPO_DIR/.venv"
"$REPO_DIR/.venv/bin/pip" install --quiet -e "$REPO_DIR[dev]"

say "Dev config at $DEV_ENV"
mkdir -p "$(dirname "$DEV_ENV")" "$DEV_RAW"
env_ensure "$DEV_ENV" HEARSAY_SECRET "$(openssl rand -hex 32)"
env_ensure "$DEV_ENV" HEARSAY_RAW_DIR "$DEV_RAW"

say "Running tests"
(cd "$REPO_DIR" && .venv/bin/pytest -q)

say "Done. Run the receiver locally with:"
echo "  (set -a; . $DEV_ENV; cd $REPO_DIR && .venv/bin/uvicorn --factory hearsay.receiver:app_from_env --no-access-log)"
