FROM python:3.12-slim

# Install from a throwaway copy: source files keep the host clone's modes
# (root-only under a 077 umask), which the non-root runtime user can't read.
COPY pyproject.toml /src/
COPY hearsay /src/hearsay
RUN pip install --no-cache-dir /src && rm -rf /src

EXPOSE 8000
# --no-access-log: the shared secret is in the query string, which access logs print.
CMD ["uvicorn", "--factory", "hearsay.receiver:app_from_env", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
