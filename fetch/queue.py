from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import uuid
from urllib.parse import parse_qs, urlsplit
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .engine import EngineError, _audio_format_spec, _friendly_error, download_url
from .paths import state_dir


def extract_http_urls(text: str) -> tuple[list[str], int]:
    """Return unique valid HTTP(S) URLs and the number of ignored nonblank lines."""
    urls: list[str] = []
    seen: set[str] = set()
    skipped = 0
    for line in text.splitlines():
        url = line.strip()
        if not url:
            continue
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)
        except ValueError:
            valid = False
        if not valid or url in seen:
            skipped += 1
            continue
        seen.add(url)
        urls.append(url)
    return urls, skipped


def is_playlist_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return parsed.path.rstrip("/").endswith("/playlist") or bool(parse_qs(parsed.query).get("list", [""])[0])


def _is_error_line(message: str) -> bool:
    return message.lstrip().upper().startswith("ERROR:")


def _idle_add(callback: Callable, *args: object) -> object:
    try:
        from gi.repository import GLib
    except ImportError:
        callback(*args)
        return None
    return GLib.idle_add(callback, *args)


@dataclass
class QueueItem:
    id: str
    url: str
    title: str
    options: dict[str, object]
    status: str = "waiting"
    progress: float = 0.0
    entry_progress: float = 0.0
    progress_indeterminate: bool = False
    playlist_index: int | None = None
    playlist_count: int | None = None
    stage: str = ""
    speed: str = ""
    eta: str = ""
    attempt: int = 0
    error: str = ""
    error_hint: str = ""
    error_action: str = "none"
    logged_errors: list[str] = field(default_factory=list)
    command: str = ""
    logs: list[str] = field(default_factory=list)
    output_path: str = ""
    artist: str = ""
    media_title: str = ""
    album: str = ""
    duration: float | None = None
    updated_at: float = field(default_factory=time.time)

    @classmethod
    def create(cls, url: str, title: str, options: dict[str, object]) -> QueueItem:
        return cls(id=uuid.uuid4().hex, url=url, title=title, options=options)


