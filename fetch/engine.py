from __future__ import annotations

import json
import os
import signal
import shutil
import shlex
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlsplit
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .dependencies import managed_deno_path, managed_yt_dlp_path
from .paths import cache_dir


class EngineError(RuntimeError):
    pass


@dataclass
class FormatInfo:
    format_id: str
    label: str
    has_video: bool
    has_audio: bool


@dataclass
class PlaylistEntry:
    index: int
    title: str
    url: str


@dataclass
class MediaInfo:
    title: str
    uploader: str
    duration: int | None
    thumbnail: str | None
    webpage_url: str
    formats: list[FormatInfo]
    subtitles: list[str]
    automatic_subtitles: list[str]
    playlist_entries: list[PlaylistEntry]
    playlist_count: int

    @property
    def is_playlist(self) -> bool:
        return bool(self.playlist_entries) or self.playlist_count > 1


def find_binary(name: str) -> str | None:
    managed = {"yt-dlp": managed_yt_dlp_path, "deno": managed_deno_path}.get(name)
    if managed:
        path = managed()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return shutil.which(name)


def engine_environment() -> dict[str, str]:
    environment = os.environ.copy()
    managed_bin = managed_yt_dlp_path().parent
    if managed_bin.is_dir():
        environment["PATH"] = str(managed_bin) + os.pathsep + environment.get("PATH", "")
    return environment


def binary_version(name: str) -> str | None:
    path = find_binary(name)
    if not path:
        return None
    try:
        result = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=5, check=False
        )
        output = (result.stdout or result.stderr).strip().splitlines()
        return output[0] if output else "Version unavailable"
    except (OSError, subprocess.TimeoutExpired):
        return "Version unavailable"


def _base_args(options: dict[str, object] | None = None) -> list[str]:
    options = options or {}
    args = []
    if not bool(options.get("use_yt_dlp_config", False)):
        args.append("--ignore-config")
    cookies_file = str(options.get("cookies_file") or "")
    browser_cookies = str(options.get("browser_cookies") or "")
    if cookies_file:
        args.extend(["--cookies", cookies_file])
    elif browser_cookies:
        args.extend(["--cookies-from-browser", browser_cookies])
    proxy = str(options.get("proxy") or "").strip()
    if proxy:
        args.extend(["--proxy", proxy])
    args.extend(["--retries", str(max(0, min(100, int(options.get("retry_count", 10) or 0))))])
    if bool(options.get("no_playlist", False)):
        args.append("--no-playlist")
    return args


def _is_video_playlist_url(url: str) -> bool:
    query = parse_qs(urlsplit(url).query)
    return bool(query.get("v", [""])[0] and query.get("list", [""])[0])


def _quality_args(quality_preference: str, max_resolution: str, *, audio_only: bool = False) -> list[str]:
    if audio_only:
        return []
    args = []
    if quality_preference == "compatible":
        args.extend(["-S", "vcodec:h264,lang,quality,res,fps,hdr:12,acodec:aac", "--merge-output-format", "mp4"])
    if max_resolution in {"2160", "1440", "1080", "720", "480"}:
        args.extend(["-S", f"res:{max_resolution}"])
    return args


