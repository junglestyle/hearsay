FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY hearsay ./hearsay
RUN pip install --no-cache-dir .

EXPOSE 8000
# --no-access-log: the shared secret is in the query string, which access logs print.
CMD ["uvicorn", "--factory", "hearsay.receiver:app_from_env", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