class QueueManager:
    """Persistent, bounded subprocess queue with explicit recovery after restart."""

    def __init__(
        self,
        destination: Callable[[], Path],
        concurrency: int = 2,
        scheduler: Callable[..., object] = _idle_add,
    ) -> None:
        self.path = state_dir() / "queue.json"
        self.destination = destination
        self.scheduler = scheduler
        self.concurrency = max(1, min(4, concurrency))
        self._lock = threading.RLock()
        self._items: list[QueueItem] = []
        self._listeners: list[Callable[[list[QueueItem]], None]] = []
        self._active: dict[str, Future[None]] = {}
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._cancel_requested: set[str] = set()
        self._pause_requested: set[str] = set()
        self._last_saved = 0.0
        self._started = False
        self._start_explicitly_required = False
        self._closed = False
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="fetch-queue")
        self._load()
        self._start_explicitly_required = any(
            item.status in {"waiting", "interrupted"} for item in self._items
        )

    def subscribe(self, callback: Callable[[list[QueueItem]], None]) -> None:
        with self._lock:
            self._listeners.append(callback)
        self._emit()

    def unsubscribe(self, callback: Callable[[list[QueueItem]], None]) -> None:
        with self._lock:
            self._listeners = [entry for entry in self._listeners if entry != callback]

    def items(self) -> list[QueueItem]:
        with self._lock:
            return [QueueItem(**asdict(item)) for item in self._items]

    def add(self, url: str, title: str, options: dict[str, object]) -> str:
        queue_options = dict(options)
        audio_format = str(queue_options.get("audio_format") or "")
        if audio_format:
            queue_options["format_spec"] = _audio_format_spec(audio_format)
        item = QueueItem.create(url, title, queue_options)
        with self._lock:
            self._items.append(item)
            should_start = not self._start_explicitly_required
            self._persist_locked()
        self._emit()
        if should_start:
            self.start()
        return item.id

    def start(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._started = True
            self._start_explicitly_required = False
        self._pump()

    def pause(self, item_id: str) -> None:
        with self._lock:
            item = self._get(item_id)
            if not item or item.status not in {"running", "waiting"}:
                return
            item.status = "paused"
            item.updated_at = time.time()
            process = self._processes.get(item_id)
            if item_id in self._active:
                self._pause_requested.add(item_id)
            self._persist_locked()
        if process and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                pass
        self._emit()

    def resume(self, item_id: str) -> None:
        with self._lock:
            item = self._get(item_id)
            if not item or item.status != "paused":
                return
            item.status = "waiting"
            item.updated_at = time.time()
            self._started = True
            self._persist_locked()
        self._emit()
        self._pump()

    def cancel(self, item_id: str) -> None:
        with self._lock:
            item = self._get(item_id)
            if not item or item.status in {"completed", "completed_with_errors", "failed", "cancelled"}:
                return
            process = self._processes.get(item_id)
            if process:
                self._cancel_requested.add(item_id)
            else:
                item.status = "cancelled"
            if item_id in self._active:
                self._cancel_requested.add(item_id)
            item.updated_at = time.time()
            self._persist_locked()
        if process and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGCONT)
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                pass
        self._emit()
        self._pump()

    def retry(self, item_id: str) -> None:
        with self._lock:
            item = self._get(item_id)
            if not item or item.status not in {"failed", "interrupted", "cancelled"}:
                return
            item.status = "waiting"
            item.progress = 0.0
            item.entry_progress = 0.0
            item.progress_indeterminate = False
            item.playlist_index = None
            item.playlist_count = None
            item.stage = ""
            item.error = ""
            item.error_hint = ""
            item.error_action = "none"
            item.logged_errors = []
            item.logs = []
            item.attempt += 1
            item.updated_at = time.time()
            self._started = True
            self._persist_locked()
        self._emit()
        self._pump()

    def set_concurrency(self, value: int) -> None:
        with self._lock:
            self.concurrency = max(1, min(4, value))
        self._pump()

    def clear(self) -> None:
        """Remove every queued item and stop active downloads."""
        with self._lock:
            self._clear_locked()
        self._emit()

    def _clear_locked(self) -> None:
        for item in self._items:
            if item.id in self._active:
                self._cancel_requested.add(item.id)
            process = self._processes.get(item.id)
            if process and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGCONT)
                    os.killpg(process.pid, signal.SIGTERM)
                except OSError:
                    pass
        self._items.clear()
        self._start_explicitly_required = False
        self._persist_locked()

    def shutdown(self, clear_queue: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if clear_queue:
                self._clear_locked()
            else:
                for item in self._items:
                    if item.status in {"running", "paused"}:
                        item.status = "interrupted"
                        item.error = "Fetch closed while this download was active. Retry when you’re ready."
                        process = self._processes.get(item.id)
                        if process and process.poll() is None:
                            try:
                                os.killpg(process.pid, signal.SIGCONT)
                                os.killpg(process.pid, signal.SIGTERM)
                            except OSError:
                                pass
                self._persist_locked()
            self._listeners.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _load(self) -> None:
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(stored, list):
                return
            for value in stored:
                if not isinstance(value, dict):
                    continue
                try:
                    item = QueueItem(**value)
                except (TypeError, ValueError):
                    continue
                if item.status in {"running", "paused"}:
                    item.status = "interrupted"
                    item.error = "Fetch closed while this download was active. Retry when you’re ready."
                self._items.append(item)
            self._persist_locked()
        except (OSError, json.JSONDecodeError):
            pass

    def _persist_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        persisted = []
        for item in self._items:
            data = asdict(item)
            data["logs"] = item.logs[-50:]
            persisted.append(data)
        temporary.write_text(
            json.dumps(persisted, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)
        self._last_saved = time.monotonic()

    def _emit(self) -> None:
        with self._lock:
            listeners = list(self._listeners)
            snapshot = [QueueItem(**asdict(item)) for item in self._items]
        for listener in listeners:
            self.scheduler(listener, snapshot)

    def _pump(self) -> None:
        with self._lock:
            if not self._started or self._closed:
                return
            available = self.concurrency - len(self._active)
            pending = [
                item for item in self._items
                if item.status == "waiting" and item.id not in self._active
            ][:available]
            for item in pending:
                item.status = "running"
                item.error = ""
                item.updated_at = time.time()
                self._active[item.id] = self._executor.submit(self._run_item, item.id)
            if pending:
                self._persist_locked()
        if pending:
            self._emit()

    def _run_item(self, item_id: str) -> None:
        with self._lock:
            item = self._get(item_id)
            if not item:
                self._active.pop(item_id, None)
                self._cancel_requested.discard(item_id)
                should_pump = True
            else:
                should_pump = False
            if should_pump:
                options = {}
                url = ""
            elif item.status == "cancelled":
                self._active.pop(item_id, None)
                self._cancel_requested.discard(item_id)
                should_pump = True
                options = {}
                url = ""
            else:
                options = dict(item.options)
                url = item.url
        if should_pump:
            self._emit()
            self._pump()
            return
        def on_event(event: dict[str, object]) -> None:
            self._handle_event(item_id, event)

        def on_command(command: str) -> None:
            with self._lock:
                current = self._get(item_id)
                if current:
                    current.command = command
                    current.updated_at = time.time()
                    self._persist_locked()
            self._emit()

        def register(process: subprocess.Popen[str] | None) -> None:
            with self._lock:
                if process is None:
                    self._processes.pop(item_id, None)
                    return
                self._processes[item_id] = process
                item = self._get(item_id)
                if self._closed or item_id in self._cancel_requested:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except OSError:
                        pass
                elif item and item.status == "paused":
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except OSError:
                        pass
        try:
            code, command = download_url(
                url,
                self.destination(),
                on_event,
                register,
                on_command,
                format_spec=_audio_format_spec(str(options.get("audio_format")))
                if options.get("audio_format")
                else str(options.get("format_spec") or "bestvideo*+bestaudio/best"),
                audio_format=str(options["audio_format"]) if options.get("audio_format") else None,
                subtitle_languages=str(options.get("subtitle_languages") or ""),
                embed_subtitles=bool(options.get("embed_subtitles")),
                automatic_subtitles=bool(options.get("automatic_subtitles")),
                embed_chapters=bool(options.get("embed_chapters", True)),
                playlist_items=str(options.get("playlist_items") or ""),
                use_yt_dlp_config=bool(options.get("use_yt_dlp_config")),
                cookies_file=str(options.get("cookies_file") or ""),
                browser_cookies=str(options.get("browser_cookies") or ""),
                proxy=str(options.get("proxy") or ""),
                rate_limit=str(options.get("rate_limit") or ""),
                retry_count=int(options.get("retry_count") or 0),
                quality_preference=str(options.get("quality_preference") or "compatible"),
                max_resolution=str(options.get("max_resolution") or ""),
                is_playlist=bool(options.get("is_playlist")),
                archive_file=str(options.get("archive_file") or ""),
                sponsorblock=bool(options.get("sponsorblock")),
                embed_metadata=bool(options.get("embed_metadata", True)),
                embed_thumbnail=bool(options.get("embed_thumbnail", True)),
                include_media_id=bool(options.get("include_media_id", False)),
                custom_arguments=str(options.get("custom_arguments") or ""),
            )
            with self._lock:
                item = self._get(item_id)
                cancelled = item_id in self._cancel_requested
                pause_requested = item_id in self._pause_requested
                if item:
                    item.command = command
                    if self._closed:
                        item.status = "interrupted"
                    elif cancelled:
                        item.status = "cancelled"
                    elif code == 0:
                        item.progress = 100.0
                        item.entry_progress = 100.0
                        item.progress_indeterminate = False
                        item.speed = ""
                        item.eta = ""
                        if item.logged_errors:
                            item.status = "completed_with_errors"
                            item.stage = "Completed with errors"
                            details = "\n".join(item.logged_errors)
                            item.error = details
                            item.error_hint = (
                                "yt-dlp reported errors for one or more items. Other playlist items may have "
                                "downloaded; open Log to review the details."
                            )
                            item.logs.append("Playlist finished with errors.")
                        else:
                            item.status = "completed"
                            item.stage = "Completed"
                            item.logs.append("Download completed successfully.")
                    elif pause_requested and item.status in {"paused", "waiting"}:
                        item.error = ""
                    else:
                        item.status = "failed"
                        details = "\n".join(item.logged_errors or item.logs[-15:]) or f"yt-dlp exited with status {code}."
                        error_details = _friendly_error(details)
                        item.error = error_details.message
                        item.error_hint = error_details.hint
                        item.error_action = error_details.action
                    item.updated_at = time.time()
                self._cancel_requested.discard(item_id)
                self._pause_requested.discard(item_id)
                self._persist_locked()
        except EngineError as error:
            with self._lock:
                item = self._get(item_id)
                if item:
                    pause_requested = item_id in self._pause_requested
                    if not self._closed and pause_requested and item.status in {"paused", "waiting"}:
                        item.error = ""
                    else:
                        item.status = "interrupted" if self._closed else "failed"
                        item.error = error.details.message
                        item.error_hint = error.details.hint
                        item.error_action = error.details.action
                    item.updated_at = time.time()
                self._pause_requested.discard(item_id)
                self._persist_locked()
        except Exception as error:
            with self._lock:
                item = self._get(item_id)
                if item:
                    pause_requested = item_id in self._pause_requested
                    if not self._closed and pause_requested and item.status in {"paused", "waiting"}:
                        item.error = ""
                    else:
                        item.status = "interrupted" if self._closed else "failed"
                        item.error = f"Fetch couldn’t complete the download: {error}"
                        item.error_hint = ""
                        item.error_action = "none"
                    item.updated_at = time.time()
                self._pause_requested.discard(item_id)
                self._persist_locked()
        finally:
            with self._lock:
                self._active.pop(item_id, None)
            self._emit()
            self._pump()

    def _handle_event(self, item_id: str, event: dict[str, object]) -> None:
        with self._lock:
            item = self._get(item_id)
            if not item:
                return
            if event.get("type") == "done":
                item.output_path = str(event.get("output_path") or item.output_path)
                item.artist = str(event.get("artist") or "")
                item.media_title = str(event.get("title") or "")
                if item.media_title:
                    item.title = item.media_title
                item.album = str(event.get("album") or "")
                value = event.get("duration")
                item.duration = float(value) if isinstance(value, (int, float)) else None
            elif event.get("type") == "progress":
                title = event.get("title")
                if isinstance(title, str) and title:
                    item.title = title
                index = event.get("playlist_index")
                count = event.get("playlist_count")
                if isinstance(index, int) and index != item.playlist_index:
                    item.playlist_index = index
                    item.entry_progress = 0.0
                if isinstance(count, int):
                    item.playlist_count = count
                percent = event.get("percent")
                if isinstance(percent, (float, int)):
                    item.entry_progress = max(item.entry_progress, float(percent))
                    if item.playlist_index and item.playlist_count:
                        overall = ((item.playlist_index - 1) * 100 + item.entry_progress) / item.playlist_count
                    else:
                        overall = item.entry_progress
                    item.progress = max(item.progress, overall)
                    item.progress_indeterminate = False
                else:
                    item.progress_indeterminate = bool(event.get("indeterminate", True))
                item.stage = str(event.get("stage") or item.stage)
                item.speed = str(event.get("speed") or "")
                item.eta = str(event.get("eta") or "")
                message = str(event.get("message") or "")
                if message:
                    item.logs.append(message)
            else:
                message = str(event.get("message") or "")
                if message:
                    item.logs.append(message)
                    if _is_error_line(message):
                        item.logged_errors.append(message)
                        item.logged_errors = item.logged_errors[-50:]
            item.logs = item.logs[-500:]
            item.updated_at = time.time()
            if time.monotonic() - self._last_saved >= 3:
                self._persist_locked()
        self._emit()

    def _get(self, item_id: str) -> QueueItem | None:
        return next((item for item in self._items if item.id == item_id), None)
