from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fetch import lyrics


def test_parse_synced_response() -> None:
    assert lyrics.parse_lrclib_response('{"syncedLyrics":"[00:01.00]Line\\n"}') == "[00:01.00]Line\n"


def test_plain_only_response_is_not_selected() -> None:
    assert lyrics.parse_lrclib_response('{"plainLyrics":"plain text","syncedLyrics":null}') is None


def test_empty_or_invalid_responses_are_misses() -> None:
    assert lyrics.parse_lrclib_response('{"syncedLyrics":"  "}') is None
    assert lyrics.parse_lrclib_response("not json") is None
    assert lyrics.parse_lrclib_response("[]") is None


def test_lyrics_filename_uses_audio_basename(tmp_path: Path) -> None:
    assert lyrics.lyrics_path_for_audio(tmp_path / "Artist - Song.m4a") == tmp_path / "Artist - Song.lrc"


def test_write_synced_lyrics_never_overwrites_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "song.lrc"
    path.write_text("existing", encoding="utf-8")
    assert not lyrics.write_synced_lyrics(path, "replacement")
    assert path.read_text(encoding="utf-8") == "existing"


def test_lookup_uses_only_signature_fields_and_identifies_fetch(monkeypatch) -> None:
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"syncedLyrics":"[00:01.00]line"}'

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(lyrics, "urlopen", fake_urlopen)
    assert lyrics.fetch_synced_lyrics("Artist", "Title", "Album", 123.4) == "[00:01.00]line"
    request = captured["request"]
    assert urlsplit(request.full_url).path == "/api/get"
    assert parse_qs(urlsplit(request.full_url).query) == {
        "artist_name": ["Artist"], "track_name": ["Title"], "album_name": ["Album"], "duration": ["123"],
    }
    assert request.get_header("User-agent") == f"Fetch/{lyrics.APP_VERSION} (https://github.com/Iroddyan/Fetch)"
    assert captured["timeout"] == 10


def test_lookup_swallows_http_errors_and_skips_missing_signature(monkeypatch) -> None:
    def failed_request(*_args, **_kwargs):
        raise OSError("offline")

    monkeypatch.setattr(lyrics, "urlopen", failed_request)
    assert lyrics.fetch_synced_lyrics("Artist", "Title", "Album", 120) is None
    assert lyrics.fetch_synced_lyrics("", "Title", "Album", 120) is None
