from __future__ import annotations

import os
from pathlib import Path

from gi.repository import GLib

from . import APP_ID


def _xdg_path(variable: str, fallback: str) -> Path:
    value = os.environ.get(variable)
    return Path(value).expanduser() if value else Path.home() / fallback


def config_dir() -> Path:
    return _xdg_path("XDG_CONFIG_HOME", ".config") / APP_ID


def cache_dir() -> Path:
    return _xdg_path("XDG_CACHE_HOME", ".cache") / APP_ID


def data_dir() -> Path:
    return _xdg_path("XDG_DATA_HOME", ".local/share") / APP_ID


def state_dir() -> Path:
    return _xdg_path("XDG_STATE_HOME", ".local/state") / APP_ID


def initial_download_dir() -> Path:
    download_dir = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
    if download_dir:
        return Path(download_dir).expanduser()
    return Path.home() / "Downloads"
