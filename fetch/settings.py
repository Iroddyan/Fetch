from __future__ import annotations

import json
import os
from pathlib import Path

from .paths import config_dir, initial_download_dir


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
                if isinstance(loaded.get("queue_concurrency"), int):
                    self.values["queue_concurrency"] = max(1, min(4, loaded["queue_concurrency"]))
                for key in (
                    "use_cookies", "use_yt_dlp_config", "download_archive", "sponsorblock",
                    "embed_metadata", "embed_thumbnail", "include_media_id", "clear_queue_on_exit",
                    "notifications_enabled", "clipboard_offer",
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
