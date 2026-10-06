#!/usr/bin/env bash
# Dev box GPU worker: an hourly systemd user timer that transcribes and
# diarizes conversation WAVs from the NAS (hearsay/transcribe.py) and writes
# the transcripts back. Run as your normal user on the dev box, after
# install/nas.sh. Safe to re-run; also how updates are deployed.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

GPU_VENV="$HOME/.local/share/hearsay/gpu-venv"
DEV_ENV="$HOME/.config/hearsay/dev.env"
UNIT_DIR="$HOME/.config/systemd/user"
DIARIZATION_MODEL="pyannote/speaker-diarization-community-1"

[ "$(id -u)" -ne 0 ] || die "run as your normal user, not root: the timer and its config are per user"

say "Checking the GPU"
# A driver/library version mismatch after a driver update also lands here.
nvidia-smi -L >/dev/null 2>&1 || die "nvidia-smi fails. If it reports a version mismatch, reboot, then re-run."
nvidia-smi -L

say "Checking ssh to the NAS works unattended"
# The timer has no terminal and no ssh agent, so the key must work without either.
env -u SSH_AUTH_SOCK ssh -o BatchMode=yes nas true \
    || die "'ssh nas' needs a key that works without a passphrase prompt or agent. Fix ~/.ssh, then re-run."

say "Checking the NAS side (install/nas.sh)"
ssh -o BatchMode=yes nas "test -w '$NAS_TRANSCRIPTS' && test -r '$NAS_ROOT/db/hearsay.sqlite'" \
    || die "the NAS isn't ready: run install/nas.sh there, then a reprocess, then re-run this."

say "Python 3.12 environment in $GPU_VENV"
# 3.12: the Python the transcribe extras are pinned and tested against.
command -v uv >/dev/null || die "install uv first: https://docs.astral.sh/uv/getting-started/installation/"
uv python install 3.12
[ -x "$GPU_VENV/bin/python" ] || uv venv --python 3.12 "$GPU_VENV"
VIRTUAL_ENV="$GPU_VENV" uv pip install --quiet -e "$REPO_DIR[transcribe]"
"$GPU_VENV/bin/python" -c "import torch; assert torch.cuda.is_available(), 'CUDA not available to torch'"

say "Hugging Face token in $DEV_ENV"
if [ -z "$(env_get "$DEV_ENV" HF_TOKEN)" ]; then
    # Manual: needs the operator's Hugging Face account.
    cat <<EOF
The diarization model is gated. In a browser, logged in to Hugging Face:
  1. Accept the terms at https://huggingface.co/$DIARIZATION_MODEL
  2. Create a read token at https://huggingface.co/settings/tokens
EOF
    read -rsp "Paste the token (hf_...): " token
    echo
    mkdir -p "$(dirname "$DEV_ENV")"
    env_ensure "$DEV_ENV" HF_TOKEN "$token"
fi

say "Checking the token can reach $DIARIZATION_MODEL"
HF_TOKEN="$(env_get "$DEV_ENV" HF_TOKEN)" "$GPU_VENV/bin/python" -c "
import os, sys
from huggingface_hub import auth_check
try:
    auth_check('$DIARIZATION_MODEL', token=os.environ['HF_TOKEN'])
except Exception as e:
    sys.exit(f'no access ({type(e).__name__}): accept the terms at https://huggingface.co/$DIARIZATION_MODEL')
" || die "fix model access, then re-run"

say "Hourly timer"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/hearsay-transcribe.service" <<EOF
[Unit]
Description=Hearsay: transcribe conversation audio from the NAS

[Service]
Type=oneshot
EnvironmentFile=$DEV_ENV
ExecStart=$GPU_VENV/bin/python -m hearsay.transcribe
EOF
cat > "$UNIT_DIR/hearsay-transcribe.timer" <<EOF
[Unit]
Description=Hearsay: transcribe new conversation audio hourly

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now hearsay-transcribe.timer
# Linger keeps user timers running when you're not logged in.
if ! loginctl show-user "$USER" -p Linger | grep -q yes; then
    loginctl enable-linger "$USER" || die "run: sudo loginctl enable-linger $USER, then re-run this"
fi

say "First run (transcribes everything pending; can take a few minutes)"
systemctl --user start hearsay-transcribe.service \
    || die "the run failed: journalctl --user -u hearsay-transcribe -n 50"
journalctl --user -u hearsay-transcribe -n 20 --no-pager -o cat

say "Done. Reprocess on the NAS to turn the new transcripts into turns."
