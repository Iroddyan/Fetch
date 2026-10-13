# Fetch

Fetch is a native Linux desktop frontend for [yt-dlp](https://github.com/yt-dlp/yt-dlp). It keeps the common path simple—inspect a link, choose a format, and download—while leaving yt-dlp as an independently updated subprocess.

## Release

Fetch **1.0.0** completes Bits 0–15 of the project roadmap. The first release targets Linux, with Fedora GNOME and Wayland as its reference desktop. The application ID is `com.iroddyan.fetch`.

## Features

- GTK 4 and libadwaita interface with searchable, persistent settings pages.
- URL and playlist inspection, thumbnails, media details, format selection, and playlist item selection.
- Best-quality and audio-only downloads, subtitles, chapters, metadata, thumbnail embedding, and SponsorBlock.
- Persistent queue with concurrency limits, structured progress, speed and ETA, pause/resume, cancel, retry, and recovery of interrupted work.
- Direct, checksum-verified updates for the official yt-dlp and Deno Linux builds, independent of app updates. System-provided tools remain supported.
- Dependency status for yt-dlp, ffmpeg, ffprobe, and Deno, with yt-dlp errors and command logs available from the queue.
- Cookies.txt import and browser-cookie selection, optional yt-dlp configuration, download archive, proxy and speed limits, retries, and raw yt-dlp arguments.
- Optional desktop notifications and clipboard link offers. Clipboard monitoring is off by default and never starts downloads automatically.
- Flatpak manifest and Fedora RPM spec for distribution.

## Requirements

For running from a checkout: Python 3, PyGObject, GTK 4, libadwaita, and yt-dlp. ffmpeg and ffprobe are needed to merge separate audio/video streams. Deno 2.3 or later is recommended for full YouTube support; the official Fetch-managed yt-dlp build includes yt-dlp-ejs scripts.

Run from the checkout with:

```sh
./run-fetch
```

On Fedora, the Python runtime must be able to import `gi`, and GTK 4 and libadwaita introspection data must be installed.

## Engine updates

Open **Settings → Engine** to see installed versions or update yt-dlp and Deno. Fetch downloads the official Linux release, verifies its published SHA-256 checksum, checks the executable version, then installs it in the current user's data directory. It does not replace a system package. When present, a Fetch-managed binary takes priority over the system binary; ffmpeg and ffprobe are detected from the system `PATH`.

Fetch uses Deno because current yt-dlp YouTube extraction can require a JavaScript runtime. The officially managed yt-dlp release is the project's Linux zipimport build, which includes its EJS support files. Third-party yt-dlp builds can package these dependencies differently.

## Settings and files

Settings apply immediately and are saved under the user's XDG directories:

- `${XDG_CONFIG_HOME:-~/.config}/com.iroddyan.fetch/` — settings and imported cookies.txt (user-only permissions).
- `${XDG_DATA_HOME:-~/.local/share}/com.iroddyan.fetch/bin/` — Fetch-managed yt-dlp and Deno.
- `${XDG_CACHE_HOME:-~/.cache}/com.iroddyan.fetch/` — cached thumbnails.
- `${XDG_STATE_HOME:-~/.local/state}/com.iroddyan.fetch/` — queue state, recent logs, and the optional yt-dlp download archive.

The initial download location is the system Downloads directory, falling back to `~/Downloads`. Browser-cookie access can require extra filesystem permissions in a Flatpak; importing a cookies.txt file is the portable option.

## Packaging

The Flatpak manifest is `flatpak/com.iroddyan.fetch.yml`. It uses GNOME Platform and the freedesktop ffmpeg extension. The Fedora RPM spec is `packaging/fetch.spec`. The app currently requires network access for media inspection, downloads, and engine updates.

Build a Fedora RPM from the checkout with `packaging/build-rpm.sh`; the package is written under `~/rpmbuild/RPMS/noarch/` by default. To build and install the Flatpak locally, use `flatpak-builder --user --install --force-clean build-dir flatpak/com.iroddyan.fetch.yml` after installing Flatpak Builder and enabling the GNOME and freedesktop runtimes.

## About

- **App:** Fetch 1.0.0
- **Application ID:** `com.iroddyan.fetch`
- **Creator and maintainer:** Iroddyan Waykins
- **Development collaborator:** Codex (AI)
- **License:** Not specified
- **Platform:** Linux desktop

## Current limits

- Playlist inspection lists up to 500 entries at once. Use the playlist range controls to select a range beyond the visible list.
- Exact format selection is available for individually inspected media. Playlist downloads use best quality or audio-only settings because a playlist inspection does not retrieve every item's complete format list.
- Pause/resume uses Linux process-group signals. If Fetch closes during a download, it is recorded as interrupted and can be retried after reopening.
- Flatpak can read browser profiles only when the user grants the required access; the imported cookies.txt flow avoids that dependency.
