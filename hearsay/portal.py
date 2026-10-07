"""Web portal for naming anonymous speakers, from a phone or any browser.

Listens only on the LAN and tailnet addresses (install/compose.yaml), never
through the Cloudflare tunnel. Login is a plain HTML form, not an HTTP auth
dialog, so password managers can fill it; sessions are HMAC-signed cookies.
The iPhone app logs in instead with the capture token it already holds
(POST /login/app, bearer): the portal shows in its Voices tab, and whoever
holds that token can already send Hearsay audio.

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
from hearsay.places import read_places, unnamed_spots
from hearsay.turns import read_operator_files, resolve, wall

COOKIE = "hearsay_session"
SESSION_SECONDS = 30 * 24 * 3600
# Samples played per cluster.
SAMPLES = 3
# Smaller clusters (mostly single noisy turns) aren't offered for naming.
MIN_TAG_CLUSTER = 3

CLUSTERS_SQL = """
SELECT tp.turn_id, tp.cluster, tp.split, t.conversation_id, t.start, t.end, ca.wav_file, ca.zero_at
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
.clip { margin-bottom: 1.2rem; }
.clip input { padding: 0.4rem; }
.clip button { padding: 0.4rem; margin-top: 0.3rem; }
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


def create_app(db_path: Path, audio_dir: Path, labels_dir: Path, user: str, password: str, session_key: str,
               app_token: str = "") -> FastAPI:
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

    def read_tags() -> tuple[dict[str, str], set[str], set[str], dict[str, str]]:
        db = connect()
        try:
            found = resolve(db, *read_operator_files(labels_dir))
        finally:
            db.close()
        return found.names, found.mixed, found.skipped, found.clips

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
            # Marked mixed: off the list until reprocess splits its speakers,
            # then each voice comes back to be named.
            unsplit = {s["turn_id"] for s in segs if not s["split"]}
            if len(segs) < MIN_TAG_CLUSTER or mixed & unsplit or cluster_name(ids, names)[0]:
                continue
            (later if skipped & ids else todo).append(c)
        return todo, later

    def append_tag(record: dict, path: Path = tags_path) -> None:
        record["tagged_at"] = datetime.now(timezone.utc).isoformat()
        with open(path, "a") as f:
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
        return session()

    @app.post("/login/app")
    async def app_login(request: Request) -> Response:
        sent = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not app_token or not hmac.compare_digest(sent.encode(), app_token.encode()):
            await asyncio.sleep(1)
            return Response(status_code=401)
        return session()

    def session() -> Response:
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
        names, mixed, skipped, _ = read_tags()
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
            f"<li><a href='/person/{quote(p, safe='')}'>{html.escape(p)}</a>"
            f" <span class='muted'>{sum(n for _, n in clusters)} turns in {len(clusters)} cluster(s)</span></li>"
            for p, clusters in sorted(people.items())
        )
        return page("Hearsay speakers", f"""
<h1>Speakers</h1>
<p class="muted">{len(grouped)} clusters · {conflicts} with conflicting names · <a href="/places">Places</a></p>
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
        names, _, _, clips = read_tags()
        turns = grouped[cluster_id]
        person, conflict = cluster_name([s["turn_id"] for s in turns], names)
        if person:
            status = f"Named <strong>{html.escape(person)}</strong>. Saving a new name renames this cluster."
        elif conflict:
            status = "Tagged with conflicting names. Saving a name settles it."
        else:
            status = "Unnamed."
        action = f"/cluster/{quote(cluster_id)}/clip"

        def player(s: dict, tagged: str | None) -> str:
            audio = (f"<audio controls preload='none' src='/audio/{quote(s['turn_id'])}'></audio>"
                     f"<div class='muted'>{s['end'] - s['start']:.1f} s</div>")
            hidden = f"<input type='hidden' name='turn_id' value='{html.escape(s['turn_id'])}'>"
            if tagged:
                return (f"{audio}<form class='clip' method='post' action='{action}'>{hidden}"
                        f"<span class='muted'>Just this clip: <strong>{html.escape(tagged)}</strong></span>"
                        "<button type='submit'>Untag clip</button></form>")
            return (f"{audio}<form class='clip' method='post' action='{action}'>{hidden}"
                    "<input name='name' list='known' autocomplete='off' autocapitalize='words'"
                    " placeholder='Just this clip: _noise, _media, a name' required>"
                    "<button type='submit'>Tag clip</button></form>")

        # Clips tagged on their own aren't the cluster's: fresh samples take
        # their place, and they're listed below until reprocess moves them out.
        players = "".join(player(s, None) for s in samples([s for s in turns if s["turn_id"] not in clips]))
        tagged_clips = "".join(player(s, clips[s["turn_id"]]) for s in turns if s["turn_id"] in clips)
        if tagged_clips:
            tagged_clips = "<h2>Tagged on their own</h2>" + tagged_clips
        categories = {"_noise", "_media", "_stranger"}
        known = "".join(f"<option value='{html.escape(n)}'>"
                        for n in sorted(set(names.values()) | set(clips.values()) | categories))
        forget = ("<button type='submit' name='action' value='forget' formnovalidate>Forget this name</button>"
                  if person or conflict else "")
        return page("Hearsay cluster", f"""
<p><a href="/">← Speakers</a></p>
<h1>{len(turns)} turns</h1>
<p class="muted">{len({s['conversation_id'] for s in turns})} conversation(s). {status}
A clip that isn't this speaker (a cough, a video, someone else) can be tagged on its own.</p>
{players}
<form method="post" action="/cluster/{quote(cluster_id)}">
  <label for="name">Who is this? Reusing a name merges into that person.</label>
  <input id="name" name="name" list="known" autocomplete="off" autocapitalize="words">
  <datalist id="known">{known}</datalist>
  <button type="submit" name="action" value="name">Save name</button>
  <button type="submit" name="action" value="mixed">More than one person</button>
  <button type="submit" name="action" value="skip" formnovalidate>Skip for now</button>
  {forget}
</form>
{tagged_clips}""")

    @app.post("/cluster/{cluster_id}/clip")
    async def tag_clip(request: Request, cluster_id: str) -> Response:
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        grouped = load_clusters()
        if cluster_id not in grouped:
            return RedirectResponse("/", status_code=303)
        form = parse_qs((await request.body()).decode())
        turn_id = form.get("turn_id", [""])[0]
        name = " ".join(form.get("name", [""])[0].split())
        turn = next((s for s in grouped[cluster_id] if s["turn_id"] == turn_id), None)
        if turn is not None:
            # No name takes the clip's tag back.
            append_tag({"type": "clip", "at": [wall(turn["zero_at"], turn["start"], turn["end"])],
                        "turn_ids": [turn_id], "name": name or None})
        return RedirectResponse(f"/cluster/{quote(cluster_id)}", status_code=303)

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
        names, _, _, clips = read_tags()
        turns = grouped[cluster_id]
        heard = samples([s for s in turns if s["turn_id"] not in clips])

        def spans(chosen) -> dict:
            # Absolute times, so the tag survives new transcripts and boundaries;
            # turn ids only for reference.
            return {"at": [wall(s["zero_at"], s["start"], s["end"]) for s in chosen],
                    "turn_ids": [s["turn_id"] for s in chosen]}

        if action == "mixed":
            append_tag({"type": "mixed", **spans(heard)})
        elif action == "skip":
            append_tag({"type": "skip", **spans(heard)})
        elif action in ("name", "forget") and (name or action == "forget"):
            # Also retag this cluster's already-tagged turns, so a new name
            # renames the cluster or settles a conflict, and forgetting
            # (a name record with no name) clears every name it carries.
            chosen = heard + [s for s in turns if s["turn_id"] in names and s not in heard]
            if name.startswith("_"):
                # A category takes only tagged turns (hearsay/people.py), so
                # it is put on the whole cluster as it is now.
                chosen = turns
            append_tag({"type": "name", **spans(chosen), "name": name if action == "name" else None})
        else:
            return RedirectResponse(f"/cluster/{quote(cluster_id)}", status_code=303)
        # Next: the first unskipped cluster; skipped ones only come round again
        # once everything else is done, and never straight back to this one.
        names, mixed, skipped, _ = read_tags()
        todo, later = unnamed(grouped, names, mixed, skipped)
        following = [c for c in todo + later if c != cluster_id]
        return RedirectResponse(f"/cluster/{quote(following[0])}" if following else "/", status_code=303)

    def person_clusters(grouped, names, person: str) -> list[str]:
        return [c for c, turns in grouped.items() if cluster_name([s["turn_id"] for s in turns], names)[0] == person]

    @app.get("/person/{person}")
    async def show_person(request: Request, person: str) -> Response:
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        grouped = load_clusters()
        names, _, _, _ = read_tags()
        clusters = person_clusters(grouped, names, person)
        if not clusters:
            return RedirectResponse("/", status_code=303)
        items = "".join(
            f"<li><a href='/cluster/{quote(c)}'>{len(grouped[c])} turns"
            f" <span class='muted'>· {len({s['conversation_id'] for s in grouped[c]})} conversation(s)</span></a></li>"
            for c in clusters
        )
        known = "".join(f"<option value='{html.escape(n)}'>" for n in sorted(set(names.values())) if n != person)
        return page("Hearsay person", f"""
<p><a href="/">← Speakers</a></p>
<h1>{html.escape(person)}</h1>
<p class="muted">{sum(len(grouped[c]) for c in clusters)} turns in {len(clusters)} cluster(s).
Open a cluster to hear it, rename just that cluster, or forget its name.</p>
<ul>{items}</ul>
<form method="post" action="/person/{quote(person, safe='')}">
  <label for="name">Rename everywhere. An existing name merges the two.</label>
  <input id="name" name="name" list="known" autocomplete="off" autocapitalize="none" required>
  <datalist id="known">{known}</datalist>
  <button type="submit">Rename</button>
</form>""")

    @app.post("/person/{person}")
    async def rename_person(request: Request, person: str) -> Response:
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        form = parse_qs((await request.body()).decode())
        new_name = " ".join(form.get("name", [""])[0].split())
        names, _, _, clips = read_tags()
        if not new_name or new_name == person or person not in {*names.values(), *clips.values()}:
            return RedirectResponse(f"/person/{quote(person, safe='')}", status_code=303)
        append_tag({"type": "rename", "from": person, "to": new_name})
        return RedirectResponse(f"/person/{quote(new_name, safe='')}", status_code=303)

    @app.get("/places")
    async def show_places(request: Request) -> Response:
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        spots = read_places(labels_dir)
        db = connect()
        try:
            readings = db.execute("SELECT at, latitude, longitude FROM locations ORDER BY at").fetchall()
            conversations = db.execute("SELECT conversation_id, start, end FROM conversations").fetchall()
            heard = {}
            for conversation_id, person in db.execute(
                "SELECT DISTINCT t.conversation_id, tp.person FROM turn_people tp JOIN turns t USING (turn_id)"
                " WHERE tp.person IS NOT NULL AND tp.person NOT LIKE '\\_%' ESCAPE '\\'"
            ):
                heard.setdefault(conversation_id, set()).add(person)
            visited = dict(db.execute("SELECT place, count(DISTINCT conversation_id) FROM conversation_places GROUP BY place"))
        finally:
            db.close()

        def maps(lat: float, lon: float, label: str) -> str:
            return f"<a href='https://maps.apple.com/?ll={lat:.5f},{lon:.5f}&q={quote(label)}'>Open in Maps</a>"

        named = ""
        for name in sorted({n for n, _, _ in spots}):
            _, lat, lon = next(sp for sp in spots if sp[0] == name)
            named += (f"<li><strong>{html.escape(name)}</strong> <span class='muted'>"
                      f"{visited.get(name, 0)} conversation(s) as of the last reprocess · {maps(lat, lon, name)}</span>"
                      f"<form class='clip' method='post' action='/places'><input type='hidden' name='from' value='{html.escape(name)}'>"
                      "<input name='name' placeholder='New name (an existing one merges them)' autocomplete='off'>"
                      "<button type='submit' name='action' value='rename'>Rename</button>"
                      "<button type='submit' name='action' value='forget' formnovalidate>Forget</button></form></li>")
        unnamed = ""
        for spot in unnamed_spots(readings, spots):
            times = spot["times"]
            days = sorted({datetime.fromtimestamp(at, timezone.utc).strftime("%b %-d") for at in times})
            people = sorted({p for cid, start, end in conversations if any(start <= at <= end for at in times)
                             for p in heard.get(cid, ())})
            lat, lon = spot["latitude"], spot["longitude"]
            unnamed += (f"<li>About {len(times)} min on {', '.join(days)} (UTC)"
                        f"<div class='muted'>{('Heard: ' + html.escape(', '.join(people))) if people else 'Nobody named heard here.'}"
                        f" · {maps(lat, lon, 'Unnamed place')}</div>"
                        f"<form class='clip' method='post' action='/places'>"
                        f"<input type='hidden' name='latitude' value='{lat:.6f}'><input type='hidden' name='longitude' value='{lon:.6f}'>"
                        "<input name='name' placeholder='Name this place (an existing name adds to it)' autocomplete='off' required>"
                        "<button type='submit' name='action' value='name'>Save place</button></form></li>")
        return page("Hearsay places", f"""
<p><a href="/">← Speakers</a></p>
<h1>Places</h1>
<p class="muted">Where conversations happened, from the phone's location while it records. Only names you give
reach the stream, never coordinates; a reading counts as a place within 100 m of it.</p>
<h2>To name</h2><ul>{unnamed or "<li class='muted'>Nowhere new.</li>"}</ul>
<h2>Named</h2><ul>{named or "<li class='muted'>None yet.</li>"}</ul>""")

    @app.post("/places")
    async def tag_place(request: Request) -> Response:
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        form = parse_qs((await request.body()).decode())

        def field(key: str) -> str:
            return " ".join(form.get(key, [""])[0].split())

        action, name = field("action"), field("name")
        places_path = labels_dir / "places.jsonl"
        if action == "name" and name:
            append_tag({"type": "place", "name": name, "latitude": float(field("latitude")),
                        "longitude": float(field("longitude"))}, places_path)
        elif action == "rename" and name and field("from") and name != field("from"):
            append_tag({"type": "rename", "from": field("from"), "to": name}, places_path)
        elif action == "forget" and field("from"):
            append_tag({"type": "forget", "name": field("from")}, places_path)
        return RedirectResponse("/places", status_code=303)

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
        # From capture.env (install/compose.yaml); without it, app login is off.
        os.environ.get("HEARSAY_CAPTURE_TOKEN", ""),
    )
