from __future__ import annotations

import os
import re
from pathlib import Path

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
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    user_dirs = config_home / "user-dirs.dirs"
    try:
        content = user_dirs.read_text(encoding="utf-8")
    except OSError:
        content = ""
    match = re.search(r'^\s*XDG_DOWNLOAD_DIR\s*=\s*(.*?)\s*$', content, flags=re.MULTILINE)
    if match:
        value = match.group(1).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        value = value.replace("${HOME}", str(Path.home())).replace("$HOME", str(Path.home()))
        download_dir = Path(value).expanduser()
        if download_dir.is_dir():
            return download_dir
    return Path.home() / "Downloads"
