"""Receivers: authenticate, then write the body to disk verbatim.

Two apps, run as separate containers. The Omi webhook receiver is public,
behind the Cloudflare tunnel. Omi can't send custom headers or sign
requests, so its shared secret travels as a `token` query parameter. The
capture receiver takes our own recorder's uploads (hearsay/capture.py) and
listens on the tailnet only, since its bodies are audio; its token is a
bearer header. Neither secret is ever written to disk or logged.

Rejected requests are logged (type, whether a token was sent, and the
Cloudflare client IP), so a sender with a wrong token shows up in
`docker logs` instead of vanishing.
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
        request.headers.get("cf-connecting-ip", "direct"),
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


def create_app(raw_dir: Path, secret: str) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    async def webhook(request: Request, webhook_type: str) -> Response:
        if not authorized(request, webhook_type, request.query_params.get("token", ""), secret):
            return Response(status_code=401)
        await store(raw_dir, request, webhook_type)

        # Empty 200: a JSON {"message": ...} on the transcript hook would push
        # a notification to the phone.
        return Response(status_code=200)

    @app.post("/omi/transcript")
    async def transcript(request: Request) -> Response:
        return await webhook(request, "transcript")

    @app.post("/omi/audio")
    async def audio(request: Request) -> Response:
        return await webhook(request, "audio")

    @app.post("/omi/memory")
    async def memory(request: Request) -> Response:
        return await webhook(request, "memory")

    return app


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


def app_from_env() -> FastAPI:
    secret = os.environ["HEARSAY_SECRET"]
    raw_dir = Path(os.environ["HEARSAY_RAW_DIR"])
    if not secret:
        raise RuntimeError("HEARSAY_SECRET is empty")
    return create_app(raw_dir, secret)
