from __future__ import annotations

import json
import os
from pathlib import Path

from .paths import config_dir, initial_download_dir


def should_check_yt_dlp_update(now: int, last_success: int, last_attempt: int) -> bool:
    if last_success and now - last_success < 24 * 60 * 60:
        return False
    return not last_attempt or now - last_attempt >= 60 * 60


def resolve_preset(name: str) -> dict[str, object]:
    if name.strip().lower() == "music":
        return {
            "preset": "Music",
            "audio_only": True,
            "audio_format": "m4a",
            "format_spec": "",
            "embed_metadata": True,
            "embed_thumbnail": True,
        }
    return {
        "preset": "Video",
        "audio_only": False,
        "audio_format": "",
        "format_spec": "bestvideo*+bestaudio/best",
        "embed_metadata": None,
        "embed_thumbnail": None,
    }


class Settings:
    """Small, immediately persisted set of user-facing preferences."""

    def __init__(self) -> None:
        self.path = config_dir() / "settings.json"
        self.values = {
            "settings_version": 2,
            "download_directory": str(initial_download_dir()),
            "color_scheme": "system",
            "settings_page": "",
            "queue_concurrency": 2,
            "quality_preference": "compatible",
            "max_resolution": "",
            "auto_check_yt_dlp": True,
            "yt_dlp_channel": "stable",
            "last_yt_dlp_check": 0,
            "last_yt_dlp_check_attempt": 0,
            "cookies_file": "",
            "use_cookies": False,
            "browser_cookies": "",
            "use_yt_dlp_config": False,
            "proxy": "",
            "rate_limit": "",
            "retry_count": 10,
            "download_archive": False,
            "sponsorblock": False,
            "embed_metadata": True,
            "embed_thumbnail": True,
            "include_media_id": False,
            "clear_queue_on_exit": True,
            "custom_arguments": "",
            "notifications_enabled": True,
            "clipboard_offer": False,
            "download_preset": "Music",
            "quick_add": True,
            "download_synced_lyrics": False,
        }
        self.load()

    def load(self) -> None:
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                settings_version = loaded.get("settings_version", 1)
                if not isinstance(settings_version, int):
                    settings_version = 1
                if isinstance(loaded.get("download_directory"), str):
                    self.values["download_directory"] = loaded["download_directory"]
                if loaded.get("color_scheme") in {"system", "light", "dark"}:
                    self.values["color_scheme"] = loaded["color_scheme"]
                if isinstance(loaded.get("settings_page"), str):
                    self.values["settings_page"] = loaded["settings_page"]
                if isinstance(loaded.get("download_preset"), str) and loaded["download_preset"] in {"Music", "Video"}:
                    self.values["download_preset"] = loaded["download_preset"]
                if isinstance(loaded.get("quick_add"), bool):
                    self.values["quick_add"] = loaded["quick_add"]
                elif self.values["download_preset"] == "Video":
                    self.values["quick_add"] = False
                if isinstance(loaded.get("queue_concurrency"), int):
                    self.values["queue_concurrency"] = max(1, min(4, loaded["queue_concurrency"]))
                if isinstance(loaded.get("quality_preference"), str) and loaded["quality_preference"] in {"compatible", "best"}:
                    self.values["quality_preference"] = loaded["quality_preference"]
                if isinstance(loaded.get("max_resolution"), str) and loaded["max_resolution"] in {"", "2160", "1440", "1080", "720", "480"}:
                    self.values["max_resolution"] = loaded["max_resolution"]
                if isinstance(loaded.get("auto_check_yt_dlp"), bool):
                    self.values["auto_check_yt_dlp"] = loaded["auto_check_yt_dlp"]
                if isinstance(loaded.get("yt_dlp_channel"), str) and loaded["yt_dlp_channel"] in {"stable", "nightly"}:
                    self.values["yt_dlp_channel"] = loaded["yt_dlp_channel"]
                if isinstance(loaded.get("last_yt_dlp_check"), (int, float)):
                    self.values["last_yt_dlp_check"] = int(loaded["last_yt_dlp_check"])
                if isinstance(loaded.get("last_yt_dlp_check_attempt"), (int, float)):
                    self.values["last_yt_dlp_check_attempt"] = int(loaded["last_yt_dlp_check_attempt"])
                for key in (
                    "use_cookies", "use_yt_dlp_config", "download_archive", "sponsorblock",
                    "embed_metadata", "embed_thumbnail", "include_media_id", "clear_queue_on_exit",
                    "notifications_enabled", "clipboard_offer",
                    "download_synced_lyrics",
                ):
                    if key == "embed_thumbnail" and settings_version < 2:
                        continue
                    if isinstance(loaded.get(key), bool):
                        self.values[key] = loaded[key]
                for key in ("cookies_file", "browser_cookies", "proxy", "rate_limit", "custom_arguments"):
                    if isinstance(loaded.get(key), str):
                        self.values[key] = loaded[key]
                if isinstance(loaded.get("retry_count"), int):
                    self.values["retry_count"] = max(0, min(100, loaded["retry_count"]))
                if settings_version < 2:
                    self.values["embed_thumbnail"] = True
                    self.save()
        except (OSError, json.JSONDecodeError):
            pass

    def set(self, key: str, value: str | int | bool) -> None:
        self.values[key] = value
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.values, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)

    @property
    def download_directory(self) -> Path:
        return Path(self.values["download_directory"]).expanduser()

    @property
    def color_scheme(self) -> str:
        return self.values["color_scheme"]

    @property
    def queue_concurrency(self) -> int:
        return int(self.values["queue_concurrency"])
