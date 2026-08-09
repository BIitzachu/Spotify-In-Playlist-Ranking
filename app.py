from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import algo
import spotify
import storage


HOST = "127.0.0.1"
PORT = 5000
REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", f"http://{HOST}:{PORT}/callback")
SPOTIFY_AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE_URL = "https://api.spotify.com/v1"
DEFAULT_SCOPES = (
    "playlist-read-private playlist-read-collaborative "
    "playlist-modify-public playlist-modify-private "
    "user-modify-playback-state user-read-playback-state"
)
DEFAULT_SUBSECTION_SIZE = 5


STATE: dict[str, Any] = {
    "client_id": "",
    "code_verifier": "",
    "oauth_state": "",
    "token": None,
    "selected_playlist": None,
    "message": "",
    "error": "",
    "subsection_size": DEFAULT_SUBSECTION_SIZE,
}


def set_flash(message: str = "", error: str = "") -> None:
    STATE["message"] = message
    STATE["error"] = error


def pkce_verifier() -> str:
    return secrets.token_urlsafe(64)


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("utf-8")


def auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def read_json(request: urllib.request.Request) -> dict[str, Any]:
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def request_json(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = None
    request_headers = headers.copy() if headers else {}

    if data is not None:
        payload = urllib.parse.urlencode(data).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

    request = urllib.request.Request(url, data=payload, headers=request_headers, method=method)
    try:
        return read_json(request)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(body or f"HTTP {exc.code}") from exc


def store_token(token_payload: dict[str, Any]) -> None:
    STATE["token"] = {
        "access_token": token_payload["access_token"],
        "refresh_token": token_payload.get("refresh_token"),
        "expires_at": time.time() + max(int(token_payload.get("expires_in", 3600)) - 60, 60),
        "scope": token_payload.get("scope", ""),
    }


def has_write_scope() -> bool:
    """Whether the current token was granted playlist-modify permissions."""
    token = STATE.get("token") or {}
    scope = token.get("scope", "") or ""
    return "playlist-modify-private" in scope or "playlist-modify-public" in scope


def has_playback_scope() -> bool:
    """Whether the current token was granted playback-control permissions."""
    token = STATE.get("token") or {}
    scope = token.get("scope", "") or ""
    return "user-modify-playback-state" in scope


def refresh_access_token() -> bool:
    token = STATE.get("token") or {}
    refresh_token = token.get("refresh_token")
    client_id = STATE.get("client_id")
    if not refresh_token or not client_id:
        return False

    try:
        payload = request_json(
            SPOTIFY_TOKEN_URL,
            method="POST",
            data={
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
    except RuntimeError:
        return False

    token["access_token"] = payload["access_token"]
    token["expires_at"] = time.time() + max(int(payload.get("expires_in", 3600)) - 60, 60)
    token["refresh_token"] = payload.get("refresh_token", refresh_token)
    STATE["token"] = token
    return True


def get_access_token() -> str | None:
    token = STATE.get("token")
    if not token:
        return None
    if time.time() >= token.get("expires_at", 0):
        if not refresh_access_token():
            return None
        token = STATE.get("token")
    return token.get("access_token")


def spotify_get(path: str) -> dict[str, Any] | None:
    access_token = get_access_token()
    if not access_token:
        return None

    url = f"{SPOTIFY_API_BASE_URL}{path}"
    request = urllib.request.Request(url, headers=auth_headers(access_token), method="GET")

    try:
        return read_json(request)
    except urllib.error.HTTPError as exc:
        if exc.code == 401 and refresh_access_token():
            refreshed = get_access_token()
            if not refreshed:
                return None
            retry = urllib.request.Request(url, headers=auth_headers(refreshed), method="GET")
            try:
                return read_json(retry)
            except Exception:
                return None
        return None
    except Exception:
        return None


class SpotifyApiError(Exception):
    """Raised by spotify_post so callers can distinguish e.g. missing-scope
    (403) errors from transient failures and show a useful message."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def spotify_post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    access_token = get_access_token()
    if not access_token:
        raise SpotifyApiError(401, "Not connected to Spotify.")

    url = f"{SPOTIFY_API_BASE_URL}{path}"
    payload = json.dumps(body).encode("utf-8")

    def _attempt(token: str) -> dict[str, Any]:
        headers = auth_headers(token)
        headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        return read_json(request)

    try:
        return _attempt(access_token)
    except urllib.error.HTTPError as exc:
        if exc.code == 401 and refresh_access_token():
            refreshed = get_access_token()
            if refreshed:
                try:
                    return _attempt(refreshed)
                except urllib.error.HTTPError as exc2:
                    body_text = exc2.read().decode("utf-8", errors="replace")
                    raise SpotifyApiError(exc2.code, body_text) from exc2
        body_text = exc.read().decode("utf-8", errors="replace")
        raise SpotifyApiError(exc.code, body_text) from exc


def spotify_put(path: str, body: dict[str, Any] | None = None) -> None:
    """
    PUT request to the Spotify API. Used for playback-control endpoints,
    which reply 204 No Content on success rather than a JSON body, so
    (unlike spotify_post) this doesn't try to parse a response.
    """
    access_token = get_access_token()
    if not access_token:
        raise SpotifyApiError(401, "Not connected to Spotify.")

    url = f"{SPOTIFY_API_BASE_URL}{path}"
    payload = json.dumps(body).encode("utf-8") if body is not None else None

    def _attempt(token: str) -> None:
        headers = auth_headers(token)
        headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=payload, headers=headers, method="PUT")
        with urllib.request.urlopen(request, timeout=20):
            return

    try:
        _attempt(access_token)
    except urllib.error.HTTPError as exc:
        if exc.code == 401 and refresh_access_token():
            refreshed = get_access_token()
            if refreshed:
                try:
                    _attempt(refreshed)
                    return
                except urllib.error.HTTPError as exc2:
                    body_text = exc2.read().decode("utf-8", errors="replace")
                    raise SpotifyApiError(exc2.code, body_text) from exc2
        body_text = exc.read().decode("utf-8", errors="replace")
        raise SpotifyApiError(exc.code, body_text) from exc


def play_track_in_context(track_uri: str, context_uri: str | None) -> tuple[bool, str]:
    """
    Start playback of `track_uri` on the user's active Spotify Connect
    device via the Web API (PUT /me/player/play), rather than a
    `spotify:track:` deep link.

    This is what lets it take over whatever's currently playing instead of
    requiring the user to pause first — a deep link just asks the already-
    running app to open a track, while this issues an explicit "start
    playback" command the same way the official app's own UI would.

    When `context_uri` (a playlist URI) is given, playback starts within
    that playlist's context at `track_uri`, so shuffle/repeat and "up next"
    behave the way they would if the user had pressed play from inside the
    playlist themselves. Without one (e.g. playing from the rankings
    gallery, which isn't tied to a playlist), it just queues the single
    track.

    Returns (ok, reason). On failure the caller should fall back to
    opening `track_uri` as a plain Spotify URI, exactly as before.
    """
    if not has_playback_scope():
        return False, "missing_scope"

    if context_uri:
        body: dict[str, Any] = {"context_uri": context_uri, "offset": {"uri": track_uri}}
    else:
        body = {"uris": [track_uri]}

    def _attempt(device_id: str | None) -> tuple[bool, str]:
        path = "/me/player/play"
        if device_id:
            path += f"?device_id={urllib.parse.quote(device_id)}"
        try:
            spotify_put(path, body)
            return True, "ok"
        except SpotifyApiError as exc:
            return False, str(exc.status)

    ok, reason = _attempt(None)
    if ok:
        return True, reason

    if reason == "404":
        # No currently-active device to target implicitly — look for any
        # available Connect device and target it explicitly instead.
        devices_payload = spotify_get("/me/player/devices") or {}
        device_id = next(
            (d.get("id") for d in devices_payload.get("devices", []) if d.get("id")), None
        )
        if device_id:
            ok, reason = _attempt(device_id)

    return ok, reason


def fetch_all_playlists() -> list[dict[str, Any]]:
    playlists: list[dict[str, Any]] = []
    path = "/me/playlists?limit=50"

    while path:
        payload = spotify_get(path)
        if not payload:
            break
        playlists.extend(payload.get("items", []))

        next_url = payload.get("next")
        if not next_url:
            break
        path = next_url.replace(SPOTIFY_API_BASE_URL, "")

    return playlists


def selected_playlist(playlists: list[dict[str, Any]]) -> dict[str, Any] | None:
    selected_id = (STATE.get("selected_playlist") or {}).get("id")
    if not selected_id:
        return None

    for playlist in playlists:
        if playlist.get("id") == selected_id:
            return playlist
    return STATE.get("selected_playlist")


def fetch_current_playlist_tracks() -> dict[str, dict[str, Any]]:
    """
    Tracks of the currently selected playlist, keyed by track ID. Freshly
    fetched from Spotify every call (so playlist edits show up), and any
    new song metadata is cached into local storage for reuse — e.g. so the
    rankings gallery can show titles without needing a live playlist to
    reference.
    """
    selected = STATE.get("selected_playlist") or {}
    playlist_id = selected.get("id")
    if not playlist_id:
        return {}

    tracks = spotify.get_playlist_as_ranking_dict(playlist_id, spotify_get)
    storage.upsert_songs(tracks)
    return tracks


def fetch_current_user_id() -> str | None:
    if STATE.get("user_id"):
        return STATE["user_id"]
    profile = spotify_get("/me")
    if not profile:
        return None
    STATE["user_id"] = profile.get("id")
    return STATE["user_id"]


def parse_form(body: bytes) -> dict[str, str]:
    parsed = urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items() if values}


def safe(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def playlist_card(playlist: dict[str, Any]) -> str:
    owner = playlist.get("owner", {}) or {}
    owner_name = owner.get("display_name") or owner.get("id") or "Unknown"
    tags = [f"<span class='pill'>{safe('Public' if playlist.get('public') else 'Private')}</span>"]
    if playlist.get("collaborative"):
        tags.append("<span class='pill'>Collaborative</span>")

    return f"""
      <div class="playlist">
        <div><strong>{safe(playlist.get('name'))}</strong></div>
        <div class="muted">{safe(playlist.get('items', {}).get('total', 0))} tracks · {safe(owner_name)}</div>
        <div class="tags">{''.join(tags)}</div>
      </div>
    """


PAGE_STYLE = """
    :root { color-scheme: dark; --bg: #0b1020; --panel: #12192d; --panel-2: #151f38; --text: #e6edf7; --muted: #8fa3c7; --accent: #25d366; --accent-dim: rgba(37, 211, 102, .16); }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: system-ui, Segoe UI, sans-serif; background: radial-gradient(circle at top, #1a2748, var(--bg) 52%); color: var(--text); min-height: 100vh; }
    .shell { max-width: 1040px; margin: 0 auto; padding: 20px 18px 40px; }
    .shell.wide { max-width: 1280px; }
    .navbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 6px 0 18px; flex-wrap: wrap; }
    .navbar .brand { font-weight: 800; letter-spacing: -.01em; color: var(--text); text-decoration: none; font-size: 1.05rem; }
    .navbar .brand span { color: var(--accent); }
    .navlinks { display: flex; gap: 8px; }
    .navlinks a { color: var(--muted); text-decoration: none; padding: 8px 14px; border-radius: 999px; font-size: .92rem; font-weight: 600; border: 1px solid transparent; }
    .navlinks a:hover { color: var(--text); border-color: rgba(143, 163, 199, .25); }
    .navlinks a.active { color: var(--accent); background: var(--accent-dim); border-color: rgba(37, 211, 102, .3); }
    .hero { margin-bottom: 20px; }
    .eyebrow { text-transform: uppercase; letter-spacing: .18em; color: var(--accent); font-size: .78rem; font-weight: 700; }
    h1 { margin: 10px 0 12px; font-size: clamp(1.8rem, 4.4vw, 3.2rem); line-height: 1.04; letter-spacing: -.01em; }
    h2 { letter-spacing: -.01em; }
    .lede, .muted { color: var(--muted); }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
    .panel { background: rgba(18, 25, 45, .88); border: 1px solid rgba(143, 163, 199, .18); border-radius: 22px; padding: 20px; }
    .panel h2 { margin: 0 0 8px; font-size: 1.1rem; }
    .stack { display: grid; gap: 12px; }
    label { display: grid; gap: 6px; }
    input, select, button, .button { font: inherit; }
    input, select { width: 100%; border-radius: 14px; border: 1px solid rgba(143, 163, 199, .22); background: var(--panel-2); color: var(--text); padding: 12px 14px; }
    button, .button { display: inline-flex; align-items: center; justify-content: center; border: 0; border-radius: 999px; padding: 12px 16px; background: rgba(255,255,255,.12); color: var(--text); text-decoration: none; cursor: pointer; gap: 8px; }
    button:hover, .button:hover { background: rgba(255,255,255,.18); }
    .primary { background: var(--accent); color: #052010; font-weight: 700; }
    .primary:hover { background: #2fe378; }
    .ghost { background: transparent; border: 1px solid rgba(143, 163, 199, .3); }
    .actions { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
    .alert { border-radius: 14px; padding: 12px 14px; margin-bottom: 14px; }
    .alert.ok { background: rgba(37, 211, 102, .12); border: 1px solid rgba(37, 211, 102, .25); }
    .alert.error { background: rgba(248, 113, 113, .12); border: 1px solid rgba(248, 113, 113, .25); }
    .playlist-list { display: grid; gap: 10px; max-height: 440px; overflow: auto; margin-top: 12px; }
    .playlist { background: rgba(255,255,255,.03); border: 1px solid rgba(143, 163, 199, .14); border-radius: 18px; padding: 14px; display: grid; gap: 6px; }
    .playlist strong { display: block; margin-bottom: 2px; }
    .tags { display: flex; gap: 8px; flex-wrap: wrap; }
    .pill { display: inline-flex; align-items: center; border-radius: 999px; padding: 4px 10px; font-size: .78rem; background: rgba(255,255,255,.08); }
    .summary-row { display: grid; gap: 4px; padding: 10px 0; border-top: 1px solid rgba(143, 163, 199, .12); }
    .summary-row:first-child { border-top: 0; padding-top: 0; }
    .full { grid-column: 1 / -1; }

    /* --- ranking cards --- */
    .rank-stage { display: flex; flex-wrap: wrap; gap: 18px; align-items: stretch; justify-content: center; padding: 10px 0 26px; }
    .rank-stage.compact .rank-card { max-width: 230px; }
    .rank-card { position: relative; flex: 1 1 200px; max-width: 260px; background: var(--panel); border: 1px solid rgba(143, 163, 199, .18); border-radius: 20px; padding: 18px 16px 16px; display: flex; flex-direction: column; gap: 10px; cursor: grab; transition: transform .16s ease, box-shadow .16s ease, border-color .16s ease; user-select: none; }
    .rank-card:hover { transform: translateY(-5px); box-shadow: 0 14px 30px rgba(0,0,0,.35); border-color: rgba(37, 211, 102, .4); }
    .rank-card.dragging { opacity: .35; cursor: grabbing; }
    .rank-card .ord { font-size: 2.2rem; font-weight: 800; color: var(--accent); line-height: 1; font-variant-numeric: tabular-nums; letter-spacing: -.02em; }
    .rank-card .titles { display: grid; gap: 2px; min-height: 52px; }
    .rank-card .title { font-weight: 700; font-size: .98rem; overflow: hidden; text-overflow: ellipsis; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
    .rank-card .artist { color: var(--muted); font-size: .85rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .rank-card .play-btn { align-self: flex-start; width: 38px; height: 38px; padding: 0; border-radius: 50%; background: var(--accent); color: #052010; display: flex; align-items: center; justify-content: center; text-decoration: none; font-size: .95rem; }
    .rank-card .play-btn:hover { background: #2fe378; }
    .rank-card .play-btn.loading { opacity: .6; cursor: wait; }
    .rank-card .grip { position: absolute; top: 14px; right: 16px; color: var(--muted); font-size: .75rem; letter-spacing: .1em; text-transform: uppercase; }
    .finished-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 14px; max-height: 62vh; overflow-y: auto; padding: 4px 4px 10px; }
    .finished-grid .rank-card { cursor: default; max-width: none; }
    .finished-grid .rank-card:hover { transform: none; box-shadow: none; border-color: rgba(143, 163, 199, .18); }

    /* --- subsections gallery --- */
    .subsection-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 14px; margin-top: 16px; }
    .subsection-square { display: block; background: var(--panel); border: 1px solid rgba(143, 163, 199, .18); border-radius: 18px; padding: 16px; text-decoration: none; color: var(--text); transition: transform .15s ease, border-color .15s ease; aspect-ratio: 1 / 1; overflow: hidden; }
    .subsection-square:hover { transform: translateY(-4px); border-color: rgba(37, 211, 102, .4); }
    .subsection-square .count { color: var(--accent); font-weight: 800; font-size: .78rem; text-transform: uppercase; letter-spacing: .08em; margin-bottom: 8px; display: block; }
    .subsection-square ol { margin: 0; padding: 0 0 0 1.1em; font-size: .82rem; color: var(--muted); display: grid; gap: 3px; }
    .subsection-square ol li { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .subsection-square .more { font-size: .76rem; color: var(--muted); margin-top: 6px; }
    .empty-state { border: 1px dashed rgba(143, 163, 199, .3); border-radius: 18px; padding: 34px 20px; text-align: center; color: var(--muted); }

    @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
"""


def nav_links(active: str) -> str:
    items = [("", "/", "Picker"), ("rank", "/rank", "Rank playlist"), ("subsections", "/subsections", "Rankings")]
    out = []
    for key, href, label in items:
        cls = " active" if key == active else ""
        out.append(f'<a class="navlinks-item{cls}" href="{href}">{safe(label)}</a>')
    return "".join(out)


def render_page(title: str, active: str, body: str, wide: bool = False) -> str:
    shell_cls = "shell wide" if wide else "shell"
    return f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe(title)}</title>
  <style>
{PAGE_STYLE}
  </style>
</head>
<body>
  <main class="{shell_cls}">
    <div class="navbar">
      <a class="brand" href="/">Local Spotify <span>Ranker</span></a>
      <nav class="navlinks">{nav_links(active)}</nav>
    </div>
    {body}
  </main>
</body>
</html>
    """


def render_homepage() -> str:
    playlists: list[dict[str, Any]] = []
    selected = None

    if STATE.get("token"):
        playlists = fetch_all_playlists()
        selected = selected_playlist(playlists)
        if selected and not STATE.get("selected_playlist"):
            STATE["selected_playlist"] = selected

    playlist_options = []
    playlist_list = []
    for playlist in playlists:
        playlist_id = playlist.get("id", "")
        selected_attr = " selected" if selected and playlist_id == selected.get("id") else ""
        playlist_options.append(
            f'<option value="{safe(playlist_id)}"{selected_attr}>{safe(playlist.get("name"))} ({safe(playlist.get("items", {}).get("total", 0))} tracks)</option>'
        )
        playlist_list.append(playlist_card(playlist))

    selected_block = "<p class='muted'>No playlist selected yet.</p>"
    if selected:
        rank_prompt = f"""
          <div class="actions" style="margin-top: 6px;">
            <a class="button primary" href="/rank">Start ranking this playlist →</a>
          </div>
        """
        selected_block = f"""
          <div class="summary-row"><span>Name</span><strong>{safe(selected.get('name'))}</strong></div>
          <div class="summary-row"><span>Playlist ID</span><strong>{safe(selected.get('id'))}</strong></div>
          <div class="summary-row"><span>Track count</span><strong>{safe(selected.get('items', {}).get('total', 0))}</strong></div>
          <div class="summary-row"><span>URI</span><strong>{safe(selected.get('uri'))}</strong></div>
          {rank_prompt}
        """

    message = f"<div class='alert ok'>{safe(STATE['message'])}</div>" if STATE.get("message") else ""
    error = f"<div class='alert error'>{safe(STATE['error'])}</div>" if STATE.get("error") else ""

    connect_section = ""
    if not STATE.get("client_id"):
        connect_section = f"""
          <form method="post" action="/configure" class="stack">
            <label>
              <span>Spotify Client ID</span>
              <input name="client_id" placeholder="Paste your Spotify app client ID" required>
            </label>
            <button type="submit">Save client ID</button>
          </form>
        """
    else:
        missing_perms = []
        if STATE.get("token") and not has_write_scope():
            missing_perms.append("importing a finished ranking as a new Spotify playlist")
        if STATE.get("token") and not has_playback_scope():
            missing_perms.append("playing songs directly from here")
        reconnect_hint = ""
        if missing_perms:
            reconnect_hint = f"""
              <p class="muted" style="margin-top: 4px;">
                Connected, but without permission for {safe(' and '.join(missing_perms))} yet.
                Reconnect once to grant it.
              </p>
            """
        connect_section = f"""
          <div class="summary-row"><span>Configured client ID</span><strong>{safe(STATE.get('client_id'))}</strong></div>
          <div class="actions">
            <a class="button primary" href="/login">Connect Spotify account</a>
            <form method="post" action="/reset"><button class="button ghost" type="submit">Reset local state</button></form>
          </div>
          {reconnect_hint}
        """

    choose_section = "<p class='muted'>Connect Spotify first to load your playlists.</p>"
    if playlists:
        choose_section = f"""
          <form method="post" action="/choose" class="stack">
            <label>
              <span>Available playlists</span>
              <select name="playlist_id" required>
                {''.join(playlist_options)}
              </select>
            </label>
            <button type="submit">Use selected playlist</button>
          </form>
          <div class="playlist-list">
            {''.join(playlist_list)}
          </div>
        """

    debug_note = ""
    if STATE.get("token"):
        debug_note = """
          <p class="muted" style="margin-top: 10px;">
            Debugging track counts? Visit <a href="/debug/playlists">/debug/playlists</a> to see the raw Spotify API response.
          </p>
        """

    body = f"""
    <section class="hero">
      <div class="eyebrow">Local Spotify picker</div>
      <h1>Connect your account and choose a playlist locally.</h1>
      <p class="lede">This stays on your machine. It opens Spotify login in your browser, loads your playlists, and keeps the selected playlist in local app state.</p>
    </section>
    {message}
    {error}
    <section class="grid">
      <div class="panel stack">
        <div>
          <h2>1. Connect Spotify</h2>
          <p class="muted">Use the redirect URI <code>{safe(REDIRECT_URI)}</code>. This app uses PKCE, so no client secret is needed.</p>
        </div>
        {connect_section}
      </div>
      <div class="panel stack">
        <div>
          <h2>2. Choose a playlist</h2>
          <p class="muted">After connecting, pick a playlist and keep it as the current local selection.</p>
        </div>
        {choose_section}
      </div>
      <div class="panel full stack">
        <div>
          <h2>Selected playlist</h2>
          <p class="muted">Ranking works on this playlist once you head to the ranking view.</p>
        </div>
        {selected_block}
      </div>
    </section>
    {debug_note}
    <p class="muted" style="margin-top: 14px;">Local server: <code>http://{HOST}:{PORT}</code></p>
    """
    return render_page("Local Spotify Picker", "", body)


# ---------------------------------------------------------------------------
# Ranking flow: draggable cards for the current subsection, a finished
# scrollable view once fully determined, and the cross-playlist rankings
# gallery. Everything here operates on Spotify track IDs, never playlist
# IDs, so a ranking made here is reusable by any other playlist.
# ---------------------------------------------------------------------------

DRAG_SCRIPT = """
<script>
(function () {
  const container = document.getElementById('card-stage');
  if (!container) return;
  let dragEl = null;

  function updateOrderInput() {
    const input = document.getElementById('order-input');
    if (!input) return;
    const ids = [...container.querySelectorAll('.rank-card')].map((c) => c.dataset.trackId);
    input.value = ids.join(',');
  }

  function renumber() {
    [...container.querySelectorAll('.rank-card')].forEach((card, i) => {
      const ord = card.querySelector('.ord');
      if (ord) ord.textContent = String(i + 1);
    });
  }

  container.querySelectorAll('.rank-card').forEach((card) => {
    card.setAttribute('draggable', 'true');
    card.addEventListener('dragstart', (e) => {
      dragEl = card;
      card.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', card.dataset.trackId || '');
    });
    card.addEventListener('dragend', () => {
      if (dragEl) dragEl.classList.remove('dragging');
      dragEl = null;
      renumber();
      updateOrderInput();
    });
    card.addEventListener('dragover', (e) => {
      e.preventDefault();
      if (!dragEl || dragEl === card) return;
      const cards = [...container.children];
      const dragIdx = cards.indexOf(dragEl);
      const overIdx = cards.indexOf(card);
      if (dragIdx < overIdx) {
        container.insertBefore(dragEl, card.nextSibling);
      } else {
        container.insertBefore(dragEl, card);
      }
    });
  });

  updateOrderInput();
})();
</script>
"""


PLAY_SCRIPT = """
<script>
function playTrack(btn) {
  const trackUri = btn.dataset.trackUri;
  const contextUri = btn.dataset.contextUri || '';
  if (!trackUri) return;

  function fallback() {
    window.location.href = trackUri;
  }

  btn.classList.add('loading');
  fetch('/play', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: 'track_uri=' + encodeURIComponent(trackUri) + '&context_uri=' + encodeURIComponent(contextUri),
  })
    .then((res) => res.json().catch(() => ({ ok: false })))
    .then((data) => {
      // Falls back to the plain spotify: URI (old behavior) whenever the
      // Web API call couldn't do it directly — e.g. no active device, the
      // connected account lacks the playback scope, or it's a free account.
      if (!data || !data.ok) fallback();
    })
    .catch(fallback)
    .finally(() => btn.classList.remove('loading'));
}
</script>
"""


def render_rank_card(
    position: int,
    track_id: str,
    meta: dict[str, Any],
    draggable: bool = True,
    context_uri: str | None = None,
) -> str:
    meta = meta or {}
    title = meta.get("title") or "(song no longer cached)"
    artist = meta.get("artist") or "Unknown artist"
    uri = meta.get("uri") or f"spotify:track:{track_id}"
    grip = '<span class="grip">drag</span>' if draggable else ""
    return f"""
      <div class="rank-card" data-track-id="{safe(track_id)}">
        {grip}
        <div class="ord">{position}</div>
        <div class="titles">
          <div class="title">{safe(title)}</div>
          <div class="artist">{safe(artist)}</div>
        </div>
        <button
          type="button"
          class="play-btn"
          title="Play in Spotify"
          data-track-uri="{safe(uri)}"
          data-context-uri="{safe(context_uri or '')}"
          onclick="event.stopPropagation(); playTrack(this)"
        >▶</button>
      </div>
    """


def render_finished_page(selected: dict[str, Any], order: list[str], tracks: dict[str, dict], message: str, error: str) -> str:
    context_uri = selected.get("uri")
    cards_html = "".join(
        render_rank_card(i + 1, tid, tracks.get(tid, {}), draggable=False, context_uri=context_uri)
        for i, tid in enumerate(order)
    )

    if has_write_scope():
        import_action = '<button class="button primary" type="submit">Import as new Spotify playlist</button>'
        import_note = ""
    else:
        import_action = '<a class="button primary" href="/login">Reconnect Spotify to enable import</a>'
        import_note = "<p class='muted'>Importing needs one extra permission your current connection doesn't have — reconnecting will ask for it.</p>"

    body = f"""
      <section class="hero">
        <div class="eyebrow">Fully ranked &middot; {safe(selected.get('name'))}</div>
        <h1>Every song has a place.</h1>
        <p class="lede">{len(order)} songs, fully ordered from known comparisons — some possibly inherited from other playlists you've ranked before.</p>
      </section>
      {message}
      {error}
      <div class="finished-grid">
        {cards_html}
      </div>
      <div class="actions" style="margin-top:18px;">
        <form method="post" action="/playlist/import">
          {import_action}
        </form>
      </div>
      {import_note}
      {PLAY_SCRIPT}
    """
    return render_page(f"{selected.get('name')} — finished", "rank", body)


def render_rank_page() -> str:
    selected = STATE.get("selected_playlist")
    message = f"<div class='alert ok'>{safe(STATE['message'])}</div>" if STATE.get("message") else ""
    error = f"<div class='alert error'>{safe(STATE['error'])}</div>" if STATE.get("error") else ""

    if not selected or not selected.get("id"):
        set_flash(error="Choose a playlist on the home page first.")
        body = f"""
          <section class="hero"><div class="eyebrow">Ranking</div><h1>No playlist selected</h1></section>
          {error}
          <div class="empty-state">Head to the <a href="/">picker</a> and choose a playlist first.</div>
        """
        return render_page("Rank a playlist", "rank", body)

    tracks = fetch_current_playlist_tracks()
    set_flash()

    if not tracks:
        body = f"""
          <section class="hero">
            <div class="eyebrow">Ranking</div>
            <h1>{safe(selected.get('name'))}</h1>
          </section>
          <div class="empty-state">Couldn't load any tracks for this playlist. Reconnect Spotify or pick another playlist.</div>
        """
        return render_page("Rank a playlist", "rank", body)

    subsections = [s["track_ids"] for s in storage.list_subsections()]
    order = algo.finalize_order(subsections, tracks)

    if order is not None:
        return render_finished_page(selected, order, tracks, message, error)

    size = STATE.get("subsection_size", DEFAULT_SUBSECTION_SIZE)
    subsection = algo.nextSubsectionToSort(subsections, tracks, size)

    context_uri = selected.get("uri")
    cards_html = "".join(
        render_rank_card(i + 1, tid, tracks.get(tid, {}), draggable=True, context_uri=context_uri)
        for i, tid in enumerate(subsection)
    )

    body = f"""
      <section class="hero">
        <div class="eyebrow">Ranking &middot; {safe(selected.get('name'))}</div>
        <h1>Drag these into your preferred order.</h1>
        <p class="lede">Comparing {len(subsection)} of {len(tracks)} songs right now. Your choices here apply to every playlist these songs are part of, not just this one.</p>
      </section>
      {message}
      {error}
      <form method="post" action="/rank/submit" id="rank-form">
        <input type="hidden" name="order" id="order-input" value="{','.join(subsection)}">
        <div class="rank-stage" id="card-stage">
          {cards_html}
        </div>
        <div class="actions" style="justify-content: flex-end;">
          <button class="button primary" type="submit">Next →</button>
        </div>
      </form>
      {DRAG_SCRIPT}
      {PLAY_SCRIPT}
    """
    return render_page(f"Ranking {selected.get('name')}", "rank", body)


def render_subsection_square(sub: dict[str, Any]) -> str:
    track_ids = sub["track_ids"]
    songs = storage.get_songs(track_ids)
    preview_count = 3
    items_html = []
    for tid in track_ids[:preview_count]:
        title = (songs.get(tid) or {}).get("title") or "Unknown"
        items_html.append(f"<li>{safe(title)}</li>")
    more = ""
    if len(track_ids) > preview_count:
        more = f'<div class="more">+{len(track_ids) - preview_count} more</div>'
    return f"""
      <a class="subsection-square" href="/subsections/{safe(sub['id'])}">
        <span class="count">{len(track_ids)} song{'s' if len(track_ids) != 1 else ''}</span>
        <ol>{''.join(items_html)}</ol>
        {more}
      </a>
    """


def render_subsections_page() -> str:
    subs = storage.list_subsections()
    message = f"<div class='alert ok'>{safe(STATE['message'])}</div>" if STATE.get("message") else ""
    error = f"<div class='alert error'>{safe(STATE['error'])}</div>" if STATE.get("error") else ""
    set_flash()

    intro = """
      <section class="hero">
        <div class="eyebrow">Rankings</div>
        <h1>Every ranking you've made, in one place.</h1>
        <p class="lede">These aren't tied to any playlist — they're built from songs, so they apply anywhere those songs show up.</p>
      </section>
    """

    if not subs:
        body = f"""
          {intro}
          {message}{error}
          <div class="empty-state">Nothing ranked yet. Start ranking a playlist and your first subsection will show up here.</div>
        """
        return render_page("Rankings", "subsections", body)

    squares = "".join(render_subsection_square(s) for s in reversed(subs))
    body = f"""
      {intro}
      {message}{error}
      <div class="subsection-grid">{squares}</div>
    """
    return render_page("Rankings", "subsections", body, wide=True)


def render_subsection_edit_page(sub: dict[str, Any]) -> str:
    track_ids = sub["track_ids"]
    songs = storage.get_songs(track_ids)
    cards_html = "".join(
        render_rank_card(i + 1, tid, songs.get(tid, {}), draggable=True)
        for i, tid in enumerate(track_ids)
    )
    source = sub.get("source_playlist_name")
    origin_note = f"<p class='lede'>Originally ranked while sorting \u201c{safe(source)}\u201d.</p>" if source else ""

    body = f"""
      <section class="hero">
        <div class="eyebrow">Editing a ranking</div>
        <h1>Adjust this order.</h1>
        {origin_note}
      </section>
      <form method="post" action="/subsections/{safe(sub['id'])}/save" id="rank-form">
        <input type="hidden" name="order" id="order-input" value="{','.join(track_ids)}">
        <div class="rank-stage" id="card-stage">
          {cards_html}
        </div>
        <div class="actions" style="justify-content: flex-end;">
          <a class="button ghost" href="/subsections">Cancel</a>
          <button class="button primary" type="submit">Save</button>
        </div>
      </form>
      {DRAG_SCRIPT}
      {PLAY_SCRIPT}
    """
    return render_page("Edit ranking", "subsections", body)


def import_current_playlist_as_new() -> None:
    selected = STATE.get("selected_playlist") or {}
    name = selected.get("name") or "Ranked playlist"

    tracks = fetch_current_playlist_tracks()
    if not tracks:
        set_flash(error="Couldn't load playlist tracks to import.")
        return

    subsections = [s["track_ids"] for s in storage.list_subsections()]
    order = algo.finalize_order(subsections, tracks)
    if order is None:
        set_flash(error="This playlist isn't fully ranked yet — finish ranking before importing.")
        return

    user_id = fetch_current_user_id()
    if not user_id:
        set_flash(error="Could not identify your Spotify account. Try reconnecting.")
        return

    try:
        created = spotify_post(
            f"/users/{user_id}/playlists",
            {
                "name": f"{name} (Ranked)",
                "public": False,
                "description": "Created by Local Spotify Ranker",
            },
        )
    except SpotifyApiError as exc:
        if exc.status in (401, 403):
            set_flash(error="Spotify rejected the request — reconnect your account to grant playlist creation permission.")
        else:
            set_flash(error=f"Could not create the playlist: {exc.message}")
        return

    new_playlist_id = created.get("id")
    uris = [tracks[tid]["uri"] for tid in order if tracks.get(tid, {}).get("uri")]

    try:
        for i in range(0, len(uris), 100):
            spotify_post(f"/playlists/{new_playlist_id}/tracks", {"uris": uris[i:i + 100]})
    except SpotifyApiError as exc:
        set_flash(error=f"Playlist created, but adding tracks failed partway through: {exc.message}")
        return

    link = (created.get("external_urls") or {}).get("spotify")
    if link:
        set_flash(message=f"Imported as a new playlist: {link}")
    else:
        set_flash(message="Imported as a new playlist.")


def redirect_url(client_id: str, oauth_state: str, verifier: str) -> str:
    params = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": DEFAULT_SCOPES,
            "code_challenge_method": "S256",
            "code_challenge": pkce_challenge(verifier),
            "state": oauth_state,
        }
    )
    return f"{SPOTIFY_AUTHORIZE_URL}?{params}"


def exchange_code(code: str) -> bool:
    try:
        payload = request_json(
            SPOTIFY_TOKEN_URL,
            method="POST",
            data={
                "client_id": STATE.get("client_id"),
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": STATE.get("code_verifier"),
            },
        )
    except RuntimeError as exc:
        set_flash(error=f"Could not connect Spotify: {exc}")
        return False

    store_token(payload)
    STATE["code_verifier"] = ""
    STATE["oauth_state"] = ""
    set_flash(message="Spotify account connected.")
    return True


def choose_playlist(playlist_id: str) -> None:
    playlists = fetch_all_playlists()
    for playlist in playlists:
        if playlist.get("id") == playlist_id:
            STATE["selected_playlist"] = {
                "id": playlist.get("id"),
                "name": playlist.get("name"),
                "uri": playlist.get("uri"),
                "items": playlist.get("items", {}),
                "owner": playlist.get("owner", {}),
            }
            set_flash(message=f"Selected playlist: {playlist.get('name')}")
            return
    set_flash(error="That playlist is not available in your account.")


def reset_state() -> None:
    STATE.clear()
    STATE.update(
        {
            "client_id": "",
            "code_verifier": "",
            "oauth_state": "",
            "token": None,
            "selected_playlist": None,
            "message": "",
            "error": "",
            "subsection_size": DEFAULT_SUBSECTION_SIZE,
        }
    )
    set_flash(message="Local Spotify state cleared. Your saved rankings are untouched.")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def send_html(self, body: str, status: int = 200, headers: dict[str, str] | None = None) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(encoded)

    def send_json(self, payload: Any, status: int = 200) -> None:
        encoded = json.dumps(payload, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/":
            self.send_html(render_homepage())
            return

        if path == "/debug/playlists":
            if not STATE.get("token"):
                self.send_json({"error": "Not connected to Spotify yet. Visit / and connect first."}, status=400)
                return

            # Raw first page, exactly as Spotify returns it — no parsing/mutation.
            raw = spotify_get("/me/playlists?limit=5")
            if raw is None:
                self.send_json(
                    {
                        "error": (
                            "spotify_get() returned None. This usually means the access "
                            "token is missing/expired and refresh also failed, or the "
                            "HTTP request itself errored out silently."
                        )
                    },
                    status=502,
                )
                return

            self.send_json(
                {
                    "note": (
                        "This is the raw, unmodified JSON from GET /me/playlists?limit=5. "
                        "Check each item's 'tracks' field directly."
                    ),
                    "raw_response": raw,
                }
            )
            return

        if path == "/login":
            client_id = STATE.get("client_id") or ""
            if not client_id:
                set_flash(error="Save a Spotify client ID before connecting.")
                self.redirect("/")
                return

            verifier = pkce_verifier()
            oauth_state = secrets.token_urlsafe(24)
            STATE["code_verifier"] = verifier
            STATE["oauth_state"] = oauth_state
            self.redirect(redirect_url(client_id, oauth_state, verifier))
            return

        if path == "/callback":
            error = query.get("error", [""])[0]
            if error:
                set_flash(error=f"Spotify sign-in failed: {error}")
                self.redirect("/")
                return

            if query.get("state", [""])[0] != STATE.get("oauth_state"):
                set_flash(error="Spotify returned an invalid state token.")
                self.redirect("/")
                return

            code = query.get("code", [""])[0]
            if not code:
                set_flash(error="Spotify did not return an authorization code.")
                self.redirect("/")
                return

            exchange_code(code)
            self.redirect("/")
            return

        if path == "/rank":
            size_param = query.get("size", [""])[0]
            if size_param.isdigit() and int(size_param) > 0:
                STATE["subsection_size"] = int(size_param)
            self.send_html(render_rank_page())
            return

        if path == "/subsections":
            self.send_html(render_subsections_page())
            return

        if path.startswith("/subsections/"):
            subsection_id = path[len("/subsections/"):].strip("/")
            sub = storage.get_subsection(subsection_id) if subsection_id else None
            if not sub:
                self.send_html("<h1>Ranking not found</h1>", status=404)
                return
            self.send_html(render_subsection_edit_page(sub))
            return

        self.send_html("<h1>Not found</h1>", status=404)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        form = parse_form(body)

        if parsed.path == "/configure":
            client_id = form.get("client_id", "").strip()
            if not client_id:
                set_flash(error="A Spotify client ID is required.")
                self.redirect("/")
                return

            STATE["client_id"] = client_id
            STATE["token"] = None
            STATE["selected_playlist"] = None
            STATE["code_verifier"] = ""
            STATE["oauth_state"] = ""
            STATE["user_id"] = None
            set_flash(message="Client ID saved locally. Connect Spotify to continue.")
            self.redirect("/")
            return

        if parsed.path == "/choose":
            playlist_id = form.get("playlist_id", "").strip()
            if not playlist_id:
                set_flash(error="Choose a playlist first.")
                self.redirect("/")
                return

            choose_playlist(playlist_id)
            self.redirect("/")
            return

        if parsed.path == "/reset":
            reset_state()
            self.redirect("/")
            return

        if parsed.path == "/rank/submit":
            order_raw = form.get("order", "").strip()
            order_ids = [tid for tid in order_raw.split(",") if tid]
            if len(order_ids) < 2:
                set_flash(error="That didn't look like a valid ranking — nothing was saved.")
                self.redirect("/rank")
                return

            selected = STATE.get("selected_playlist") or {}
            storage.add_subsection(order_ids, source_playlist_name=selected.get("name"))
            set_flash(message="Saved that ranking.")
            self.redirect("/rank")
            return

        if parsed.path.startswith("/subsections/") and parsed.path.endswith("/save"):
            subsection_id = parsed.path[len("/subsections/"):-len("/save")].strip("/")
            order_raw = form.get("order", "").strip()
            order_ids = [tid for tid in order_raw.split(",") if tid]
            if not subsection_id or len(order_ids) < 2:
                set_flash(error="That didn't look like a valid ranking — nothing was saved.")
                self.redirect("/subsections")
                return

            updated = storage.update_subsection(subsection_id, order_ids)
            if not updated:
                self.send_html("<h1>Ranking not found</h1>", status=404)
                return

            set_flash(message="Ranking updated.")
            self.redirect("/subsections")
            return

        if parsed.path == "/play":
            track_uri = form.get("track_uri", "").strip()
            context_uri = form.get("context_uri", "").strip() or None
            if not track_uri:
                self.send_json({"ok": False, "reason": "missing_track"}, status=400)
                return
            ok, reason = play_track_in_context(track_uri, context_uri)
            self.send_json({"ok": ok, "reason": reason})
            return

        if parsed.path == "/playlist/import":
            import_current_playlist_as_new()
            self.redirect("/rank")
            return

        self.send_html("<h1>Not found</h1>", status=404)


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """
    Same as ThreadingHTTPServer, except it doesn't print a full traceback
    every time a client disconnects mid-request (closed tab, navigated
    away, browser prefetch/cancel, etc.) — that's routine and not a bug,
    just noisy under the default socketserver behavior. Anything else
    still gets logged normally.
    """

    def handle_error(self, request, client_address) -> None:
        exc_type = sys.exc_info()[0]
        if exc_type is not None and issubclass(
            exc_type, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)
        ):
            return
        super().handle_error(request, client_address)


def main() -> None:
    server = QuietThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Local Spotify picker running at http://{HOST}:{PORT}")
    try:
        webbrowser.open(f"http://{HOST}:{PORT}")
    except Exception:
        pass
    server.serve_forever()


if __name__ == "__main__":
    main()