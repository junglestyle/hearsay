FROM python:3.12-slim AS app

# Install from a throwaway copy: source files keep the host clone's modes
# (root-only under a 077 umask), which the non-root runtime user can't read.
COPY pyproject.toml /src/
COPY hearsay /src/hearsay
RUN pip install --no-cache-dir /src && rm -rf /src

# The test suite, for running on the NAS where real captures are mounted.
FROM app AS test
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
