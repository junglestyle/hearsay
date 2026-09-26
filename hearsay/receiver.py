"""Omi webhook receiver: authenticate, then write the body to disk verbatim.

Omi can't send custom headers or sign requests, so the shared secret travels
as a `token` query parameter. It is never written to disk.
"""

import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request, Response


def write_atomically(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def create_app(raw_dir: Path, secret: str) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    async def store(request: Request, webhook_type: str) -> Response:
        token = request.query_params.get("token", "")
        if not hmac.compare_digest(token.encode(), secret.encode()):
            return Response(status_code=401)

        body = await request.body()
        now = datetime.now(timezone.utc)
        day_dir = raw_dir / webhook_type / now.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{now.strftime('%Y%m%dT%H%M%S.%fZ')}-{uuid.uuid4().hex[:8]}"

        sidecar = {
            "received_at": now.isoformat(),
            "webhook_type": webhook_type,
            "method": request.method,
            "path": request.url.path,
            "query": [[k, v] for k, v in request.query_params.multi_items() if k != "token"],
            "headers": [[k, v] for k, v in request.headers.items()],
            "body_bytes": len(body),
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "body_file": f"{stem}.body",
        }
        # Body first: a sidecar only exists once its body is complete.
        write_atomically(day_dir / f"{stem}.body", body)
        write_atomically(day_dir / f"{stem}.json", json.dumps(sidecar, indent=2).encode())

        # Empty 200: a JSON {"message": ...} on the transcript hook would push
        # a notification to the phone.
        return Response(status_code=200)

    @app.post("/omi/transcript")
    async def transcript(request: Request) -> Response:
        return await store(request, "transcript")

    @app.post("/omi/audio")
    async def audio(request: Request) -> Response:
        return await store(request, "audio")

    @app.post("/omi/memory")
    async def memory(request: Request) -> Response:
        return await store(request, "memory")

    return app


def app_from_env() -> FastAPI:
    secret = os.environ["HEARSAY_SECRET"]
    raw_dir = Path(os.environ["HEARSAY_RAW_DIR"])
    if not secret:
        raise RuntimeError("HEARSAY_SECRET is empty")
    return create_app(raw_dir, secret)
