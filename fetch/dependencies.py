from __future__ import annotations

import hashlib
import json
import os
import re
import io
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
import platform

from . import APP_VERSION
from .paths import data_dir


GITHUB_RELEASES = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
DENO_RELEASES = "https://api.github.com/repos/denoland/deno/releases/latest"


def managed_yt_dlp_path() -> Path:
    return data_dir() / "bin" / "yt-dlp"


def managed_deno_path() -> Path:
    return data_dir() / "bin" / "deno"


def update_yt_dlp() -> tuple[str, bool]:
    """Install the latest official Linux-friendly zipimport release in user data."""
    release = _get_json(GITHUB_RELEASES)
    tag = str(release.get("tag_name") or "").removeprefix("v")
    if not tag:
        raise RuntimeError("GitHub did not return a yt-dlp release version.")

    assets = release.get("assets")
    if not isinstance(assets, list):
        raise RuntimeError("GitHub did not return the release files.")
    binary = next((asset for asset in assets if isinstance(asset, dict) and asset.get("name") == "yt-dlp"), None)
    checksums = next(
        (asset for asset in assets if isinstance(asset, dict) and asset.get("name") == "SHA2-256SUMS"),
        None,
    )
    if not binary or not binary.get("browser_download_url"):
        raise RuntimeError("The official release does not include the Linux yt-dlp executable.")

    path = managed_yt_dlp_path()
    current = _binary_version(path) if path.is_file() else None
    if current == tag:
        return tag, False

    expected = _release_digest(binary)
    if not expected and checksums and checksums.get("browser_download_url"):
        checksum_text = _get_bytes(str(checksums["browser_download_url"]), limit=2_000_000).decode("utf-8", "replace")
        expected = _checksum_for_file(checksum_text, "yt-dlp")
    if not expected:
        raise RuntimeError("Fetch couldn’t verify the release checksum, so it left yt-dlp unchanged.")

    payload = _get_bytes(str(binary["browser_download_url"]), limit=20_000_000)
    actual = hashlib.sha256(payload).hexdigest()
    if actual.lower() != expected.lower():
        raise RuntimeError("The downloaded yt-dlp file did not match its published SHA-256 checksum.")

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("yt-dlp.download")
    try:
        temporary.write_bytes(payload)
        os.chmod(temporary, 0o700)
        if _binary_version(temporary) != tag:
            raise RuntimeError("The verified release did not report the expected version.")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return tag, True


def update_deno() -> tuple[str, bool]:
    """Install a checksum-verified official Deno Linux runtime in user data."""
    architecture = platform.machine().lower()
    asset_name = {
        "x86_64": "deno-x86_64-unknown-linux-gnu.zip",
        "amd64": "deno-x86_64-unknown-linux-gnu.zip",
        "aarch64": "deno-aarch64-unknown-linux-gnu.zip",
        "arm64": "deno-aarch64-unknown-linux-gnu.zip",
    }.get(architecture)
    if not asset_name:
        raise RuntimeError(f"Fetch’s Deno installer doesn’t support this architecture yet: {architecture}.")
    release = _get_json(DENO_RELEASES)
    tag = str(release.get("tag_name") or "").removeprefix("v")
    assets = release.get("assets")
    if not tag or not isinstance(assets, list):
        raise RuntimeError("GitHub did not return a Deno release.")
    asset = next((item for item in assets if isinstance(item, dict) and item.get("name") == asset_name), None)
    checksum_asset = next(
        (item for item in assets if isinstance(item, dict) and item.get("name") == asset_name + ".sha256sum"),
        None,
    )
    if not asset or not asset.get("browser_download_url") or not checksum_asset or not checksum_asset.get("browser_download_url"):
        raise RuntimeError("The official Deno release is missing its runtime or checksum file.")
    path = managed_deno_path()
    current = _deno_version(path) if path.is_file() else None
    if current == tag:
        return tag, False
    checksum_text = _get_bytes(str(checksum_asset["browser_download_url"]), limit=4096).decode("utf-8", "replace")
    match = re.search(r"[0-9a-fA-F]{64}", checksum_text)
    if not match:
        raise RuntimeError("Fetch couldn’t read Deno’s published SHA-256 checksum.")
    payload = _get_bytes(str(asset["browser_download_url"]), limit=200_000_000)
    if hashlib.sha256(payload).hexdigest().lower() != match.group(0).lower():
        raise RuntimeError("The Deno download did not match its published SHA-256 checksum.")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            info = next((item for item in archive.infolist() if item.filename == "deno" and not item.is_dir()), None)
            if not info:
                raise RuntimeError("The official Deno archive did not contain the deno executable.")
            binary = archive.read(info)
    except zipfile.BadZipFile as error:
        raise RuntimeError("The Deno release archive was not a valid zip file.") from error

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("deno.download")
    try:
        temporary.write_bytes(binary)
        os.chmod(temporary, 0o700)
        if _deno_version(temporary) != tag:
            raise RuntimeError("The verified Deno release did not report the expected version.")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return tag, True


def _release_digest(asset: dict[str, object]) -> str | None:
    value = asset.get("digest")
    if isinstance(value, str) and value.startswith("sha256:"):
        return value.split(":", 1)[1]
    return None


def _checksum_for_file(payload: str, filename: str) -> str | None:
    escaped = re.escape(filename)
    match = re.search(rf"^([0-9a-fA-F]{{64}})\s+\*?{escaped}\s*$", payload, re.MULTILINE)
    return match.group(1) if match else None


def _get_json(url: str) -> dict[str, object]:
    payload = _get_bytes(url, limit=8_000_000, accept="application/vnd.github+json")
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("GitHub returned an unexpected release response.")
    return value


def _get_bytes(url: str, *, limit: int, accept: str = "application/octet-stream") -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": f"Fetch/{APP_VERSION}", "Accept": accept},
    )
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > limit:
                raise RuntimeError("The release file is larger than Fetch’s safety limit.")
            payload = response.read(limit + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise RuntimeError(f"Fetch couldn’t reach the official release service: {error}") from error
    if len(payload) > limit:
        raise RuntimeError("The release file is larger than Fetch’s safety limit.")
    return payload


def _binary_version(path: Path) -> str | None:
    import subprocess

    try:
        result = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=12, check=False)
        return (result.stdout or result.stderr).strip().splitlines()[0]
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return None


def _deno_version(path: Path) -> str | None:
    value = _binary_version(path)
    if not value:
        return None
    match = re.search(r"\bdeno\s+(\d+\.\d+\.\d+)\b", value)
    return match.group(1) if match else None
