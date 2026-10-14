import json
from pathlib import Path

from fetch import queue
from fetch.queue import QueueItem, QueueManager, _is_error_line


def test_is_error_line_matches_yt_dlp_errors_only() -> None:
    assert _is_error_line("ERROR: Postprocessing failed")
    assert _is_error_line("  error: HTTP Error 403")
    assert not _is_error_line("[download] Finished downloading playlist")
    assert not _is_error_line("Downloading…")


def test_playlist_with_logged_error_is_not_marked_fully_completed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(queue, "state_dir", lambda: tmp_path)

    def download_url(_url, _destination, output, _register, command_ready, **_options):
        command_ready("yt-dlp --example")
        output({"type": "log", "message": "ERROR: Postprocessing: Unable to embed thumbnail"})
        return 0, "yt-dlp --example"

    monkeypatch.setattr(queue, "download_url", download_url)
    manager = QueueManager(lambda: tmp_path)
    item = QueueItem.create("https://example.test/playlist", "Example playlist", {"is_playlist": True})
    manager._items.append(item)

    manager._run_item(item.id)

    result = manager.items()[0]
    assert result.status == "completed_with_errors"
    assert result.progress == 100.0
    assert "Unable to embed thumbnail" in result.error
    assert result.error_hint
    manager.shutdown(clear_queue=True)


def test_audio_queue_options_use_engine_audio_selector(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(queue, "state_dir", lambda: tmp_path)
    manager = QueueManager(lambda: tmp_path, scheduler=lambda callback, *args: callback(*args))
    monkeypatch.setattr(manager, "start", lambda: None)

    manager.add("https://example.test/audio", "Audio", {"audio_format": "m4a", "format_spec": "wrong"})

    assert manager.items()[0].options["format_spec"] == "bestaudio[ext=m4a]/bestaudio/best"


def test_queue_stores_and_persists_finished_file_metadata(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(queue, "state_dir", lambda: tmp_path)
    manager = QueueManager(destination=lambda: tmp_path, scheduler=lambda *_args: None)
    item_id = manager.add("https://example.test/video", "Queue label", {"format_spec": "best"})
    manager._handle_event(item_id, {
        "type": "done", "output_path": "/tmp/Música/My Song.m4a", "artist": "Beyoncé",
        "title": "Song", "album": "Álbum", "duration": 211.0,
    })
    manager.shutdown(clear_queue=False)
    stored = json.loads((tmp_path / "queue.json").read_text(encoding="utf-8"))[0]
    assert stored["output_path"] == "/tmp/Música/My Song.m4a"
    assert stored["artist"] == "Beyoncé"
    assert stored["media_title"] == "Song"
    assert stored["album"] == "Álbum"
    assert stored["duration"] == 211.0


def test_queue_replaces_url_label_when_progress_has_real_title(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(queue, "state_dir", lambda: tmp_path)
    manager = QueueManager(destination=lambda: tmp_path, scheduler=lambda *_args: None)
    item_id = manager.add("https://example.test/video", "https://example.test/video", {})
    manager._handle_event(item_id, {
        "type": "progress", "title": "A real title", "percent": 15.0,
        "indeterminate": False, "playlist_index": 1, "playlist_count": 1,
    })
    assert manager.items()[0].title == "A real title"
    manager.shutdown(clear_queue=True)
    manager.shutdown()
