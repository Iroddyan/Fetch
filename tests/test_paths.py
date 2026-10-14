from pathlib import Path

from fetch import paths


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
