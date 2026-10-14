from pathlib import Path

from fetch import paths
from fetch.settings import Settings, resolve_preset, should_check_yt_dlp_update


def test_xdg_application_directories_keep_application_id(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    config = tmp_path / "config"
    data = tmp_path / "data"
    cache = tmp_path / "cache"
    state = tmp_path / "state"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))

    assert paths.config_dir() == config / "com.iroddyan.fetch"
    assert paths.data_dir() == data / "com.iroddyan.fetch"
    assert paths.cache_dir() == cache / "com.iroddyan.fetch"
    assert paths.state_dir() == state / "com.iroddyan.fetch"


def test_xdg_defaults_match_existing_locations(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        monkeypatch.delenv(key, raising=False)

    assert paths.config_dir() == home / ".config/com.iroddyan.fetch"
    assert paths.data_dir() == home / ".local/share/com.iroddyan.fetch"
    assert paths.cache_dir() == home / ".cache/com.iroddyan.fetch"
    assert paths.state_dir() == home / ".local/state/com.iroddyan.fetch"


def test_initial_download_dir_expands_home_and_requires_existing_folder(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    config = tmp_path / "config"
    downloads = home / "My Downloads"
    downloads.mkdir(parents=True)
    config.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    (config / "user-dirs.dirs").write_text('XDG_DOWNLOAD_DIR="$HOME/My Downloads"\n', encoding="utf-8")

    assert paths.initial_download_dir() == downloads

    (config / "user-dirs.dirs").write_text('XDG_DOWNLOAD_DIR="$HOME/missing"\n', encoding="utf-8")
    assert paths.initial_download_dir() == home / "Downloads"


def test_update_check_schedule_retries_failures_hourly_and_success_daily() -> None:
    assert should_check_yt_dlp_update(100_000, 0, 0)
    assert not should_check_yt_dlp_update(100_000, 90_000, 0)
    assert not should_check_yt_dlp_update(100_000, 0, 99_000)
    assert should_check_yt_dlp_update(103_000, 0, 99_000)


def test_settings_load_validates_update_attempt_timestamp(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    settings = Settings()
    settings.path.parent.mkdir(parents=True)
    settings.path.write_text(
        '{"last_yt_dlp_check": 1234, "last_yt_dlp_check_attempt": "invalid"}',
        encoding="utf-8",
    )
    loaded = Settings()
    assert loaded.values["last_yt_dlp_check"] == 1234
    assert loaded.values["last_yt_dlp_check_attempt"] == 0

    settings.path.write_text(
        '{"last_yt_dlp_check_attempt": 5678}',
        encoding="utf-8",
    )
    assert Settings().values["last_yt_dlp_check_attempt"] == 5678


def test_download_preset_defaults_to_music_and_validates_existing_values(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("fetch.settings.config_dir", lambda: tmp_path)
    monkeypatch.setattr("fetch.settings.initial_download_dir", lambda: tmp_path)
    assert Settings().values["download_preset"] == "Music"
    settings = Settings()
    settings.path.write_text('{"download_preset":"Video"}', encoding="utf-8")
    assert Settings().values["download_preset"] == "Video"
    assert Settings().values["quick_add"] is False
    settings.path.write_text('{"download_preset":"Not a preset"}', encoding="utf-8")
    assert Settings().values["download_preset"] == "Music"
    assert Settings().values["quick_add"] is True


def test_preset_resolution_returns_builtin_queue_options() -> None:
    assert resolve_preset("Music") == {
        "preset": "Music", "audio_only": True, "audio_format": "m4a", "format_spec": "",
        "embed_metadata": True, "embed_thumbnail": True,
    }
    assert resolve_preset("Video") == {
        "preset": "Video", "audio_only": False, "audio_format": "",
        "format_spec": "bestvideo*+bestaudio/best", "embed_metadata": None,
        "embed_thumbnail": None,
    }
