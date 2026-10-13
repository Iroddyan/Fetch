from __future__ import annotations

import hashlib
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


YT_DLP_BINARY_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"
YT_DLP_CHECKSUMS_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/SHA2-256SUMS"
DENO_LATEST_URL = "https://github.com/denoland/deno/releases/latest/download/{}"
YT_DLP_CHANNELS = {
    "stable": "yt-dlp/yt-dlp",
    "nightly": "yt-dlp/yt-dlp-nightly-builds",
}


def managed_yt_dlp_path() -> Path:
    return data_dir() / "bin" / "yt-dlp"


def previous_yt_dlp_path() -> Path:
    return managed_yt_dlp_path().with_name("yt-dlp.previous")


def managed_deno_path() -> Path:
    return data_dir() / "bin" / "deno"


def _yt_dlp_release_urls(channel: str) -> tuple[str, str]:
    repository = YT_DLP_CHANNELS.get(channel)
    if not repository:
        raise RuntimeError(f"Unknown yt-dlp update channel: {channel}")
    base = f"https://github.com/{repository}/releases/latest/download"
    return f"{base}/yt-dlp", f"{base}/SHA2-256SUMS"


def latest_yt_dlp_version(channel: str = "stable") -> str:
    binary_url, _checksums_url = _yt_dlp_release_urls(channel)
    return _latest_release_tag(binary_url)


def update_yt_dlp(channel: str = "stable") -> tuple[str, bool]:
    """Install the latest official Linux-friendly zipimport release in user data."""
    binary_url, checksums_url = _yt_dlp_release_urls(channel)
    tag = _latest_release_tag(binary_url)

    path = managed_yt_dlp_path()
    current = _binary_version(path) if path.is_file() else None
    if current == tag:
        return tag, False

    checksum_text = _get_bytes(checksums_url, limit=2_000_000).decode("utf-8", "replace")
    expected = _checksum_for_file(checksum_text, "yt-dlp")
    if not expected:
        raise RuntimeError("Fetch couldn’t verify the release checksum, so it left yt-dlp unchanged.")

    payload = _get_bytes(binary_url, limit=20_000_000)
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
        previous = previous_yt_dlp_path()
        if path.is_file():
            backup_temporary = previous.with_name(previous.name + ".download")
            try:
                backup_temporary.write_bytes(path.read_bytes())
                os.chmod(backup_temporary, 0o700)
                backup_temporary.replace(previous)
            finally:
                backup_temporary.unlink(missing_ok=True)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return tag, True


def rollback_yt_dlp() -> str:
    path = managed_yt_dlp_path()
    previous = previous_yt_dlp_path()
    if not previous.is_file():
        raise RuntimeError("There isn’t a previous yt-dlp version to restore.")
    version = _binary_version(previous)
    if not version:
        raise RuntimeError("The saved yt-dlp version couldn’t be started, so Fetch left the current version unchanged.")
    current_temporary = previous.with_name(previous.name + ".current")
    try:
        if path.is_file():
            current_temporary.write_bytes(path.read_bytes())
            os.chmod(current_temporary, 0o700)
        previous.replace(path)
        if current_temporary.is_file():
            current_temporary.replace(previous)
    except OSError as error:
        if current_temporary.is_file() and not path.exists():
            current_temporary.replace(path)
        raise RuntimeError(f"Fetch couldn’t roll back yt-dlp: {error}") from error
    finally:
        current_temporary.unlink(missing_ok=True)
    return version


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
    asset_url = DENO_LATEST_URL.format(asset_name)
    checksum_url = DENO_LATEST_URL.format(asset_name + ".sha256sum")
    tag = _latest_release_tag(asset_url)
    path = managed_deno_path()
    current = _deno_version(path) if path.is_file() else None
    if current == tag:
        return tag, False
    checksum_text = _get_bytes(checksum_url, limit=4096).decode("utf-8", "replace")
    match = re.search(r"[0-9a-fA-F]{64}", checksum_text)
    if not match:
        raise RuntimeError("Fetch couldn’t read Deno’s published SHA-256 checksum.")
    payload = _get_bytes(asset_url, limit=200_000_000)
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


def _checksum_for_file(payload: str, filename: str) -> str | None:
    escaped = re.escape(filename)
    match = re.search(rf"^([0-9a-fA-F]{{64}})\s+\*?{escaped}\s*$", payload, re.MULTILINE)
    return match.group(1) if match else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _latest_release_tag(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": f"Fetch/{APP_VERSION}"},
        method="HEAD",
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        response = opener.open(request, timeout=40)
    except urllib.error.HTTPError as response:
        location = response.headers.get("Location", "")
        if response.code not in {301, 302, 303, 307, 308} or not location:
            raise RuntimeError(f"Fetch couldn’t determine the latest release from {url}.") from response
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise RuntimeError(f"Fetch couldn’t reach the official release service: {error}") from error
    else:
        response.close()
        raise RuntimeError(f"The official release link did not redirect to a versioned asset: {url}.")
    match = re.search(r"/releases/download/([^/]+)/", location)
    if not match:
        raise RuntimeError("The official release redirect did not contain a version tag.")
    return match.group(1).removeprefix("v")


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
