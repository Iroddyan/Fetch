from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from fetch import dependencies, engine


def test_progress_event_parses_playlist_and_stage() -> None:
    event = engine._progress_event(
        "FETCH_PROGRESS\tdownloading\t50\t100\tNA\t25\t2\t12\t50\tavc1\tNA\tNA"
    )
    assert event is not None
    assert event["percent"] == 50
    assert event["playlist_index"] == 12
    assert event["playlist_count"] == 50
    assert event["stage"] == "Video"
    assert event["indeterminate"] is False


def test_progress_event_unknown_total_is_indeterminate() -> None:
    event = engine._progress_event("FETCH_PROGRESS\tdownloading\t10\tNA\tNA\tNA\tNA")
    assert event is not None
    assert event["percent"] is None
    assert event["indeterminate"] is True


def test_progress_event_reads_title_from_documented_info_field() -> None:
    event = engine._progress_event("FETCH_PROGRESS\tdownloading\t0\t100\tNA\tNA\tNA\t1\t4\tavc1\tNA\tNA\tTrack title")
    assert event is not None
    assert event["title"] == "Track title"


def test_quality_args_order_preserves_h264_preference_and_applies_resolution_cap() -> None:
    compatible = engine._quality_args("compatible", "")
    assert compatible == ["-S", "vcodec:h264,lang,quality,res,fps,hdr:12,acodec:aac", "--merge-output-format", "mp4"]
    compatible_capped = engine._quality_args("compatible", "720")
    assert compatible_capped[:4] == compatible
    assert compatible_capped[4:] == ["-S", "res:720"]
    assert engine._quality_args("best", "") == []
    assert engine._quality_args("best", "1080") == ["-S", "res:1080"]
    assert engine._quality_args("compatible", "720", audio_only=True) == []


def test_audio_format_spec_prefers_native_streams() -> None:
    assert engine._audio_format_spec("m4a") == "bestaudio[ext=m4a]/bestaudio/best"
    assert engine._audio_format_spec("opus") == "bestaudio[acodec=opus]/bestaudio/best"
    assert engine._audio_format_spec("mp3") == "bestaudio/best"
    assert engine._audio_format_spec("flac") == "bestaudio/best"
    assert engine._audio_format_spec(None) == "bestaudio/best"


def test_parse_done_line_with_unicode_and_spaced_path() -> None:
    event = engine._parse_done_line('FETCH_DONE:{"filepath":"/tmp/Música/My Song.m4a","artist":"Beyoncé","title":"Song","album":"Álbum","duration":211}')
    assert event == {
        "type": "done", "output_path": "/tmp/Música/My Song.m4a", "artist": "Beyoncé",
        "title": "Song", "album": "Álbum", "duration": 211.0,
    }


@pytest.mark.parametrize("line", ["ordinary output", "FETCH_DONE:{bad json}", "FETCH_DONE:[]", 'FETCH_DONE:{"title":"no path"}'])
def test_parse_done_line_rejects_invalid_or_missing_path(line: str) -> None:
    assert engine._parse_done_line(line) is None


def test_parse_done_line_allows_missing_optional_metadata() -> None:
    assert engine._parse_done_line('FETCH_DONE:{"filepath":"/tmp/track.m4a"}') == {
        "type": "done", "output_path": "/tmp/track.m4a", "artist": "", "title": "",
        "album": "", "duration": None,
    }