def inspect_url(
    url: str,
    options: dict[str, object] | None = None,
    *,
    cancel_event: threading.Event | None = None,
    timeout: float = 90,
) -> tuple[MediaInfo, Path | None]:
    executable = find_binary("yt-dlp")
    if not executable:
        raise EngineError("yt-dlp isn’t installed or isn’t available on PATH. Install yt-dlp, then try again.")

    command = [
        executable,
        *_base_args(options),
        "--no-warnings",
        "--flat-playlist",
        "--skip-download",
        "--dump-single-json",
        "--",
        url,
    ]
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=engine_environment(),
        )
    except OSError as error:
        raise EngineError(f"Fetch couldn’t start yt-dlp: {error}") from error
    deadline = time.monotonic() + timeout
    while True:
        if cancel_event and cancel_event.is_set():
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
                process.communicate()
            raise EngineError("Inspection cancelled.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            process.communicate()
            raise EngineError("Inspecting timed out. Check your connection or update yt-dlp.")
        try:
            stdout, stderr = process.communicate(timeout=min(0.25, remaining))
            break
        except subprocess.TimeoutExpired:
            continue
    if process.returncode:
        raise EngineError(_friendly_error(stderr or stdout))

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise EngineError("yt-dlp returned information Fetch couldn’t read. Try updating yt-dlp and inspect the log.") from error

    raw_entries = data.get("entries") or []
    entries: list[PlaylistEntry] = []
    if isinstance(raw_entries, list):
        for fallback_index, entry in enumerate(raw_entries, start=1):
            if not isinstance(entry, dict):
                continue
            index = _to_int(entry.get("playlist_index")) or fallback_index
            entries.append(
                PlaylistEntry(
                    index=index,
                    title=str(entry.get("title") or f"Item {index}"),
                    url=str(entry.get("webpage_url") or entry.get("url") or url),
                )
            )

    duration = _to_int(data.get("duration"))
    formats = _format_list(data.get("formats"))
    raw_subtitles = data.get("subtitles") or {}
    raw_auto = data.get("automatic_captions") or {}
    info = MediaInfo(
        title=str(data.get("title") or "Untitled media"),
        uploader=str(data.get("uploader") or data.get("channel") or ""),
        duration=duration,
        thumbnail=data.get("thumbnail") if isinstance(data.get("thumbnail"), str) else None,
        webpage_url=str(data.get("webpage_url") or url),
        formats=formats,
        subtitles=sorted(raw_subtitles.keys()) if isinstance(raw_subtitles, dict) else [],
        automatic_subtitles=sorted(raw_auto.keys()) if isinstance(raw_auto, dict) else [],
        playlist_entries=entries,
        playlist_count=_to_int(data.get("playlist_count")) or len(entries),
    )
    return info, _cache_thumbnail(info.thumbnail)


ProgressCallback = Callable[[dict[str, object]], None]
ProcessCallback = Callable[[subprocess.Popen[str] | None], None]
CommandCallback = Callable[[str], None]


def download_url(
    url: str,
    destination: Path,
    output: ProgressCallback,
    register_process: ProcessCallback,
    command_ready: CommandCallback,
    *,
    format_spec: str = "bestvideo*+bestaudio/best",
    audio_format: str | None = None,
    subtitle_languages: str = "",
    embed_subtitles: bool = False,
    automatic_subtitles: bool = False,
    embed_chapters: bool = True,
    playlist_items: str = "",
    use_yt_dlp_config: bool = False,
    cookies_file: str = "",
    browser_cookies: str = "",
    proxy: str = "",
    rate_limit: str = "",
    retry_count: int = 10,
    no_playlist: bool = False,
    quality_preference: str = "compatible",
    max_resolution: str = "",
    is_playlist: bool = False,
    archive_file: str = "",
    sponsorblock: bool = False,
    embed_metadata: bool = True,
    embed_thumbnail: bool = True,
    include_media_id: bool = False,
    custom_arguments: str = "",
) -> tuple[int, str]:
    executable = find_binary("yt-dlp")
    if not executable:
        raise EngineError("yt-dlp isn’t installed or isn’t available on PATH. Install yt-dlp, then try again.")
    destination.mkdir(parents=True, exist_ok=True)
    command = [executable]
    base_options = {
        "use_yt_dlp_config": use_yt_dlp_config,
        "cookies_file": cookies_file,
        "browser_cookies": browser_cookies,
        "proxy": proxy,
        "retry_count": retry_count,
        "no_playlist": no_playlist,
    }
    title_template = "%(artist,creator,uploader,channel|Unknown Artist)s - %(title|Untitled)s" if audio_format else "%(title|Untitled)s"
    if is_playlist:
        filename_template = "%(playlist_title|Playlist)s/%(playlist_index)03d - " + title_template
    else:
        filename_template = title_template
    if include_media_id:
        filename_template += " [%(id)s]"
    command.extend(_base_args(base_options))
    command.extend([
        "--no-warnings",
        "--newline",
        "--progress-delta",
        "0.5",
        "--progress-template",
        "download:FETCH_PROGRESS\t%(progress.status)s\t%(progress.downloaded_bytes)s\t%(progress.total_bytes)s\t%(progress.total_bytes_estimate)s\t%(progress.speed)s\t%(progress.eta)s",
        "--progress-template",
        "postprocess:FETCH_PROGRESS\t%(progress.status)s\t%(progress.downloaded_bytes)s\t%(progress.total_bytes)s\t%(progress.total_bytes_estimate)s\t%(progress.speed)s\t%(progress.eta)s",
        "--format",
        format_spec,
        "--output",
        filename_template + ".%(ext)s",
        "--paths",
        str(destination),
    ])
    command.extend(_quality_args(quality_preference, max_resolution, audio_only=bool(audio_format)))
    if audio_format:
        command.extend(["--extract-audio", "--audio-format", audio_format])
    if embed_chapters:
        command.append("--embed-chapters")
    if playlist_items:
        command.extend(["--playlist-items", playlist_items])
    if rate_limit:
        command.extend(["--limit-rate", rate_limit])
    if archive_file:
        command.extend(["--download-archive", archive_file])
    if sponsorblock:
        command.extend(["--sponsorblock-remove", "sponsor"])
    if embed_metadata:
        command.append("--embed-metadata")
    if embed_thumbnail:
        command.append("--embed-thumbnail")
        if audio_format:
            command.extend([
                "--convert-thumbnails", "jpg>png/png>jpg/webp>png",
                "--ppa", r"ThumbnailsConvertor+ffmpeg_o:-vf crop=min(iw\\,ih):min(iw\\,ih)",
            ])
    if subtitle_languages:
        command.extend(["--write-subs", "--sub-langs", subtitle_languages])
        if automatic_subtitles:
            command.append("--write-auto-subs")
        if embed_subtitles:
            command.append("--embed-subs")
    elif automatic_subtitles:
        command.extend(["--write-auto-subs", "--sub-langs", "all"])
        if embed_subtitles:
            command.append("--embed-subs")
    try:
        command.extend(shlex.split(custom_arguments))
    except ValueError as error:
        raise EngineError(f"Custom yt-dlp arguments have unmatched quotes: {error}") from error
    command.extend(["--", url])
    rendered_command = " ".join(_quote_arg(arg) for arg in command)
    command_ready(rendered_command)
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
            env=engine_environment(),
        )
        register_process(process)
        if process.stdout:
            for line in process.stdout:
                line = line.rstrip()
                if line:
                    event = _progress_event(line)
                    if event is None:
                        output({"type": "log", "message": line})
                    else:
                        output(event)
        return process.wait(), rendered_command
    except OSError as error:
        raise EngineError(f"Fetch couldn’t start yt-dlp: {error}") from error
    finally:
        register_process(None)


