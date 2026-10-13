"""Web portal for naming anonymous speakers, from a phone or any browser.

Listens only on the LAN and tailnet addresses (install/compose.yaml), never
through the Cloudflare tunnel. Login is a plain HTML form, not an HTTP auth
dialog, so password managers can fill it; sessions are HMAC-signed cookies.
The iPhone app logs in instead with the capture token it already holds
(POST /login/app, bearer): the portal shows in its Voices tab, and whoever
holds that token can already send Hearsay audio. The app's own Places map
sends the token on each request instead: GET /places.json, and POST /places
with the same fields as the page's forms.

Names are written to labels/tags.jsonl, the same durable input reprocess
reads, with the time spans of the turns heard so they survive
re-transcription. The portal applies them to the current clusters as soon as
they are saved, using the same rules as reprocess (hearsay.turns.resolve and
hearsay.people.cluster_name).
"""

import asyncio
import bisect
import hashlib
import hmac
import html
import io
import json
import os
import re
import sqlite3
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from hearsay.media import HINT as MEDIA_HINT
from hearsay.people import cluster_name
from hearsay.places import (
    LOOKBACK,
    MAX_RADIUS,
    MIN_RADIUS,
    RADIUS,
    read_ignored,
    read_places,
    read_readings,
    unnamed_spots,
    visits,
)
from hearsay.turns import read_operator_files, resolve, wall

COOKIE = "hearsay_session"
SESSION_SECONDS = 30 * 24 * 3600
# Samples played per cluster.
SAMPLES = 3
# Smaller clusters (mostly single noisy turns) aren't offered for naming.
MIN_TAG_CLUSTER = 3
# People offered for one tap on a cluster's page: those heard most at its places.
SUGGESTIONS = 5
# A person's turns least like their voice, offered on their page to check.
MARGINS = 6
# Turns tagged differently from their cluster, shown on its page at a time.
STRAYS = 10
# Clips played per unnamed place, to tell by ear where it is.
PLACE_CLIPS = 3
# For the app's Places map: an unnamed spot's latest conversations, each with
# its first turns cut short, to tell by what was said where it is.
SPOT_CONVERSATIONS = 10
# And a named place's, to correct one that was put there wrongly.
PLACE_CONVERSATIONS = 20
OPENING_TURNS = 2
OPENING_CHARACTERS = 200

CLUSTERS_SQL = """
SELECT tp.turn_id, tp.cluster, tp.split, tp.fit, t.conversation_id, t.start, t.end, ca.wav_file, ca.zero_at
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
.row { display: flex; gap: 0.5rem; }
.row button { flex: 1; }
.clip .row button { font-size: 0.85em; }
.hinted { font-weight: 600; outline: 2px solid var(--accent); }
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


def voiced(turns: list[dict]) -> list[dict]:
    """The turns clustered by their own voice. Only these name a cluster, as in
    reprocess (hearsay/people.py): the rest inherited it from their diarized speaker."""
    return [s for s in turns if s["fit"] is not None]


def name_of(turns: list[dict], names: dict[str, str]) -> tuple[str | None, bool]:
    return cluster_name([s["turn_id"] for s in voiced(turns)], names)


# What isn't a person, one tap each (hearsay/stream.py leaves out all but _stranger).
CATEGORIES = {"_media": "Media", "_music": "Music", "_noise": "Noise", "_stranger": "Stranger"}


def category_buttons(hinted: str | None = None) -> str:
    return "<div class='row'>" + "".join(
        f"<button type='submit' name='category' value='{value}' formnovalidate"
        f"{' class=hinted' if value == hinted else ''}>{label}</button>"
        for value, label in CATEGORIES.items()) + "</div>"


def clip_player(s: dict, cluster_id: str, tagged: str | None = None, note: str = "", confirm: str | None = None,
                back: str = "") -> str:
    """A turn's audio, and a form to tag just that clip, take its tag back, or confirm it's `confirm`."""
    audio = (f"<audio controls preload='none' src='/audio/{quote(s['turn_id'])}'></audio>"
             f"<div class='muted'>{s['end'] - s['start']:.1f} s{' · ' + html.escape(note) if note else ''}</div>")
    hidden = (f"<input type='hidden' name='turn_id' value='{html.escape(s['turn_id'])}'>"
              + (f"<input type='hidden' name='back' value='{html.escape(back)}'>" if back else ""))
    form = f"<form class='clip' method='post' action='/cluster/{quote(cluster_id)}/clip'>{hidden}"
    if tagged:
        return (f"{audio}{form}<span class='muted'>Just this clip: <strong>{html.escape(tagged)}</strong></span>"
                "<button type='submit'>Untag clip</button></form>")
    yes = (f"<button type='submit' name='confirm' value='{html.escape(confirm)}' formnovalidate>"
           f"It's {html.escape(confirm)}</button>" if confirm else "")
    return (f"{audio}{form}<input name='name' list='known' autocomplete='off' autocapitalize='words'"
            " placeholder='Just this clip: who it is' required>"
            f"<button type='submit'>Tag clip</button>{yes}{category_buttons()}</form>")


def samples(turns: list[dict]) -> list[dict]:
    """The longest voiced turns, one per conversation first, so samples cover the cluster's spread."""
    by_length = sorted(voiced(turns), key=lambda s: (-(s["end"] - s["start"]), s["turn_id"]))
    picked, conversations = [], set()
    for s in by_length:
        if s["conversation_id"] not in conversations:
            picked.append(s)
            conversations.add(s["conversation_id"])
    picked += [s for s in by_length if s not in picked]
    return picked[:SAMPLES]


