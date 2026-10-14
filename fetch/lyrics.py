from __future__ import annotations

import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import APP_VERSION


LRCLIB_GET_URL = "https://lrclib.net/api/get"


def select_synced_lyrics(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    lyrics = payload.get("syncedLyrics")
    return lyrics if isinstance(lyrics, str) and lyrics.strip() else None


def parse_lrclib_response(data: bytes | str) -> str | None:
    try:
        return select_synced_lyrics(json.loads(data))
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return None


def lyrics_path_for_audio(audio_path: str | Path) -> Path:
    return Path(audio_path).with_suffix(".lrc")


def write_synced_lyrics(path: str | Path, lyrics: str) -> bool:
    try:
        with Path(path).open("x", encoding="utf-8", newline="") as output:
            output.write(lyrics)
    except OSError:
        return False
    return True


def fetch_synced_lyrics(artist: str, title: str, album: str, duration: float | None) -> str | None:
    if not artist.strip() or not title.strip() or not album.strip() or not duration:
        return None
    params = urlencode({
        "artist_name": artist,
        "track_name": title,
        "album_name": album,
        "duration": round(duration),
    })
    request = Request(
        f"{LRCLIB_GET_URL}?{params}",
        headers={"User-Agent": f"Fetch/{APP_VERSION} (https://github.com/Iroddyan/Fetch)"},
    )
    try:
        with urlopen(request, timeout=10) as response:
            return parse_lrclib_response(response.read())
    except (HTTPError, URLError, OSError, TimeoutError, ValueError):
        return None


def save_synced_lyrics(audio_path: str | Path, artist: str, title: str, album: str, duration: float | None) -> bool:
    lyrics = fetch_synced_lyrics(artist, title, album, duration)
    return bool(lyrics and write_synced_lyrics(lyrics_path_for_audio(audio_path), lyrics))