def _progress_event(line: str) -> dict[str, object] | None:
    marker = "FETCH_PROGRESS\t"
    position = line.find(marker)
    if position < 0:
        return None
    fields = line[position + len(marker):].split("\t")
    if len(fields) < 6:
        return None
    status, downloaded, total, estimate, speed, eta = fields[:6]
    downloaded_bytes = _to_float(downloaded)
    total_bytes = _to_float(total) or _to_float(estimate)
    percent = None
    if downloaded_bytes is not None and total_bytes:
        percent = max(0.0, min(100.0, downloaded_bytes * 100 / total_bytes))
    return {
        "type": "progress",
        "status": status,
        "percent": percent,
        "speed": _pretty_speed(_to_float(speed)),
        "eta": _pretty_eta(_to_int(eta)),
        "message": "Processing media…" if status == "finished" else "Downloading…",
    }


def _format_list(raw_formats: object) -> list[FormatInfo]:
    if not isinstance(raw_formats, list):
        return []
    formatted: list[FormatInfo] = []
    seen: set[str] = set()
    for item in raw_formats:
        if not isinstance(item, dict) or not item.get("format_id"):
            continue
        format_id = str(item["format_id"])
        if format_id in seen:
            continue
        seen.add(format_id)
        height = _to_int(item.get("height"))
        resolution = f"{height}p" if height else str(item.get("resolution") or "Audio")
        extension = str(item.get("ext") or "unknown")
        codecs = "/".join(
            value for value in (str(item.get("vcodec") or ""), str(item.get("acodec") or ""))
            if value and value != "none"
        )
        dimensions = "with audio" if item.get("acodec") not in (None, "none") else "video only"
        if item.get("vcodec") in (None, "none"):
            dimensions = "audio only"
        label = f"{resolution} · {extension} · {dimensions}"
        if codecs:
            label += f" · {codecs}"
        formatted.append(
            FormatInfo(
                format_id=format_id,
                label=label,
                has_video=item.get("vcodec") not in (None, "none"),
                has_audio=item.get("acodec") not in (None, "none"),
            )
        )
    formatted.sort(key=lambda value: (not value.has_video, value.format_id))
    return formatted