def heard_by_conversation(db: sqlite3.Connection) -> dict[str, set[str]]:
    """The named people heard in each conversation (categories aren't people)."""
    heard = {}
    for conversation_id, person in db.execute(
        "SELECT DISTINCT t.conversation_id, tp.person FROM turn_people tp JOIN turns t USING (turn_id)"
        " WHERE tp.person IS NOT NULL AND tp.person NOT LIKE '\\_%' ESCAPE '\\'"
    ):
        heard.setdefault(conversation_id, set()).add(person)
    return heard


def coordinates(text: str) -> tuple[float, float] | None:
    """Latitude and longitude from pasted text: "26.12, -80.14", or an Apple or
    Google Maps link that has them in it (ll=, q=, @). Not an address: looking
    one up would send it to a geocoding service."""
    found = re.search(r"(-?\d{1,2}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)", unquote(text))
    if not found:
        return None
    lat, lon = float(found[1]), float(found[2])
    return (lat, lon) if abs(lat) <= 90 and abs(lon) <= 180 else None


def heard_at_spot(spot_times: list[float], times: list[float], turns: list[tuple]) -> list[tuple]:
    """The longest turns recorded while the phone was at a spot, one per
    conversation first. A turn is where the latest reading before it was, as
    in hearsay/places.py. turns are (turn_id, conversation_id, wall start,
    duration); times are every reading's, in order."""
    at_spot = set(spot_times)
    found = []
    for turn in turns:
        i = bisect.bisect_right(times, turn[2]) - 1
        if i >= 0 and times[i] in at_spot and turn[2] - times[i] <= LOOKBACK:
            found.append(turn)
    found.sort(key=lambda t: (-t[3], t[0]))
    picked, conversations = [], set()
    for turn in found:
        if turn[1] not in conversations:
            picked.append(turn)
            conversations.add(turn[1])
    return (picked + [t for t in found if t not in picked])[:PLACE_CLIPS]


