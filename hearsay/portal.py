"""Web portal for naming anonymous speakers, from a phone or any browser.

Listens only on the LAN and tailnet addresses (install/compose.yaml), never
through the Cloudflare tunnel. Login is a plain HTML form, not an HTTP auth
dialog, so password managers can fill it; sessions are HMAC-signed cookies.

Names are written to labels/tags.jsonl, the same durable input reprocess
reads, with the time spans of the turns heard so they survive
re-transcription. The portal applies them to the current clusters as soon as
they are saved, using the same rules as reprocess (hearsay.turns.resolve and
hearsay.people.cluster_name).
"""

import asyncio
import hashlib
import hmac
import html
import io
import json
import os
import sqlite3
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from hearsay.people import cluster_name
from hearsay.turns import read_operator_files, resolve

COOKIE = "hearsay_session"
SESSION_SECONDS = 30 * 24 * 3600
# Samples played per cluster.
SAMPLES = 3
# Smaller clusters (mostly single noisy turns) aren't offered for naming.
MIN_TAG_CLUSTER = 3

CLUSTERS_SQL = """
SELECT tp.turn_id, tp.cluster, t.conversation_id, t.start, t.end, ca.wav_file
FROM turn_people tp
JOIN turns t ON t.turn_id = tp.turn_id
JOIN conversation_audio ca ON ca.conversation_id = t.conversation_id
ORDER BY tp.cluster, tp.turn_id
"""

STYLE = """
:root { color-scheme: light dark; --fg: #111; --bg: #fff; --muted: #666; --line: #ddd; --accent: #2458d6; }
@media (prefers-color-scheme: dark) { :root { --fg: #eee; --bg: #121212; --muted: #999; --line: #333; --accent: #7fa4ff; } }
body { font: 18px/1.4 system-ui, sans-serif; color: var(--fg); background: var(--bg); margin: 0 auto; max-width: 40rem; padding: 1rem; }
a { color: var(--accent); }
.muted { color: var(--muted); font-size: 0.9em; }
ul { list-style: none; padding: 0; }
li { border-bottom: 1px solid var(--line); padding: 0.75rem 0; }
li a { display: block; text-decoration: none; }
audio { width: 100%; margin: 0.4rem 0; }
label { display: block; margin: 0.8rem 0 0.3rem; }
input { font: inherit; width: 100%; box-sizing: border-box; padding: 0.6rem; }
button { font: inherit; width: 100%; padding: 0.8rem; margin-top: 0.6rem; }
.error { color: #c33; }
"""


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{STYLE}</style></head>"
        f"<body>{body}</body></html>"
    )


def wav_slice(path: Path, start: float, end: float) -> bytes:
    with wave.open(str(path)) as src:
        rate = src.getframerate()
        src.setpos(max(0, min(src.getnframes(), int(start * rate))))
        frames = src.readframes(max(0, int((end - start) * rate)))
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return out.getvalue()


def ranged(data: bytes, range_header: str | None) -> Response:
    """Serve bytes, honoring a single byte range. iOS Safari won't play audio without it."""
    headers = {"Accept-Ranges": "bytes"}
    if not range_header or not range_header.startswith("bytes=") or "," in range_header:
        return Response(data, media_type="audio/wav", headers=headers)
    first, _, last = range_header[len("bytes="):].partition("-")
    try:
        if first:
            start, end = int(first), int(last) if last else len(data) - 1
        else:
            start, end = max(0, len(data) - int(last)), len(data) - 1
    except ValueError:
        return Response(data, media_type="audio/wav", headers=headers)
    end = min(end, len(data) - 1)
    if start > end:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{len(data)}"})
    headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
    return Response(data[start : end + 1], status_code=206, media_type="audio/wav", headers=headers)


def samples(turns: list[dict]) -> list[dict]:
    """The longest turns, one per conversation first, so samples cover the cluster's spread."""
    by_length = sorted(turns, key=lambda s: (-(s["end"] - s["start"]), s["turn_id"]))
    picked, conversations = [], set()
    for s in by_length:
        if s["conversation_id"] not in conversations:
            picked.append(s)
            conversations.add(s["conversation_id"])
    picked += [s for s in by_length if s not in picked]
    return picked[:SAMPLES]


