"""The capture receiver: authenticate, then write the body to disk verbatim.

It takes our own recorders' uploads (hearsay/capture.py) and listens on the
tailnet only, since its bodies are audio. Its token is a bearer header,
never written to disk or logged.

Omi's webhooks used to land here too, through a public Cloudflare tunnel;
both were removed once Omi was no longer used (its last payload is from
2026-09-30). Those raw payloads are kept and still parsed (hearsay/parse.py).

Rejected requests are logged (whether a token was sent, and the client
address), so a sender with a wrong token shows up in the journal instead of
vanishing.
"""

import hashlib
import hmac
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request, Response

log = logging.getLogger("hearsay.receiver")


def write_atomically(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def authorized(request: Request, kind: str, token: str, secret: str) -> bool:
    if hmac.compare_digest(token.encode(), secret.encode()):
        return True
    log.warning(
        "rejected %s upload: %s token, client %s",
        kind,
        "wrong" if token else "no",
        request.client.host if request.client else "unknown",
    )
    return False


async def store(raw_dir: Path, request: Request, kind: str) -> None:
    body = await request.body()
    now = datetime.now(timezone.utc)
    day_dir = raw_dir / kind / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{now.strftime('%Y%m%dT%H%M%S.%fZ')}-{uuid.uuid4().hex[:8]}"

    sidecar = {
        "received_at": now.isoformat(),
        "webhook_type": kind,
        "method": request.method,
        "path": request.url.path,
        "query": [[k, v] for k, v in request.query_params.multi_items() if k != "token"],
        "headers": [[k, v] for k, v in request.headers.items() if k != "authorization"],
        "body_bytes": len(body),
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "body_file": f"{stem}.body",
    }
    # Body first: a sidecar only exists once its body is complete.
    write_atomically(day_dir / f"{stem}.body", body)
    write_atomically(day_dir / f"{stem}.json", json.dumps(sidecar, indent=2).encode())


def create_capture_app(raw_dir: Path, token: str) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/capture")
    async def capture(request: Request) -> Response:
        sent = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not authorized(request, "capture", sent, token):
            return Response(status_code=401)
        await store(raw_dir, request, "capture")
        return Response(status_code=200)

    return app


def capture_app_from_env() -> FastAPI:
    token = os.environ["HEARSAY_CAPTURE_TOKEN"]
    if not token:
        raise RuntimeError("HEARSAY_CAPTURE_TOKEN is empty")
    return create_capture_app(Path(os.environ["HEARSAY_RAW_DIR"]), token)

