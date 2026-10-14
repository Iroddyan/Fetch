from __future__ import annotations

import json
import signal
import threading
import time
from pathlib import Path

from fetch import queue
from fetch.queue import QueueItem, QueueManager


class FakeProcess:
    pid = 4321

    def poll(self):
        return None


def make_manager(monkeypatch, tmp_path: Path) -> QueueManager:
    monkeypatch.setattr(queue, "state_dir", lambda: tmp_path)
    return QueueManager(lambda: tmp_path, scheduler=lambda callback, *args: callback(*args))


def wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "condition did not become true before timeout"


def test_pause_terminates_running_item_and_keeps_part_state(monkeypatch, tmp_path: Path) -> None:
    manager = make_manager(monkeypatch, tmp_path)
    item = QueueItem.create("https://example.test/video", "Video", {})
    item.status = "running"
    item.error = ""
    manager._items.append(item)
    process = FakeProcess()
    manager._processes[item.id] = process
    manager._active[item.id] = object()
    part = tmp_path / "video.m4a.part"
    part.write_bytes(b"partial download")
    signals = []
    monkeypatch.setattr(queue.os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    manager.pause(item.id)

    result = manager.items()[0]
    assert result.status == "paused"
    assert result.error == ""
    assert signals == [(process.pid, signal.SIGTERM)]
    assert part.read_bytes() == b"partial download"
    manager.shutdown()


def test_resume_relaunches_and_completes(monkeypatch, tmp_path: Path) -> None:
    manager = make_manager(monkeypatch, tmp_path)
    calls = []

    def fake_download(*args, **kwargs):
        calls.append(1)
        args[2]({"type": "log", "message": "resumed"})
        return 0, "yt-dlp --example"

    monkeypatch.setattr(queue, "download_url", fake_download)
    item = QueueItem.create("https://example.test/video", "Video", {})
    item.status = "paused"
    manager._items.append(item)
    manager.resume(item.id)
    wait_for(lambda: manager.items()[0].status == "completed")

    assert len(calls) == 1
    manager.shutdown()


def test_pause_then_quick_resume_never_overlaps_runs(monkeypatch, tmp_path: Path) -> None:
    manager = make_manager(monkeypatch, tmp_path)
    first_started = threading.Event()
    release_first = threading.Event()
    lock = threading.Lock()
    running = 0
    max_running = 0
    calls = 0
    process = FakeProcess()

    def fake_download(_url, _destination, _output, register, _command_ready, **_options):
        nonlocal running, max_running, calls
        with lock:
            calls += 1
            this_call = calls
            running += 1
            max_running = max(max_running, running)
        register(process)
        try:
            if this_call == 1:
                first_started.set()
                release_first.wait(2)
                return -signal.SIGTERM, "yt-dlp --example"
            return 0, "yt-dlp --example"
        finally:
            with lock:
                running -= 1

    monkeypatch.setattr(queue, "download_url", fake_download)
    monkeypatch.setattr(queue.os, "killpg", lambda *_args: None)
    item_id = manager.add("https://example.test/video", "Video", {})
    assert first_started.wait(2)
    manager.pause(item_id)
    manager.resume(item_id)
    time.sleep(0.05)
    release_first.set()
    wait_for(lambda: manager.items()[0].status == "completed")

    assert calls == 2
    assert max_running == 1
    manager.shutdown()


def test_cancel_running_and_waiting_items(monkeypatch, tmp_path: Path) -> None:
    manager = make_manager(monkeypatch, tmp_path)
    running = QueueItem.create("https://example.test/a", "A", {})
    running.status = "running"
    waiting = QueueItem.create("https://example.test/b", "B", {})
    manager._items.extend((running, waiting))
    manager._processes[running.id] = FakeProcess()
    signals = []
    monkeypatch.setattr(queue.os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    manager.cancel(running.id)
    manager.cancel(waiting.id)

    states = {item.id: item.status for item in manager.items()}
    assert states[running.id] == "running"
    assert states[waiting.id] == "cancelled"
    assert manager._cancel_requested == {running.id}
    assert signals == [(4321, signal.SIGCONT), (4321, signal.SIGTERM)]
    manager.shutdown()


def test_retry_resets_progress_logs_errors_and_increments_attempt(monkeypatch, tmp_path: Path) -> None:
    manager = make_manager(monkeypatch, tmp_path)
    monkeypatch.setattr(queue, "download_url", lambda *_args, **_kwargs: (0, "yt-dlp --example"))
    item = QueueItem.create("https://example.test/video", "Video", {})
    item.status = "failed"
    item.progress = item.entry_progress = 82.0
    item.progress_indeterminate = True
    item.playlist_index = 4
    item.playlist_count = 10
    item.stage = "Audio"
    item.speed = "1 MB/s"
    item.eta = "5 seconds"
    item.attempt = 2
    item.error = "old error"
    item.error_hint = "old hint"
    item.error_action = "update_ytdlp"
    item.logged_errors = ["ERROR: old"]
    item.logs = ["old log"]
    manager._items.append(item)

    manager.retry(item.id)
    wait_for(lambda: manager.items()[0].status == "completed")
    result = manager.items()[0]
    assert result.attempt == 3
    assert result.progress == result.entry_progress == 100.0
    assert not result.progress_indeterminate
    assert result.playlist_index is None and result.playlist_count is None
    assert result.error == result.error_hint == ""
    assert result.error_action == "none"
    assert result.logged_errors == []
    assert result.logs == ["Download completed successfully."]
    manager.shutdown()


def test_progress_never_decreases_across_playlist_items_and_stages(monkeypatch, tmp_path: Path) -> None:
    manager = make_manager(monkeypatch, tmp_path)
    item = QueueItem.create("https://example.test/list", "Playlist", {})
    item.status = "running"
    manager._items.append(item)
    events = [
        {"type": "progress", "playlist_index": 2, "playlist_count": 5, "percent": 80, "stage": "Video"},
        {"type": "progress", "playlist_index": 2, "playlist_count": 5, "percent": 12, "stage": "Audio"},
        {"type": "progress", "playlist_index": 3, "playlist_count": 5, "percent": 5, "stage": "Video"},
    ]
    progress = []
    for event in events:
        manager._handle_event(item.id, event)
        progress.append(manager.items()[0].progress)
    assert progress == sorted(progress)
    assert progress == [36.0, 36.0, 41.0]
    manager.shutdown()


def test_persisted_queue_keeps_only_last_50_logs(monkeypatch, tmp_path: Path) -> None:
    manager = make_manager(monkeypatch, tmp_path)
    item = QueueItem.create("https://example.test/video", "Video", {})
    item.logs = [f"line {index}" for index in range(70)]
    manager._items.append(item)
    manager._persist_locked()

    stored = json.loads((tmp_path / "queue.json").read_text(encoding="utf-8"))[0]["logs"]
    assert len(stored) == 50
    assert stored[0] == "line 20" and stored[-1] == "line 69"
    manager.shutdown()


def test_shutdown_preserves_queue_and_marks_active_items_interrupted(monkeypatch, tmp_path: Path) -> None:
    manager = make_manager(monkeypatch, tmp_path)
    running = QueueItem.create("https://example.test/a", "A", {})
    running.status = "running"
    paused = QueueItem.create("https://example.test/b", "B", {})
    paused.status = "paused"
    manager._items.extend((running, paused))

    manager.shutdown(clear_queue=False)

    assert [item.status for item in manager.items()] == ["interrupted", "interrupted"]


def test_nonzero_exit_sets_failed_and_mapped_error_action(monkeypatch, tmp_path: Path) -> None:
    manager = make_manager(monkeypatch, tmp_path)

    def fake_download(_url, _destination, output, _register, _command_ready, **_options):
        output({"type": "log", "message": "ERROR: HTTP Error 403: Forbidden"})
        return 1, "yt-dlp --example"

    monkeypatch.setattr(queue, "download_url", fake_download)
    item = QueueItem.create("https://example.test/video", "Video", {})
    manager._items.append(item)
    manager._run_item(item.id)

    result = manager.items()[0]
    assert result.status == "failed"
    assert result.error_action == "update_ytdlp"
    assert "HTTP Error 403" in result.error
    manager.shutdown()