def _friendly_error(raw: str) -> str:
    message = raw.strip() or "yt-dlp couldn’t inspect this link."
    lowered = message.lower()
    if "sign in to confirm" in lowered or "age-restricted" in lowered or "age restricted" in lowered:
        return f"{message}\n\nThis media may require account cookies. Import a cookies.txt file or choose a browser under Settings → Cookies."
    if "ffmpeg" in lowered and ("not found" in lowered or "not installed" in lowered or "merg" in lowered):
        return f"{message}\n\nInstall ffmpeg and ffprobe to merge separate video and audio streams."
    if "no supported javascript runtime" in lowered or "js runtime" in lowered:
        return f"{message}\n\nInstall Deno 2.3 or later from Engine settings for full YouTube support."
    if "yt-dlp-ejs" in lowered or "ejs" in lowered and "not found" in lowered:
        return f"{message}\n\nUpdate yt-dlp or install its matching yt-dlp-ejs scripts from Engine settings."
    if "403" in lowered or "extractor" in lowered or "unsupported url" in lowered:
        return f"{message}\n\nCheck the link and update yt-dlp in Settings → Engine if this site recently changed."
    return message


def _to_int(value: object) -> int | None:
    try:
        return int(value) if value not in (None, "NA", "None", "") else None
    except (TypeError, ValueError):
        return None


def _to_float(value: object) -> float | None:
    try:
        return float(value) if value not in (None, "NA", "None", "") else None
    except (TypeError, ValueError):
        return None


def _pretty_speed(value: float | None) -> str:
    if value is None:
        return ""
    units = ("B/s", "KB/s", "MB/s", "GB/s")
    for unit in units:
        if value < 1000 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1000
    return ""


def _pretty_eta(seconds: int | None) -> str:
    if seconds is None:
        return ""
    minutes, seconds = divmod(max(0, seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d} remaining"
    return f"{minutes}:{seconds:02d} remaining"


def _cache_thumbnail(url: str | None) -> Path | None:
    if not url:
        return None
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Fetch/0.1"})
        with urllib.request.urlopen(request, timeout=8) as response:
            content_type = response.headers.get_content_type()
            if not content_type.startswith("image/"):
                return None
            data = response.read(5 * 1024 * 1024 + 1)
        if len(data) > 5 * 1024 * 1024:
            return None
        folder = cache_dir() / "thumbnails"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "current-thumbnail"
        path.write_bytes(data)
        return path
    except (OSError, urllib.error.URLError, TimeoutError, ValueError):
        return None


def _quote_arg(value: str) -> str:
    import shlex

    return shlex.quote(value)