def create_app(db_path: Path, audio_dir: Path, labels_dir: Path, user: str, password: str, session_key: str) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    tags_path = labels_dir / "tags.jsonl"

    def sign(expires: int) -> str:
        return hmac.new(session_key.encode(), f"{user}:{expires}".encode(), hashlib.sha256).hexdigest()

    def logged_in(request: Request) -> bool:
        expires, _, signature = request.cookies.get(COOKIE, "").partition(".")
        if not expires.isdigit() or int(expires) < time.time():
            return False
        return hmac.compare_digest(signature, sign(int(expires)))

    def connect() -> sqlite3.Connection:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    def read_tags() -> tuple[dict[str, str], set[str], set[str]]:
        db = connect()
        try:
            found = resolve(db, *read_operator_files(labels_dir))
        finally:
            db.close()
        return found.names, found.mixed, found.skipped

    def load_clusters() -> dict[str, list[dict]]:
        db = connect()
        db.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in db.execute(CLUSTERS_SQL)]
        finally:
            db.close()
        grouped = {}
        for row in rows:
            grouped.setdefault(row["cluster"], []).append(row)
        return dict(sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0])))

    def unnamed(grouped, names, mixed, skipped) -> tuple[list[str], list[str]]:
        """Clusters still to name, largest first: (not skipped, skipped)."""
        todo, later = [], []
        for c, segs in grouped.items():
            ids = {s["turn_id"] for s in segs}
            if len(segs) < MIN_TAG_CLUSTER or mixed & ids or cluster_name(ids, names)[0]:
                continue
            (later if skipped & ids else todo).append(c)
        return todo, later

    def append_tag(record: dict) -> None:
        record["tagged_at"] = datetime.now(timezone.utc).isoformat()
        with open(tags_path, "a") as f:
            f.write(json.dumps(record) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def login_page(error: str = "") -> HTMLResponse:
        message = f"<p class='error'>{html.escape(error)}</p>" if error else ""
        return page("Hearsay login", f"""
<h1>Hearsay</h1>{message}
<form method="post" action="/login">
  <label for="username">Username</label>
  <input id="username" name="username" autocomplete="username" autocapitalize="none" required>
  <label for="password">Password</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required>
  <button type="submit">Log in</button>
</form>""")

    @app.get("/login")
    async def login_form() -> HTMLResponse:
        return login_page()

    @app.post("/login")
    async def login(request: Request) -> Response:
        form = parse_qs((await request.body()).decode())
        given_user = form.get("username", [""])[0]
        given_password = form.get("password", [""])[0]
        user_ok = hmac.compare_digest(given_user.encode(), user.encode())
        password_ok = hmac.compare_digest(given_password.encode(), password.encode())
        if not (user_ok and password_ok):
            await asyncio.sleep(1)  # slows guessing
            return login_page("Wrong username or password.")
        expires = int(time.time()) + SESSION_SECONDS
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(COOKIE, f"{expires}.{sign(expires)}", max_age=SESSION_SECONDS,
                            httponly=True, samesite="strict")
        return response

    @app.post("/logout")
    async def logout() -> Response:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE)
        return response

    @app.get("/")
    async def index(request: Request) -> Response:
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        grouped = load_clusters()
        names, mixed, skipped = read_tags()
        todo, later = unnamed(grouped, names, mixed, skipped)
        people = {}
        conflicts = 0
        for c, segs in grouped.items():
            person, conflict = cluster_name([s["turn_id"] for s in segs], names)
            conflicts += conflict
            if person:
                people.setdefault(person, []).append((c, len(segs)))
        def items(clusters):
            return "".join(
                f"<li><a href='/cluster/{quote(c)}'>{len(grouped[c])} turns"
                f" <span class='muted'>· {len({s['conversation_id'] for s in grouped[c]})} conversation(s)</span></a></li>"
                for c in clusters
            )

        people_items = "".join(
            f"<li><a href='/cluster/{quote(clusters[0][0])}'>{html.escape(p)}</a>"
            f" <span class='muted'>{sum(n for _, n in clusters)} turns in {len(clusters)} cluster(s)</span></li>"
            for p, clusters in sorted(people.items())
        )
        return page("Hearsay speakers", f"""
<h1>Speakers</h1>
<p class="muted">{len(grouped)} clusters · {conflicts} with conflicting names</p>
<h2>To name ({len(todo)})</h2><ul>{items(todo) or "<li class='muted'>Nothing left to name.</li>"}</ul>
<h2>Skipped ({len(later)})</h2><ul>{items(later) or "<li class='muted'>None.</li>"}</ul>
<h2>People ({len(people)})</h2><ul>{people_items or "<li class='muted'>None yet.</li>"}</ul>
<form method="post" action="/logout"><button type="submit">Log out</button></form>""")

    @app.get("/cluster/{cluster_id}")
    async def show_cluster(request: Request, cluster_id: str) -> Response:
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        grouped = load_clusters()
        if cluster_id not in grouped:
            return RedirectResponse("/", status_code=303)
        names, _, _ = read_tags()
        turns = grouped[cluster_id]
        person, conflict = cluster_name([s["turn_id"] for s in turns], names)
        if person:
            status = f"Named <strong>{html.escape(person)}</strong>. Saving a new name renames this cluster."
        elif conflict:
            status = "Tagged with conflicting names. Saving a name settles it."
        else:
            status = "Unnamed."
        players = "".join(
            f"<audio controls preload='none' src='/audio/{quote(s['turn_id'])}'></audio>"
            f"<div class='muted'>{s['end'] - s['start']:.1f} s</div>"
            for s in samples(turns)
        )
        known = "".join(f"<option value='{html.escape(n)}'>" for n in sorted(set(names.values())))
        return page("Hearsay cluster", f"""
<p><a href="/">← Speakers</a></p>
<h1>{len(turns)} turns</h1>
<p class="muted">{len({s['conversation_id'] for s in turns})} conversation(s). {status}</p>
{players}
<form method="post" action="/cluster/{quote(cluster_id)}">
  <label for="name">Who is this? Reusing a name merges into that person.</label>
  <input id="name" name="name" list="known" autocomplete="off" autocapitalize="words">
  <datalist id="known">{known}</datalist>
  <button type="submit" name="action" value="name">Save name</button>
  <button type="submit" name="action" value="mixed">More than one person</button>
  <button type="submit" name="action" value="skip" formnovalidate>Skip for now</button>
</form>""")

    @app.post("/cluster/{cluster_id}")
    async def tag_cluster(request: Request, cluster_id: str) -> Response:
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        grouped = load_clusters()
        if cluster_id not in grouped:
            return RedirectResponse("/", status_code=303)
        form = parse_qs((await request.body()).decode())
        action = form.get("action", [""])[0]
        name = " ".join(form.get("name", [""])[0].split())
        names, _, _ = read_tags()
        turns = grouped[cluster_id]
        heard = samples(turns)

        def spans(chosen):
            return [{k: s[k] for k in ("turn_id", "conversation_id", "start", "end")} for s in chosen]

        if action == "mixed":
            append_tag({"type": "mixed", "turns": spans(heard)})
        elif action == "skip":
            append_tag({"type": "skip", "turns": spans(heard)})
        elif action == "name" and name:
            # Also retag this cluster's already-tagged turns, so a new name
            # renames the cluster or settles a conflict.
            chosen = heard + [s for s in turns if s["turn_id"] in names and s not in heard]
            append_tag({"type": "name", "turns": spans(chosen), "name": name})
        else:
            return RedirectResponse(f"/cluster/{quote(cluster_id)}", status_code=303)
        # Next: the first unskipped cluster; skipped ones only come round again
        # once everything else is done, and never straight back to this one.
        todo, later = unnamed(grouped, *read_tags())
        following = [c for c in todo + later if c != cluster_id]
        return RedirectResponse(f"/cluster/{quote(following[0])}" if following else "/", status_code=303)

    @app.get("/audio/{turn_id}")
    async def audio(request: Request, turn_id: str) -> Response:
        if not logged_in(request):
            return Response(status_code=401)
        db = connect()
        try:
            row = db.execute(
                "SELECT ca.wav_file, t.start, t.end FROM turns t"
                " JOIN conversation_audio ca ON ca.conversation_id = t.conversation_id"
                " WHERE t.turn_id = ?",
                (turn_id,),
            ).fetchone()
        finally:
            db.close()
        if row is None or row[0] is None:
            return Response(status_code=404)
        return ranged(wav_slice(audio_dir / row[0], row[1], row[2]), request.headers.get("range"))

    return app


def app_from_env() -> FastAPI:
    for key in ("HEARSAY_PORTAL_USER", "HEARSAY_PORTAL_PASSWORD", "HEARSAY_SESSION_KEY"):
        if not os.environ.get(key):
            raise RuntimeError(f"{key} is empty")
    return create_app(
        Path(os.environ["HEARSAY_DB"]),
        Path(os.environ["HEARSAY_AUDIO_DIR"]),
        Path(os.environ["HEARSAY_LABELS_DIR"]),
        os.environ["HEARSAY_PORTAL_USER"],
        os.environ["HEARSAY_PORTAL_PASSWORD"],
        os.environ["HEARSAY_SESSION_KEY"],
    )
