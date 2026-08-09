from __future__ import annotations

import json
import os
import secrets
import threading
import time
from typing import Any

# Everything here is keyed by Spotify track ID, never by playlist, which is
# what lets a ranked subsection apply to any playlist that happens to share
# songs with it. Data lives in one local JSON file next to this script, so
# rankings survive restarts and browser reloads without any external
# services or packages.
DATA_FILE = os.getenv(
    "RANKER_DATA_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ranker_data.json"),
)

_LOCK = threading.Lock()

_EMPTY: dict[str, Any] = {"songs": {}, "subsections": []}


def _load() -> dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        return {"songs": {}, "subsections": []}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"songs": {}, "subsections": []}
    data.setdefault("songs", {})
    data.setdefault("subsections", [])
    return data


def _save(data: dict[str, Any]) -> None:
    tmp_path = DATA_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, DATA_FILE)


def upsert_songs(songs: dict[str, dict[str, Any]]) -> None:
    """Cache/refresh metadata (title/artist/album/uri) for a batch of track IDs."""
    if not songs:
        return
    with _LOCK:
        data = _load()
        data["songs"].update(songs)
        _save(data)


def get_song(track_id: str) -> dict[str, Any] | None:
    with _LOCK:
        data = _load()
    return data["songs"].get(track_id)


def get_songs(track_ids: list[str]) -> dict[str, dict[str, Any]]:
    with _LOCK:
        data = _load()
    return {tid: data["songs"][tid] for tid in track_ids if tid in data["songs"]}


def list_subsections() -> list[dict[str, Any]]:
    """All persisted subsections, oldest first (the order the algorithm expects)."""
    with _LOCK:
        data = _load()
    return data["subsections"]


def get_subsection(subsection_id: str) -> dict[str, Any] | None:
    with _LOCK:
        data = _load()
    for sub in data["subsections"]:
        if sub["id"] == subsection_id:
            return sub
    return None


def add_subsection(track_ids: list[str], source_playlist_name: str | None = None) -> dict[str, Any]:
    """Persist a newly-ranked subsection. `track_ids` order = rank order (best first)."""
    record = {
        "id": secrets.token_urlsafe(8),
        "track_ids": list(track_ids),
        "source_playlist_name": source_playlist_name,
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    with _LOCK:
        data = _load()
        data["subsections"].append(record)
        _save(data)
    return record


def update_subsection(subsection_id: str, track_ids: list[str]) -> dict[str, Any] | None:
    with _LOCK:
        data = _load()
        for sub in data["subsections"]:
            if sub["id"] == subsection_id:
                sub["track_ids"] = list(track_ids)
                sub["updated_at"] = time.time()
                _save(data)
                return sub
    return None