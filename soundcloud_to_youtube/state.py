"""Persistenter State pro SoundCloud-URL.

Stages:
  - "downloaded"     MP3 liegt vor
  - "video_created"  MP4 liegt vor
  - "uploaded"       erfolgreich auf YouTube
  - "failed"         Upload final fehlgeschlagen (nach allen Retries)

Damit:
  - bereits konvertierte Tracks werden NICHT erneut heruntergeladen / re-encoded
  - fehlgeschlagene Uploads koennen spaeter mit --retry-pending nachgereicht werden
"""

import json
import os
from typing import Optional

from .config import get_state_path


def _load() -> dict:
    path = get_state_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    path = get_state_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def get(url: str) -> Optional[dict]:
    return _load().get(url)


def upsert(url: str, **fields) -> dict:
    data = _load()
    entry = data.get(url, {"url": url, "attempts": 0})
    entry.update(fields)
    data[url] = entry
    _save(data)
    return entry


def increment_attempts(url: str) -> int:
    data = _load()
    entry = data.get(url, {"url": url, "attempts": 0})
    entry["attempts"] = entry.get("attempts", 0) + 1
    data[url] = entry
    _save(data)
    return entry["attempts"]


def reset_attempts(url: str) -> None:
    data = _load()
    if url in data:
        data[url]["attempts"] = 0
        _save(data)


def pending() -> list:
    """Eintraege deren Stage nicht 'uploaded' ist und die ein Video haben."""
    out = []
    for url, entry in _load().items():
        if entry.get("stage") == "uploaded":
            continue
        if entry.get("video") and os.path.exists(entry["video"]):
            out.append(entry)
    return out


def all_entries() -> list:
    return list(_load().values())


def remove(url: str) -> None:
    data = _load()
    if url in data:
        del data[url]
        _save(data)
