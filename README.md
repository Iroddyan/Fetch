# Fetch

Fetch is a native Linux desktop frontend for [yt-dlp](https://github.com/yt-dlp/yt-dlp). It keeps the common path simple—inspect a link, choose a format, and download—while leaving yt-dlp as an independently updated subprocess.

## Release

Fetch **1.2.0** builds on the Bits 0–15 v1.0 roadmap release. The app targets Linux, with Fedora GNOME and Wayland as its reference desktop. The application ID is `com.iroddyan.fetch`.

## Features

- GTK 4 and libadwaita interface with searchable, persistent settings pages.
- URL and playlist inspection, thumbnails, media details, format selection, and playlist item selection.
- Best-quality and audio-only downloads, subtitles, chapters, metadata, thumbnail embedding, and SponsorBlock.
- Queue with concurrency limits, structured progress, speed and ETA, pause/resume from partial files, cancel, retry, and a Clear Queue action. Pause stops yt-dlp; resume relaunches it so it can continue from partial files. By default, the queue is cleared when Fetch closes; this can be changed in Settings → Downloads.
- Music downloads default to M4A, embedded square artwork, and `Artist - Title` filenames without the media ID. Playlist music uses the same filenames as standalone tracks. Settings → Media lets you change artwork embedding or include IDs.
- Direct, checksum-verified updates for the official yt-dlp and Deno Linux builds, independent of app updates. System-provided tools remain supported.
- Dependency status for yt-dlp, ffmpeg, ffprobe, and Deno, with yt-dlp errors and command logs available from the queue.
- Cookies.txt import and browser-cookie selection, optional yt-dlp configuration, download archive, proxy and speed limits, retries, and raw yt-dlp arguments.
- Optional desktop notifications and clipboard link offers. Clipboard monitoring is off by default and never starts downloads automatically.
- Flatpak manifest and Fedora RPM spec for distribution.

Tests use only the Python standard library plus pytest. In a Python environment, install pytest with `python -m pip install pytest`, then run `python -m pytest`.

## Requirements

For running from a checkout: Python 3, PyGObject, GTK 4.8 or later, libadwaita 1.4 or later, and yt-dlp. GTK 4.8 is required for thumbnail fitting, and libadwaita 1.4 is required for settings navigation pages. ffmpeg and ffprobe are needed to merge separate audio/video streams. Deno 2.3 or later is recommended for full YouTube support; the official Fetch-managed yt-dlp build includes yt-dlp-ejs scripts.

Run from the checkout with:

```sh
./run-fetch
```

On Fedora, the Python runtime must be able to import `gi`, and GTK 4 and libadwaita introspection data must be installed.

## Engine updates

Open **Settings → Engine** to see installed versions or update yt-dlp and Deno. Dependency versions load in the background so Settings can open immediately. Fetch downloads the official Linux release, verifies its published SHA-256 checksum, checks the executable version, then installs it in the current user's data directory. It does not replace a system package. When present, a Fetch-managed binary takes priority over the system binary; ffmpeg and ffprobe are detected from the system `PATH`.

Fetch uses Deno because current yt-dlp YouTube extraction can require a JavaScript runtime. The officially managed yt-dlp release is the project's Linux zipimport build, which includes its EJS support files. Third-party yt-dlp builds can package these dependencies differently.

## Settings and files

Settings apply immediately and are saved under the user's XDG directories:

- `${XDG_CONFIG_HOME:-~/.config}/com.iroddyan.fetch/` — settings and imported cookies.txt (user-only permissions).
- `${XDG_DATA_HOME:-~/.local/share}/com.iroddyan.fetch/bin/` — Fetch-managed yt-dlp and Deno.
- `${XDG_CACHE_HOME:-~/.cache}/com.iroddyan.fetch/` — cached thumbnails.
- `${XDG_STATE_HOME:-~/.local/state}/com.iroddyan.fetch/` — queue state, recent logs, and the optional yt-dlp download archive.

The initial download location is the system Downloads directory, falling back to `~/Downloads`. Browser-cookie access can require extra filesystem permissions in a Flatpak; importing a cookies.txt file is the portable option.

## Install, update, and uninstall

Fetch can be installed as a locally built Flatpak, a user-level native install, or as a Fedora RPM. The Flatpak is built from this checkout; no Flathub listing is required. The RPM is intended for Fedora and compatible RPM systems that provide the dependencies listed in `packaging/fetch.spec`.

### Which install should I use?

- **Flatpak** works on any Linux distribution with Flatpak, including older distributions. It bundles the GTK runtime Fetch needs.
- **User-level native install** with `install.sh` works on distributions that provide GTK 4.8+, libadwaita 1.4+, and PyGObject on the host.
- **RPM** is for Fedora and compatible RPM-based systems.
- Debian 12 provides libadwaita 1.2.2, and Ubuntu 22.04 provides 1.1.7 through its updates repository; both are older than Fetch’s 1.4 minimum. Use the Flatpak on these releases ([Debian package](https://packages.debian.org/bookworm/libadwaita-1-dev), [Ubuntu updates package](https://packages.ubuntu.com/jammy-updates/libadwaita-1-dev)).

### User-level native install

From the Fetch checkout, install under `~/.local` without administrator access:

```sh
./install.sh
```

Launch Fetch from the application menu or run `~/.local/bin/fetch`. To remove this user-level installation:

```sh
./uninstall.sh
```

These scripts remove or replace only Fetch’s application files; your settings and downloaded media remain in place.

### Flatpak

Install Flatpak and Flatpak Builder with your distribution’s package manager, then make sure the Flathub remote is configured. From the Fetch checkout, install the GNOME 50 runtime and SDK and build/install Fetch for your user:

```sh
flatpak install --user flathub org.gnome.Platform//50 org.gnome.Sdk//50
fetch_build_dir="$HOME/.cache/fetch-flatpak"
flatpak-builder --user --install --force-clean \
  --state-dir="$fetch_build_dir/state" \
  "$fetch_build_dir/build" flatpak/com.iroddyan.fetch.yml
```

The first build may take a while because Flatpak Builder prepares the runtime and SDK. Launch Fetch from your desktop’s application menu, or run:

```sh
flatpak run com.iroddyan.fetch
```

To update Fetch itself, use the updated checkout and rerun the build/install command. Flatpak Builder rebuilds the application and updates the installed copy:

```sh
fetch_build_dir="$HOME/.cache/fetch-flatpak"
flatpak-builder --user --install --force-clean \
  --state-dir="$fetch_build_dir/state" \
  "$fetch_build_dir/build" flatpak/com.iroddyan.fetch.yml
```

To remove the application while keeping its private settings and managed yt-dlp/Deno files for a later reinstall:

```sh
flatpak uninstall --user com.iroddyan.fetch
```

To also erase Fetch’s Flatpak data, including settings, imported cookies, and managed engine binaries, use:

```sh
flatpak uninstall --user --delete-data com.iroddyan.fetch
```

This does not automatically remove shared GNOME/Freedesktop runtimes, since other Flatpak applications may use them.

### Fedora RPM

Install the RPM build tools, then build the package from the Fetch checkout:

```sh
sudo dnf install rpm-build
./packaging/build-rpm.sh
```

The script writes the package to `~/rpmbuild/RPMS/noarch/`. For this release, install it with:

```sh
sudo dnf install ~/rpmbuild/RPMS/noarch/fetch-1.2.0-1*.noarch.rpm
```

DNF installs the runtime requirements declared by the RPM from your enabled repositories. Launch Fetch from the application menu or with `fetch`.

To update Fetch itself, build the RPM from the updated checkout with `./packaging/build-rpm.sh`, then install the newly built RPM. DNF replaces the installed version and keeps your per-user data:

```sh
sudo dnf install "$HOME/rpmbuild/RPMS/noarch/REPLACE_WITH_THE_NEW_RPM_FILENAME"
```

Replace the filename with the new versioned RPM written by the build script. If the RPM filename changed, DNF may show it as an install/upgrade transaction; review and confirm the transaction to apply it.

To uninstall the RPM:

```sh
sudo dnf remove fetch
```

Removing the RPM leaves your per-user Fetch settings and data in place. They are stored in the XDG directories listed in [Settings and files](#settings-and-files); remove those folders manually only if you also want to erase your preferences, cookies, queue state, and managed yt-dlp/Deno binaries.

The app requires network access for media inspection, downloads, and engine updates.

Fetch app releases are separate from yt-dlp updates. To update the managed yt-dlp engine without rebuilding Fetch, use **Settings → Engine**; see [Engine updates](#engine-updates).

## About

- **App:** Fetch 1.2.0
- **Application ID:** `com.iroddyan.fetch`
- **Creator and maintainer:** Iroddyan Waykins
- **Development collaborator:** Codex (AI)
- **License:** MIT
- **Platform:** Linux desktop

## Current limits

- Playlist inspection lists up to 500 entries at once. Use the playlist range controls to select a range beyond the visible list.
- Exact format selection is available for individually inspected media. Playlist downloads use best quality or audio-only settings because a playlist inspection does not retrieve every item's complete format list.
- Pause stops yt-dlp, and resume relaunches it to continue from partial files. By default, Clear Queue on Quit removes queued work and stops active downloads when Fetch closes. Turn it off in Settings → Downloads to keep interrupted items for later.
- Flatpak can read browser profiles only when the user grants the required access; the imported cookies.txt flow avoids that dependency.