def create_app(db_path: Path, audio_dir: Path, labels_dir: Path, user: str, password: str, session_key: str,
               app_token: str = "") -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    tags_path = labels_dir / "tags.jsonl"

    def sign(expires: int) -> str:
        return hmac.new(session_key.encode(), f"{user}:{expires}".encode(), hashlib.sha256).hexdigest()

    def logged_in(request: Request) -> bool:
        # The iPhone app's own screens (GET /places.json) send the capture token on each request.
        sent = request.headers.get("authorization", "")
        if app_token and sent.startswith("Bearer "):
            return hmac.compare_digest(sent.removeprefix("Bearer ").encode(), app_token.encode())
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

    def heard_at_places(conversation_ids: set[str]) -> tuple[list[str], list[str]]:
        """The named places these conversations happened at, and the people heard
        in any conversation at those places, most heard first. A hint for the
        operator only: a place never names anyone on its own."""
        db = connect()
        try:
            ids = sorted(conversation_ids)
            places = [p for (p,) in db.execute(
                f"SELECT DISTINCT place FROM conversation_places WHERE conversation_id IN ({','.join('?' * len(ids))})"
                " ORDER BY place", ids)]
            if not places:
                return [], []
            people = [p for p, _ in db.execute(
                "SELECT tp.person, sum(t.end - t.start) FROM turn_people tp JOIN turns t ON t.turn_id = tp.turn_id"
                " WHERE t.conversation_id IN (SELECT conversation_id FROM conversation_places"
                f" WHERE place IN ({','.join('?' * len(places))}))"
                " AND tp.person IS NOT NULL AND tp.person NOT LIKE '\\_%' ESCAPE '\\'"
                " GROUP BY tp.person ORDER BY 2 DESC LIMIT ?", [*places, SUGGESTIONS])]
        finally:
            db.close()
        return places, people

    def hints(turns: list[dict], person: str | None) -> tuple[list[str], dict[str, list[str]]]:
        """What a cluster's page offers for one tap: the named people it sounds
        most like and those heard at its places (with the places), and _media
        when it sounds like tagged media. Voice and media for unnamed clusters only."""
        places, people = heard_at_places({s["conversation_id"] for s in turns})
        score = media_score([s["turn_id"] for s in turns]) if not person else None
        return places, {"voice": [] if person else closest_voices(turns[0]["cluster"]), "place": people,
                        "media": ["_media"] if score is not None and score >= MEDIA_HINT else []}

    def closest_voices(cluster_id: str) -> list[str]:
        """Named people this cluster sounds most like, though not enough to join (hearsay/people.py)."""
        db = connect()
        try:
            return [p for (p,) in db.execute(
                "SELECT person FROM cluster_voices WHERE cluster = ? ORDER BY similarity DESC", (cluster_id,))]
        except sqlite3.OperationalError:
            return []  # a database from before cluster_voices, until the next reprocess
        finally:
            db.close()

    def media_score(turn_ids: list[str]) -> float | None:
        """How much a cluster sounds like the media the operator has tagged (hearsay/media.py)."""
        db = connect()
        try:
            [(score,)] = db.execute(f"SELECT avg(score) FROM turn_media WHERE turn_id IN ({','.join('?' * len(turn_ids))})",
                                    turn_ids).fetchall()
        finally:
            db.close()
        return score

    def pending_mixed() -> set[str]:
        """Turns marked mixed since the last reprocess, which hasn't split them yet."""
        if not tags_path.exists():
            return set()
        built = datetime.fromtimestamp(db_path.stat().st_mtime, timezone.utc)
        return {turn_id for line in tags_path.read_text().splitlines()
                for record in [json.loads(line)]
                if record["type"] == "mixed" and datetime.fromisoformat(record["tagged_at"]) > built
                for turn_id in record.get("turn_ids", [])}

    def strays(turns: list[dict], names: dict[str, str], clips: dict[str, str]) -> list[dict]:
        """Turns tagged with a name their cluster doesn't have: the operator heard
        someone else there (a person, the TV), or the cluster's voices disagree.
        A clip tagged on its own has been dealt with: reprocess moves it out."""
        person, _ = name_of(turns, names)
        return [s for s in turns if names.get(s["turn_id"], person) != person and s["turn_id"] not in clips]

    def unnamed(grouped, names, clips, skipped) -> tuple[list[str], list[str], list[str]]:
        """Clusters to check (their tags disagree), and still to name, largest
        first: (to check, not skipped, skipped)."""
        check, todo, later = [], [], []
        pending = pending_mixed()
        for c, segs in grouped.items():
            ids = {s["turn_id"] for s in segs}
            # Marked mixed: off the list until reprocess splits its speakers,
            # then each voice comes back to be named. A turn reprocess can't
            # split (a lone turn) comes back too, rather than hiding its
            # cluster for good: its clips can be tagged on their own.
            if len(segs) < MIN_TAG_CLUSTER or pending & ids:
                continue
            if strays(segs, names, clips):
                check.append(c)
            elif not name_of(segs, names)[0]:
                (later if skipped & ids else todo).append(c)
        return check, todo, later

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
        names, _, skipped, clips = read_tags()
        check, todo, later = unnamed(grouped, names, clips, skipped)
        people = {}
        for c, segs in grouped.items():
            person, _ = name_of(segs, names)
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
<p class="muted">{len(grouped)} clusters · <a href="/places">Places</a></p>
{f"<h2>To check ({len(check)})</h2><p class='muted'>Tagged with more than one name.</p><ul>{items(check)}</ul>" if check else ""}
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
        person, conflict = name_of(turns, names)
        if person:
            status = f"Named <strong>{html.escape(person)}</strong>. Saving a new name renames this cluster."
        elif conflict:
            status = "Tagged with conflicting names. Saving a name settles it."
        else:
            status = "Unnamed."
        if conflict:
            tagged = sorted({names[s["turn_id"]] for s in voiced(turns) if s["turn_id"] in names})
            status = f"Tagged {html.escape(' and '.join(tagged))}. Listen below, then save the right name to settle it."

        # Clips tagged on their own aren't the cluster's: fresh samples take
        # their place, and they're listed below until reprocess moves them out.
        players = "".join(clip_player(s, cluster_id) for s in samples([s for s in turns if s["turn_id"] not in clips]))
        tagged_clips = "".join(clip_player(s, cluster_id, tagged=clips[s["turn_id"]]) for s in turns if s["turn_id"] in clips)
        if tagged_clips:
            tagged_clips = "<h2>Tagged on their own</h2>" + tagged_clips
        # Turns whose own tags disagree with the cluster's name: hear each, and
        # either confirm it's this person or tag the clip for what it is.
        odd = "".join(clip_player(s, cluster_id, note=f"Tagged {names[s['turn_id']]}", confirm=person)
                      for s in strays(turns, names, clips)[:STRAYS])
        if odd:
            odd = ("<h2>Tagged differently</h2><p class='muted'>Earlier tags here say someone else."
                   " Tag each clip for what it is.</p>" + odd)
        places, offered = hints(turns, person)
        suggested = offered["voice"] + [p for p in offered["place"] if p not in offered["voice"]]
        media = bool(offered["media"])
        hint = ""
        if offered["voice"]:
            hint += "<p class='muted'>Sounds most like:</p>" + "".join(
                f"<button type='submit' name='voice' value='{html.escape(p)}' formnovalidate>{html.escape(p)}</button>"
                for p in offered["voice"])
        if offered["place"]:
            hint += f"<p class='muted'>Heard at {html.escape(', '.join(places))}:</p>" + "".join(
                f"<button type='submit' name='suggested' value='{html.escape(p)}' formnovalidate>{html.escape(p)}</button>"
                for p in offered["place"])
        others = sorted(n for n in set(names.values()) | set(clips.values()) if not n.startswith("_"))
        known = "".join(f"<option value='{html.escape(n)}'>" for n in suggested + [n for n in others if n not in suggested])
        forget = ("<button type='submit' name='action' value='forget' formnovalidate>Forget this name</button>"
                  if person or conflict else "")
        return page("Hearsay cluster", f"""
<p><a href="/">← Speakers</a></p>
<h1>{len(turns)} turns</h1>
<p class="muted">{len({s['conversation_id'] for s in turns})} conversation(s). {status}
A clip that isn't this speaker (a cough, a video, someone else) can be tagged on its own.</p>
{players}
{odd}
<form method="post" action="/cluster/{quote(cluster_id)}">
  {hint}
  <label for="name">Who is this? Reusing a name merges into that person.</label>
  <input id="name" name="name" list="known" autocomplete="off" autocapitalize="words">
  <datalist id="known">{known}</datalist>
  <button type="submit" name="action" value="name">Save name</button>
  <p class="muted">{"Not a person? It sounds like the TV or video you've tagged before." if media else "Not a person?"}</p>
  {category_buttons("_media" if media else None)}
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
        name = form.get("category", [""])[0] or " ".join(form.get("name", [""])[0].split())
        confirm = " ".join(form.get("confirm", [""])[0].split())
        turn = next((s for s in grouped[cluster_id] if s["turn_id"] == turn_id), None)
        span = {"at": [wall(turn["zero_at"], turn["start"], turn["end"])], "turn_ids": [turn_id]} if turn else None
        if span and confirm:
            # It is the cluster's person after all: a name on this one turn,
            # which keeps it with them however it sounds.
            append_tag({"type": "name", **span, "name": confirm})
        elif span:
            # No name takes the clip's tag back.
            append_tag({"type": "clip", **span, "name": name or None})
        back = form.get("back", [""])[0]
        if not (back.startswith("/person/") or back.startswith("/cluster/")):
            back = f"/cluster/{quote(cluster_id)}"
        return RedirectResponse(back, status_code=303)

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
        # A name offered by a hint (people heard at this cluster's places, or
        # _media when it sounds like tagged media): marked with which hint, so
        # how often each is taken can be counted.
        suggested = " ".join(form.get("suggested", [""])[0].split())
        voice = " ".join(form.get("voice", [""])[0].split())
        category = form.get("category", [""])[0]
        if voice:
            action, name = "name", voice
        elif suggested:
            action, name = "name", suggested
        elif category in CATEGORIES:
            action, name = "name", category
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
            # What the page offered, so hearsay/hints.py can tell a hint
            # passed over from one never offered.
            _, offered = hints(turns, name_of(turns, names)[0])
            # The Media button stands out when the media hint offers it: taking it then is taking the hint.
            via = "voice" if voice else "place" if suggested else "media" if category == "_media" and offered["media"] else None
            append_tag({"type": "name", **spans(chosen), "name": name if action == "name" else None, "offered": offered}
                       | ({"via": via} if via else {}))
        else:
            return RedirectResponse(f"/cluster/{quote(cluster_id)}", status_code=303)
        # Next: the first unskipped cluster; skipped ones only come round again
        # once everything else is done, and never straight back to this one.
        names, _, skipped, clips = read_tags()
        check, todo, later = unnamed(grouped, names, clips, skipped)
        following = [c for c in check + todo + later if c != cluster_id]
        return RedirectResponse(f"/cluster/{quote(following[0])}" if following else "/", status_code=303)

    def person_clusters(grouped, names, person: str) -> list[str]:
        return [c for c, turns in grouped.items() if name_of(turns, names)[0] == person]

    @app.get("/person/{person}")
    async def show_person(request: Request, person: str) -> Response:
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        grouped = load_clusters()
        names, _, _, clips = read_tags()
        clusters = person_clusters(grouped, names, person)
        if not clusters:
            return RedirectResponse("/", status_code=303)
        # The margins: the voiced turns least like the rest of their cluster,
        # not yet tagged one by one. Confirming one names that turn, so it
        # leaves this list; anything else is tagged as a clip, and leaves the
        # person at the next reprocess.
        back = f"/person/{quote(person, safe='')}"
        unchecked = [s for c in clusters for s in voiced(grouped[c])
                     if s["turn_id"] not in names and s["turn_id"] not in clips]
        margins = "".join(clip_player(s, s["cluster"], note=f"fit {s['fit']:.2f}", confirm=person, back=back)
                          for s in sorted(unchecked, key=lambda s: (s["fit"], s["turn_id"]))[:MARGINS])
        if margins:
            margins = (f"<h2>Least like {html.escape(person)}</h2><p class='muted'>The turns that sound least like"
                       f" the rest. {len(unchecked)} not checked yet.</p>" + margins)
        others = sorted(set(names.values()) | set(clips.values()) | {"_noise", "_media", "_stranger"})
        items = "".join(
            f"<li><a href='/cluster/{quote(c)}'>{len(grouped[c])} turns"
            f" <span class='muted'>· {len({s['conversation_id'] for s in grouped[c]})} conversation(s)</span></a></li>"
            for c in clusters
        )
        known = "".join(f"<option value='{html.escape(n)}'>" for n in others if n != person)
        return page("Hearsay person", f"""
<p><a href="/">← Speakers</a></p>
<h1>{html.escape(person)}</h1>
<p class="muted">{sum(len(grouped[c]) for c in clusters)} turns in {len(clusters)} cluster(s).
Open a cluster to hear it, rename just that cluster, or forget its name.</p>
<ul>{items}</ul>
{margins}
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
        ignored = read_ignored(labels_dir)
        db = connect()
        try:
            readings = read_readings(db, labels_dir, spots)
            conversations = db.execute("SELECT conversation_id, start, end FROM conversations").fetchall()
            turns = [(turn_id, cid, datetime.fromisoformat(zero_at).timestamp() + start, end - start)
                     for turn_id, cid, zero_at, start, end in db.execute(
                         "SELECT t.turn_id, t.conversation_id, ca.zero_at, t.start, t.end FROM turns t"
                         " JOIN conversation_audio ca USING (conversation_id) WHERE ca.wav_file IS NOT NULL")]
            heard = heard_by_conversation(db)
            visited = dict(db.execute("SELECT place, count(DISTINCT conversation_id) FROM conversation_places GROUP BY place"))
        finally:
            db.close()

        def maps(lat: float, lon: float, label: str) -> str:
            return f"<a href='https://maps.apple.com/?ll={lat:.5f},{lon:.5f}&q={quote(label)}'>Open in Maps</a>"

        named = ""
        for name in sorted({sp[0] for sp in spots}):
            _, lat, lon, radius = next(sp for sp in spots if sp[0] == name)
            named += (f"<li><strong>{html.escape(name)}</strong> <span class='muted'>"
                      f"{visited.get(name, 0)} conversation(s) as of the last reprocess"
                      f"{f' · within {radius:.0f} m' if radius != RADIUS else ''} · {maps(lat, lon, name)}</span>"
                      f"<form class='clip' method='post' action='/places'><input type='hidden' name='from' value='{html.escape(name)}'>"
                      "<input name='name' placeholder='New name (an existing one merges them)' autocomplete='off'>"
                      "<button type='submit' name='action' value='rename'>Rename</button>"
                      "<button type='submit' name='action' value='forget' formnovalidate>Forget</button></form>"
                      f"<form class='clip' method='post' action='/places'><input type='hidden' name='from' value='{html.escape(name)}'>"
                      "<input name='at' placeholder='Coordinates or a Maps link' autocomplete='off' required>"
                      "<button type='submit' name='action' value='move'>Move here</button></form></li>")
        reading_times = [at for at, _, _, _ in readings]
        unnamed = ""
        for spot in unnamed_spots([r[:3] for r in readings if r[3] is None], spots, ignored):
            times = spot["times"]
            days = sorted({datetime.fromtimestamp(at, timezone.utc).strftime("%b %-d") for at in times})
            people = sorted({p for cid, start, end in conversations if any(start <= at <= end for at in times)
                             for p in heard.get(cid, ())})
            lat, lon = spot["latitude"], spot["longitude"]
            clips = "".join(
                f"<audio controls preload='none' src='/audio/{quote(turn_id)}'></audio><div class='muted'>"
                f"{datetime.fromtimestamp(at, timezone.utc):%b %-d %H:%M} UTC · {seconds:.0f} s</div>"
                for turn_id, _, at, seconds in heard_at_spot(times, reading_times, turns))
            unnamed += (f"<li>About {len(times)} min on {', '.join(days)} (UTC)"
                        f"<div class='muted'>{('Heard: ' + html.escape(', '.join(people))) if people else 'Nobody named heard here.'}"
                        f" · {maps(lat, lon, 'Unnamed place')}</div>{clips}"
                        f"<form class='clip' method='post' action='/places'>"
                        f"<input type='hidden' name='latitude' value='{lat:.6f}'><input type='hidden' name='longitude' value='{lon:.6f}'>"
                        "<input name='name' placeholder='Name this place (an existing name adds to it)' autocomplete='off' required>"
                        "<button type='submit' name='action' value='name'>Save place</button>"
                        "<button type='submit' name='action' value='ignore' formnovalidate>Not a place</button></form></li>")
        not_places = "".join(
            f"<li>{maps(lat, lon, 'Not a place')}<form class='clip' method='post' action='/places'>"
            f"<input type='hidden' name='latitude' value='{lat:.6f}'><input type='hidden' name='longitude' value='{lon:.6f}'>"
            "<button type='submit' name='action' value='unignore'>Offer again</button></form></li>"
            for lat, lon in ignored)
        return page("Hearsay places", f"""
<p><a href="/">← Speakers</a></p>
<h1>Places</h1>
<p class="muted">Where conversations happened, from the phone's location while it records. Only names you give
reach the stream, never coordinates; a reading counts as a place within 100 m of it, or the area set
in the app's Places map. To put a place
exactly where it is, find it in Maps, drop a pin, copy its coordinates, and paste them under Move here.</p>
<h2>To name</h2><ul>{unnamed or "<li class='muted'>Nowhere new.</li>"}</ul>
<h2>Named</h2><ul>{named or "<li class='muted'>None yet.</li>"}</ul>
{f"<h2>Not places ({len(ignored)})</h2><ul>{not_places}</ul>" if ignored else ""}""")

    @app.get("/places.json")
    async def places_json(request: Request) -> Response:
        """For the iPhone app's map: every named spot, and the spots to name."""
        if not logged_in(request):
            return Response(status_code=401)
        spots = read_places(labels_dir)
        db = connect()
        try:
            readings = read_readings(db, labels_dir, spots)
            conversations = db.execute("SELECT conversation_id, start, end FROM conversations ORDER BY start DESC").fetchall()
            heard = heard_by_conversation(db)
            opening = {}
            for conversation_id, text in db.execute(
                "SELECT conversation_id, text FROM turns WHERE idx < ? ORDER BY conversation_id, idx", (OPENING_TURNS,)
            ):
                opening[conversation_id] = f"{opening.get(conversation_id, '')} {text}".strip()
        finally:
            db.close()

        def entry(cid: str, start: float, end: float, at: list[float]) -> dict:
            # When, who and how it began, to tell from what was said where it was;
            # "readings" is the span of the readings that put it there, to correct.
            return {"start": start, "end": end, "people": sorted(heard.get(cid, ())),
                    "opening": opening.get(cid, "")[:OPENING_CHARACTERS], "readings": [min(at), max(at)]}

        def at_spot(times: list[float]) -> list[dict]:
            found = []
            for cid, start, end in conversations:
                at = [t for t in times if start <= t <= end]
                if at:
                    found.append(entry(cid, start, end, at))
            return found[:SPOT_CONVERSATIONS]

        # Each named place's conversations, latest first, as reprocess will
        # find them with the corrections so far.
        times, names = [r[0] for r in readings], [r[3] for r in readings]
        visited = {}
        for cid, start, end in conversations:
            for place in {place for place, _, _ in visits(times, names, start, end)}:
                at = [t for t, name in zip(times, names) if name == place and start - LOOKBACK <= t <= end]
                if at and len(visited.setdefault(place, [])) < PLACE_CONVERSATIONS:
                    visited[place].append(entry(cid, start, end, at))

        unnamed = unnamed_spots([r[:3] for r in readings if r[3] is None], spots, read_ignored(labels_dir))
        return JSONResponse({
            "named": [{"name": name, "latitude": lat, "longitude": lon, "radius": radius}
                      for name, lat, lon, radius in spots],
            "visits": visited,
            "unnamed": [{"latitude": s["latitude"], "longitude": s["longitude"], "minutes": len(s["times"]),
                         "conversations": at_spot(s["times"])} for s in unnamed]})

    @app.post("/places")
    async def tag_place(request: Request) -> Response:
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        form = parse_qs((await request.body()).decode())

        def field(key: str) -> str:
            return " ".join(form.get(key, [""])[0].split())

        action, name = field("action"), field("name")
        places_path = labels_dir / "places.jsonl"
        def radius(text: str, default: float = RADIUS) -> float:
            try:
                return min(max(float(text), MIN_RADIUS), MAX_RADIUS)
            except ValueError:
                return default

        def place(name: str, lat: float, lon: float, r: float) -> dict:
            # Only a radius the operator set is written, so the default can still change.
            return {"type": "place", "name": name, "latitude": lat, "longitude": lon,
                    **({"radius": r} if r != RADIUS else {})}

        if action == "name" and name:
            append_tag(place(name, float(field("latitude")), float(field("longitude")), radius(field("radius"))),
                       places_path)
        elif action == "rename" and name and field("from") and name != field("from"):
            append_tag({"type": "rename", "from": field("from"), "to": name}, places_path)
        elif action in ("ignore", "unignore"):
            # The coordinates as the page showed them, so "Offer again" matches exactly.
            append_tag({"type": action, "latitude": float(field("latitude")),
                        "longitude": float(field("longitude"))}, places_path)
        elif action == "forget" and field("from"):
            append_tag({"type": "forget", "name": field("from")}, places_path)
        elif action in ("drop", "at") and field("start") and field("end") and (action == "drop" or name):
            # A span of readings that was wrong, or was really at a named place (hearsay/places.py).
            append_tag({"type": action, "start": float(field("start")), "end": float(field("end"))}
                       | ({"name": name} if action == "at" else {}), places_path)
        elif action == "move" and field("from"):
            # Its spots, where the phone's readings put them, give way to the points given:
            # one pasted here, or from the app's map each of the place's spots, one dragged
            # or resized. Each at may have a radius, in order; one without keeps the place's.
            points = [coordinates(at) for at in form.get("at", [])]
            current = max((sp[3] for sp in read_places(labels_dir) if sp[0] == field("from")), default=RADIUS)
            radii = form.get("radius", [])
            if points and None not in points:
                append_tag({"type": "forget", "name": field("from")}, places_path)
                for i, (lat, lon) in enumerate(points):
                    r = radius(radii[i], current) if i < len(radii) else current
                    append_tag(place(field("from"), lat, lon, r), places_path)
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
