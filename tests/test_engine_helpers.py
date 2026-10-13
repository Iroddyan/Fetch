from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

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