@pytest.mark.skipif(shutil.which("yt-dlp") is None, reason="yt-dlp is not installed")
def test_m4a_selector_chooses_aac_in_synthetic_info_json(tmp_path: Path) -> None:
    info = {
        "id": "synthetic",
        "title": "Synthetic song",
        "extractor": "test",
        "extractor_key": "Test",
        "webpage_url": "https://example.test/synthetic",
        "original_url": "https://example.test/synthetic",
        "formats": [
            {
                "format_id": "251", "url": "https://example.test/opus", "ext": "webm",
                "acodec": "opus", "vcodec": "none", "protocol": "https", "filesize": 100,
            },
            {
                "format_id": "140", "url": "https://example.test/aac", "ext": "m4a",
                "acodec": "mp4a.40.2", "vcodec": "none", "protocol": "https", "filesize": 100,
            },
        ],
    }
    info_path = tmp_path / "synthetic.info.json"
    info_path.write_text(json.dumps(info), encoding="utf-8")
    result = subprocess.run(
        [
            "yt-dlp", "--load-info-json", str(info_path), "--simulate", "--format",
            engine._audio_format_spec("m4a"), "--print", "%(format_id)s %(acodec)s",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "140 mp4a.40.2"


def test_is_video_playlist_url_and_base_no_playlist_flag() -> None:
    assert engine._is_video_playlist_url("https://www.youtube.com/watch?v=video&list=playlist")
    assert engine._is_video_playlist_url("https://youtu.be/video?list=playlist")
    assert engine._is_video_playlist_url("https://music.youtube.com/watch?v=video&list=playlist")
    assert engine._is_video_playlist_url("https://youtube.com/watch?list=playlist&v=video")
    assert not engine._is_video_playlist_url("https://www.youtube.com/playlist?list=playlist")
    assert not engine._is_video_playlist_url("https://www.youtube.com/watch?v=video")
    assert not engine._is_video_playlist_url("https://youtu.be/video")
    assert not engine._is_video_playlist_url("https://example.com/watch?v=video&list=playlist")
    assert "--no-playlist" in engine._base_args({"no_playlist": True})
    assert "--no-playlist" not in engine._base_args({"no_playlist": False})


def test_format_list_deduplicates_and_labels_formats() -> None:
    result = engine._format_list([
        {"format_id": "137", "height": 1080, "ext": "mp4", "vcodec": "avc1", "acodec": "none"},
        {"format_id": "137", "height": 1080, "ext": "mp4", "vcodec": "avc1", "acodec": "none"},
        {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a"},
    ])
    assert len(result) == 2
    assert result[0].format_id == "137"
    assert "1080p" in result[0].label and result[0].has_video and not result[0].has_audio
    assert result[1].has_audio and not result[1].has_video


def test_playlist_audio_uses_the_standalone_music_filename() -> None:
    single_track = engine._filename_template(False, "m4a", False)
    playlist_track = engine._filename_template(True, "m4a", False)
    assert playlist_track == single_track
    assert playlist_track == "%(artist,creator,uploader,channel|Unknown Artist)s - %(title|Untitled)s"
    assert engine._filename_template(True, "m4a", True).endswith(" [%(id)s]")
    assert engine._filename_template(True, None, False).startswith("%(playlist_title|Playlist)s/%(playlist_index)03d - ")


def test_friendly_error_maps_common_errors_and_ignores_incidental_status_digits() -> None:
    cases = [
        ("ERROR: HTTP Error 429: Too Many Requests", "import_cookies"),
        ("ERROR: HTTP Error 403: Forbidden", "update_ytdlp"),
        ("ERROR: This video is private", "none"),
        ("ERROR: This video is not available in your country", "none"),
        ("ERROR: Requested format is not available", "none"),
        ("ERROR: Sign in to confirm your age", "import_cookies"),
        ("ERROR: ffmpeg not found", "install_ffmpeg"),
    ]
    for raw, action in cases:
        assert engine._friendly_error(raw).action == action

    incidental = engine._friendly_error("ERROR: video 403not-status has 1080 bytes")
    assert incidental.action == "none"
    assert "403not-status" in incidental.message


def test_checksum_for_file_matches_exact_filename() -> None:
    checksum = "a" * 64
    assert dependencies._checksum_for_file(f"{checksum} *yt-dlp\n", "yt-dlp") == checksum
    assert dependencies._checksum_for_file(f"{checksum} *other\n", "yt-dlp") is None


def test_pretty_speed_and_eta() -> None:
    assert engine._pretty_speed(1000) == "1.0 KB/s"
    assert engine._pretty_speed(None) == ""
    assert engine._pretty_eta(3661) == "1:01:01 remaining"
    assert engine._pretty_eta(None) == ""


def test_inspect_and_download_share_cookie_proxy_and_retry_arguments(tmp_path: Path) -> None:
    options = {
        "cookies_file": "/tmp/cookies.txt",
        "browser_cookies": "",
        "proxy": "socks5://localhost:9050",
        "retry_count": 4,
    }
    commands: list[list[str]] = []

    class InspectProcess:
        pid = 111
        returncode = 0

        def communicate(self, timeout=None):
            return json.dumps({"title": "Sample", "formats": []}), ""

    class DownloadProcess:
        stdout = ()

        def __init__(self, command: list[str]):
            self.command = command

        def wait(self):
            return 0

    def popen(command, **kwargs):
        commands.append(list(command))
        if len(commands) == 1:
            return InspectProcess()
        return DownloadProcess(command)

    with (
        patch.object(engine, "find_binary", return_value="yt-dlp"),
        patch.object(engine, "engine_environment", return_value={}),
        patch.object(engine, "_cache_thumbnail", return_value=None),
        patch.object(engine.subprocess, "Popen", side_effect=popen),
    ):
        engine.inspect_url("https://example.com/video", options)
        engine.download_url(
            "https://example.com/video", tmp_path, lambda _event: None,
            lambda _process: None, lambda _command: None,
            cookies_file=str(options["cookies_file"]), proxy=str(options["proxy"]),
            retry_count=int(options["retry_count"]),
        )

    shared = ["--ignore-config", "--cookies", "/tmp/cookies.txt", "--proxy", "socks5://localhost:9050", "--retries", "4"]
    assert commands[0][1:1 + len(shared)] == shared
    assert commands[1][1:1 + len(shared)] == shared
