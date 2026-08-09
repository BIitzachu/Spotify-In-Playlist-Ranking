from __future__ import annotations

from typing import Any, Callable

# `fetch` is expected to behave like app.py's `spotify_get`: given a path
# relative to the Spotify API base (e.g. "/playlists/abc/tracks?limit=100"),
# it returns the parsed JSON response, or None on failure. This keeps this
# module free of any HTTP/auth logic (and of the `spotipy` dependency), so
# it stays consistent with the rest of the project's "stdlib only" approach.
FetchFn = Callable[[str], "dict[str, Any] | None"]


def get_playlist_as_ranking_dict(playlist_id: str, fetch: FetchFn) -> dict[str, dict[str, Any]]:
    """
    Fetch every track in a Spotify playlist and return it as an unsorted
    dictionary keyed by Spotify track ID.

    Keying by track ID (rather than "Title - Artist") is what makes a
    ranked subsection portable across playlists: the same song added to
    two different playlists shares one ID, so a ranking learned while
    sorting one playlist applies to the other automatically.

    Args:
        playlist_id: Spotify playlist ID.
        fetch: callable(path) -> parsed JSON dict or None. See `FetchFn`.

    Returns:
        Dict of track_id -> {"title", "artist", "album", "uri"}.
        Local files and any track missing an ID are skipped, since they
        don't have a stable identity to key on.
    """
    playlist_dict: dict[str, dict[str, Any]] = {}
    # Spotify's March 2026 API migration removed GET/POST/PUT/DELETE on
    # /playlists/{id}/tracks entirely (it now 403s) in favor of
    # /playlists/{id}/items, with each paging entry's "track" field renamed
    # to "item" — same rename pattern as the "tracks"->"items" summary
    # field on /me/playlists.
    path = (
        f"/playlists/{playlist_id}/items"
        "?limit=100&fields=items(item(id,name,uri,artists(name),album(name))),next"
    )

    while path:
        payload = fetch(path)
        if not payload:
            break

        for entry in payload.get("items", []):
            track = entry.get("item")
            if not track:
                continue

            track_id = track.get("id")
            if not track_id:
                # Local files / some podcast episodes have no stable ID.
                continue

            artists = track.get("artists") or []
            artist = artists[0]["name"] if artists else "Unknown Artist"
            album = track.get("album") or {}

            playlist_dict[track_id] = {
                "title": track.get("name") or "Untitled",
                "artist": artist,
                "album": album.get("name"),
                "uri": track.get("uri"),
            }

        next_url = payload.get("next")
        if not next_url:
            break
        # `next` is a full URL; `fetch` wants a path relative to the API base.
        marker = "/v1"
        idx = next_url.find(marker)
        path = next_url[idx + len(marker):] if idx != -1 else None

    return playlist_dict