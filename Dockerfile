FROM python:3.12-slim AS app

# Install from a throwaway copy: source files keep the host clone's modes
# (root-only under a 077 umask), which the non-root runtime user can't read.
COPY pyproject.toml /src/
COPY hearsay /src/hearsay
RUN pip install --no-cache-dir /src && rm -rf /src

# Reprocessing: adds torch (CPU) and the speaker model, pinned to a commit and
# baked in so runs are offline and repeatable.
FROM app AS worker
COPY pyproject.toml /src/
COPY hearsay /src/hearsay
RUN pip install --no-cache-dir torch==2.14.0 torchaudio==2.11.0 --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir "/src[speakers]" && rm -rf /src
# One load at build time: speechbrain adds files (symlinks) to the model dir on
# first load, which the non-root runtime user can't write.
RUN python -c "from huggingface_hub import snapshot_download; snapshot_download('speechbrain/spkrec-ecapa-voxceleb', revision='0f99f2d0ebe89ac095bcc5903c4dd8f72b367286', local_dir='/models/ecapa')" \
    && HF_HUB_OFFLINE=1 python -c "from pathlib import Path; from hearsay.speakers import load_model; load_model(Path('/models/ecapa'))" \
    && chmod -R a+rX /models
ENV HEARSAY_MODEL_DIR=/models/ecapa HF_HUB_OFFLINE=1
# Last in the stage, so a new commit doesn't rebuild the layers above.
ARG HEARSAY_COMMIT=unknown
ENV HEARSAY_COMMIT=$HEARSAY_COMMIT

# The test suite, for running on the NAS where real captures are mounted.
FROM worker AS test
COPY pyproject.toml /src/
COPY hearsay /src/hearsay
RUN pip install --no-cache-dir "/src[dev]" && rm -rf /src
COPY tests /tests
RUN chmod -R a+rX /tests
ENV PYTHONDONTWRITEBYTECODE=1

# Last stage: what a plain `build` produces.
FROM app AS receiver
EXPOSE 8000
# --no-access-log: the shared secret is in the query string, which access logs print.
CMD ["uvicorn", "--factory", "hearsay.receiver:app_from_env", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
