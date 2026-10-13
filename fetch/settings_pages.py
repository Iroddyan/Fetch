from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio, GLib, Gtk

from . import APP_VERSION, DEVELOPER_NAME
from .dependencies import is_flatpak, previous_yt_dlp_path, rollback_yt_dlp, update_deno, update_yt_dlp
from .engine import binary_version
from .paths import config_dir, state_dir
from .queue import QueueManager
from .settings import Settings

if TYPE_CHECKING:
    from .window import FetchWindow


class SettingsWindow(Adw.PreferencesWindow):
    def __init__(self, parent: FetchWindow, settings: Settings, queue_manager: QueueManager) -> None:
        super().__init__(transient_for=parent, modal=True, title="Settings", search_enabled=True)
        self.parent_window = parent
        self.settings = settings
        self.queue_manager = queue_manager
        self._entry_timers: dict[str, int] = {}
        self.set_default_size(1000, 760)

        downloads = Adw.PreferencesPage(title="Downloads", icon_name="folder-download-symbolic")
        folder_group = Adw.PreferencesGroup(title="Save Downloads")
        folder_row = Adw.ActionRow(title="Download Location", subtitle=str(settings.download_directory))
        choose_button = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        choose_button.connect("clicked", self._choose_directory, folder_row)
        folder_row.add_suffix(choose_button)
        folder_row.set_activatable_widget(choose_button)
        folder_group.add(folder_row)
        downloads.add(folder_group)

        queue_group = Adw.PreferencesGroup(title="Queue")
        concurrency_row = Adw.ActionRow(
            title="Simultaneous Downloads",
            subtitle="Limit active downloads to reduce network and system load.",
        )
        adjustment = Gtk.Adjustment(value=settings.queue_concurrency, lower=1, upper=4, step_increment=1)
        concurrency = Gtk.SpinButton(adjustment=adjustment, climb_rate=1, digits=0, valign=Gtk.Align.CENTER)
        concurrency.connect("value-changed", self._concurrency_changed)
        concurrency_row.add_suffix(concurrency)
        queue_group.add(concurrency_row)
        self._add_switch(
            queue_group,
            "Clear Queue on Quit",
            "Remove queued items and stop active downloads when Fetch closes. Turn this off to keep interrupted items for later.",
            "clear_queue_on_exit",
        )
        downloads.add(queue_group)
        quality_group = Adw.PreferencesGroup(
            title="Video Quality",
            description="Compatible prefers H.264 and AAC and merges to MP4. Maximum resolution sets yt-dlp’s resolution preference.",
        )
        self.quality_row = Adw.ComboRow(
            title="Quality Preference",
            model=Gtk.StringList.new(["Compatible", "Best quality"]),
        )
        self.quality_row.set_selected(0 if settings.values.get("quality_preference", "compatible") == "compatible" else 1)
        self.quality_row.connect("notify::selected", self._quality_preference_changed)
        quality_group.add(self.quality_row)
        self.resolution_values = ("", "2160", "1440", "1080", "720", "480")
        self.resolution_row = Adw.ComboRow(
            title="Maximum Resolution",
            model=Gtk.StringList.new(["No limit", "2160p", "1440p", "1080p", "720p", "480p"]),
        )
        saved_resolution = str(settings.values.get("max_resolution", ""))
        self.resolution_row.set_selected(
            self.resolution_values.index(saved_resolution) if saved_resolution in self.resolution_values else 0
        )
        self.resolution_row.connect("notify::selected", self._max_resolution_changed)
        quality_group.add(self.resolution_row)
        downloads.add(quality_group)
        downloads.set_name("downloads")
        self.add(downloads)

        media = Adw.PreferencesPage(title="Media", icon_name="audio-x-generic-symbolic")
        music_group = Adw.PreferencesGroup(
            title="Music Downloads",
            description="Audio files use Artist - Title. When artist details are missing, Fetch uses the uploader name.",
        )
        self._add_switch(
            music_group,
            "Embed Artwork",
            "Add square, 1:1 artwork to supported audio formats by default.",
            "embed_thumbnail",
        )
        self._add_switch(
            music_group,
            "Include Media ID in Filename",
            "Append the source media ID to music filenames to help distinguish similarly named tracks.",
            "include_media_id",
        )
        media.add(music_group)
        media.set_name("media")
        self.add(media)

        privacy = Adw.PreferencesPage(title="Privacy & Security", icon_name="channel-secure-symbolic")
        privacy_group = Adw.PreferencesGroup(title="Sign In")
        cookies = Adw.PreferencesPage(title="Cookies", icon_name="channel-secure-symbolic")
        cookie_group = Adw.PreferencesGroup(
            title="Account Cookies",
            description="Use an exported cookies.txt file for sign-in or age-gated media. Fetch keeps the imported file in your private app settings folder.",
        )
        self.cookie_row = Adw.ActionRow(title="Imported cookies.txt", subtitle=self._cookie_subtitle())
        import_button = Gtk.Button(label="Import…", valign=Gtk.Align.CENTER)
        import_button.connect("clicked", self._import_cookies)
        self.cookie_row.add_suffix(import_button)
        self.cookie_row.set_activatable_widget(import_button)
        cookie_group.add(self.cookie_row)
        self.use_cookies_switch = self._add_switch(
            cookie_group, "Use Cookies", "Pass the selected cookie source to yt-dlp.", "use_cookies"
        )
        browser_row = Adw.ComboRow(
            title="Browser Cookies",
            subtitle="Browser profile access may be limited by Flatpak permissions.",
            model=Gtk.StringList.new(["Off", "Firefox", "Chrome", "Chromium", "Brave", "Edge", "Vivaldi"]),
        )
        browser_map = ("", "firefox", "chrome", "chromium", "brave", "edge", "vivaldi")
        saved_browser = str(settings.values.get("browser_cookies", ""))
        browser_row.set_selected(browser_map.index(saved_browser) if saved_browser in browser_map else 0)
        browser_row.connect("notify::selected", self._browser_cookies_changed, browser_map)
        if is_flatpak():
            browser_row.set_sensitive(False)
            cookie_group.add(Adw.ActionRow(
                title="Browser cookies unavailable",
                subtitle="Flatpak cannot read browser profiles by default. Import a cookies.txt file above instead.",
            ))
        cookie_group.add(browser_row)
        cookie_group.add(Adw.ActionRow(
            title="Cookie storage",
            subtitle="Imported cookies are stored with user-only permissions. Remove them here when you no longer need them.",
        ))
        self.remove_cookies_button = Gtk.Button(label="Remove Imported File", valign=Gtk.Align.CENTER)
        self.remove_cookies_button.set_sensitive(bool(settings.values.get("cookies_file", "")))
        remove_row = Adw.ActionRow(title="Remove cookies")
        self.remove_cookies_button.connect("clicked", self._remove_cookies)
        remove_row.add_suffix(self.remove_cookies_button)
        cookie_group.add(remove_row)
        cookies.add(cookie_group)
        self._cookies_subpage = Adw.NavigationPage.new(cookies, "Cookies")
        cookie_row = Adw.ActionRow(title="Cookies", subtitle="Manage sign-in cookies for media sites.")
        cookie_row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        cookie_row.set_activatable(True)
        cookie_row.connect("activated", lambda *_: self._open_subpage(self._cookies_subpage, "Cookies"))
        privacy_group.add(cookie_row)
        privacy.add(privacy_group)
        privacy.set_name("privacy")
        self.add(privacy)

        engine = Adw.PreferencesPage(title="Engine", icon_name="applications-system-symbolic")
        engine.set_name("engine")
        engine_group = Adw.PreferencesGroup(
            title="yt-dlp",
            description="Fetch maintains its own copy under your user data folder. Updating it does not replace a system-managed copy.",
        )
        self.engine_version_row = Adw.ActionRow(title="Active version", subtitle="Checking…")
        engine_group.add(self.engine_version_row)
        self._add_switch(
            engine_group,
            "Check for yt-dlp Updates",
            "Look for a newer release at most once every 24 hours when Fetch launches.",
            "auto_check_yt_dlp",
        )
        self.yt_dlp_channels = ("stable", "nightly")
        self.yt_dlp_channel_row = Adw.ComboRow(
            title="Update Channel",
            subtitle="Stable is recommended; Nightly receives newer extractor changes sooner.",
            model=Gtk.StringList.new(["Stable", "Nightly"]),
        )
        saved_channel = str(settings.values.get("yt_dlp_channel", "stable"))
        self.yt_dlp_channel_row.set_selected(self.yt_dlp_channels.index(saved_channel) if saved_channel in self.yt_dlp_channels else 0)
        self.yt_dlp_channel_row.connect("notify::selected", self._yt_dlp_channel_changed)
        engine_group.add(self.yt_dlp_channel_row)
        update_row = Adw.ActionRow(
            title="Update yt-dlp",
            subtitle="Check the official release and install it after verifying its SHA-256 checksum.",
        )
        self.update_feedback = Gtk.Label(label="", xalign=1, wrap=True)
        self.update_button = Gtk.Button(label="Check & Update", valign=Gtk.Align.CENTER)
        self.update_button.connect("clicked", self._update_yt_dlp)
        update_row.add_suffix(self.update_button)
        engine_group.add(update_row)
        engine_group.add(self.update_feedback)
        rollback_row = Adw.ActionRow(
            title="Roll Back yt-dlp",
            subtitle="Restore the previously installed Fetch-managed version.",
        )
        self.rollback_feedback = Gtk.Label(label="", xalign=1, wrap=True)
        self.rollback_button = Gtk.Button(label="Roll Back", valign=Gtk.Align.CENTER)
        self.rollback_button.set_sensitive(previous_yt_dlp_path().is_file())
        self.rollback_button.connect("clicked", self._rollback_yt_dlp)
        rollback_row.add_suffix(self.rollback_button)
        engine_group.add(rollback_row)
        engine_group.add(self.rollback_feedback)
        engine.add(engine_group)

        dependency_group = Adw.PreferencesGroup(
            title="Media Dependencies",
            description="ffmpeg is used to merge separate streams and handle conversion. Deno 2.3+ is recommended for full YouTube support; official yt-dlp binaries include the EJS scripts.",
        )
        self.dependency_rows: dict[str, tuple[Adw.ActionRow, Gtk.Image]] = {}
        for name in ("ffmpeg", "ffprobe", "deno"):
            row = Adw.ActionRow(title=name, subtitle="Checking…")
            icon = Gtk.Image.new_from_icon_name("content-loading-symbolic")
            icon.set_valign(Gtk.Align.CENTER)
            row.add_suffix(icon)
            dependency_group.add(row)
            self.dependency_rows[name] = (row, icon)
            if name == "deno":
                self.deno_version_row = row
        deno_update_row = Adw.ActionRow(
            title="Update Deno",
            subtitle="Fetch can install a checksum-verified runtime in your user data folder.",
        )
        self.deno_update_feedback = Gtk.Label(label="", xalign=1, wrap=True)
        self.deno_update_button = Gtk.Button(label="Check & Update", valign=Gtk.Align.CENTER)
        self.deno_update_button.connect("clicked", self._update_deno)
        deno_update_row.add_suffix(self.deno_update_button)
        dependency_group.add(deno_update_row)
        dependency_group.add(self.deno_update_feedback)
        ejs_version = "Bundled with the official Fetch-managed yt-dlp release. Third-party builds vary."
        dependency_group.add(Adw.ActionRow(title="yt-dlp-ejs support", subtitle=ejs_version))
        engine.add(dependency_group)
        self.add(engine)

        advanced = Adw.PreferencesPage(title="Advanced", icon_name="preferences-other-symbolic")
        advanced_group = Adw.PreferencesGroup(
            title="Engine Options",
            description="These options are captured when you add a download to the queue. The Command action shows the exact invocation.",
        )
        self._add_switch(advanced_group, "Use yt-dlp.conf", "Allow yt-dlp to load its normal configuration file.", "use_yt_dlp_config")
        self._add_switch(advanced_group, "Avoid Duplicate Downloads", "Keep a Fetch download archive and skip items already completed by yt-dlp.", "download_archive")
        self._add_switch(advanced_group, "Remove Sponsored Segments", "Ask SponsorBlock to remove sponsor segments when available.", "sponsorblock")
        self._add_switch(advanced_group, "Embed Metadata", "Write available title and creator information into the media file.", "embed_metadata")
        self._add_text_setting(advanced_group, "Proxy", "Optional HTTP, HTTPS or SOCKS proxy.", "proxy", "https://proxy.example:8080")
        self._add_text_setting(advanced_group, "Speed Limit", "Optional yt-dlp rate limit, for example 2M.", "rate_limit", "2M")
        retries_row = Adw.ActionRow(title="Network Retries", subtitle="Retry transient connection failures.")
        retry_adjustment = Gtk.Adjustment(value=int(settings.values.get("retry_count", 10)), lower=0, upper=100, step_increment=1)
        retries = Gtk.SpinButton(adjustment=retry_adjustment, climb_rate=1, digits=0, valign=Gtk.Align.CENTER)
        retries.connect("value-changed", self._retry_count_changed)
        retries_row.add_suffix(retries)
        advanced_group.add(retries_row)
        self._add_text_setting(
            advanced_group,
            "Custom yt-dlp Arguments",
            "Optional raw arguments, split using normal shell-style quoting. Fetch still launches yt-dlp without a shell.",
            "custom_arguments",
            "--merge-output-format mkv",
        )
        advanced.add(advanced_group)
        self._advanced_subpage = Adw.NavigationPage.new(advanced, "Advanced")
        advanced_row = Adw.ActionRow(title="Advanced Engine Options", subtitle="Open custom arguments and network controls.")
        advanced_row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        advanced_row.set_activatable(True)
        advanced_row.connect("activated", lambda *_: self._open_subpage(self._advanced_subpage, "Advanced"))
        engine_group.add(advanced_row)

        appearance = Adw.PreferencesPage(title="Appearance", icon_name="applications-graphics-symbolic")
        appearance_group = Adw.PreferencesGroup(title="Appearance")
        theme_row = Adw.ComboRow(
            title="Theme",
            subtitle="Match your desktop or choose a light or dark appearance.",
            model=Gtk.StringList.new(["System", "Light", "Dark"]),
        )
        theme_row.set_selected({"system": 0, "light": 1, "dark": 2}[settings.color_scheme])
        theme_row.connect("notify::selected", self._theme_changed)
        appearance_group.add(theme_row)
        appearance.add(appearance_group)
        appearance.set_name("appearance")
        self.add(appearance)

        notifications = Adw.PreferencesPage(title="Notifications", icon_name="preferences-system-notifications-symbolic")
        notifications_group = Adw.PreferencesGroup(title="Desktop")
        self._add_switch(notifications_group, "Download Notifications", "Show a desktop notification when a download completes or fails.", "notifications_enabled")
        clipboard_switch_row = Adw.ActionRow(
            title="Offer Copied Links",
            subtitle="Check the clipboard for media links and offer a Paste Link action. Fetch never starts a download automatically.",
        )
        clipboard_switch = Gtk.Switch(
            active=bool(settings.values.get("clipboard_offer", False)),
            valign=Gtk.Align.CENTER,
        )
        clipboard_switch.connect("notify::active", self._clipboard_changed)
        clipboard_switch_row.add_suffix(clipboard_switch)
        notifications_group.add(clipboard_switch_row)
        notifications.add(notifications_group)
        notifications.set_name("notifications")
        self.add(notifications)

        about = Adw.PreferencesPage(title="About", icon_name="help-about-symbolic")
        identity_group = Adw.PreferencesGroup(title="Fetch")
        identity_group.add(Adw.ActionRow(title="Version", subtitle=f"{APP_VERSION} · Stable release"))
        identity_group.add(Adw.ActionRow(title="Application ID", subtitle="com.iroddyan.fetch"))
        identity_group.add(Adw.ActionRow(title="Creator and Maintainer", subtitle=DEVELOPER_NAME))
        identity_group.add(Adw.ActionRow(title="Development Collaborator", subtitle="Codex · AI"))
        identity_group.add(Adw.ActionRow(title="Project", subtitle="Personal project · Linux desktop"))
        identity_group.add(Adw.ActionRow(title="License", subtitle="Not specified"))
        identity_group.add(Adw.ActionRow(title="Copyright", subtitle=f"© 2026 {DEVELOPER_NAME}"))
        about.add(identity_group)
        versions_group = Adw.PreferencesGroup(title="Versions")
        versions_group.add(Adw.ActionRow(title="Fetch", subtitle=APP_VERSION))
        self.about_version_rows: dict[str, Adw.ActionRow] = {}
        for name in ("yt-dlp", "ffmpeg", "ffprobe", "deno"):
            row = Adw.ActionRow(title=name, subtitle="Checking…")
            versions_group.add(row)
            self.about_version_rows[name] = row
        versions_group.add(Adw.ActionRow(
            title="Desktop toolkit",
            subtitle=f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()} · libadwaita {Adw.get_major_version()}.{Adw.get_minor_version()}",
        ))
        about.add(versions_group)
        about.set_name("about")
        self.add(about)
        saved_page = str(settings.values.get("settings_page", ""))
        if saved_page in {"Cookies", "Advanced"}:
            self.set_visible_page_name("privacy" if saved_page == "Cookies" else "engine")
            GLib.idle_add(self._restore_subpage, self._cookies_subpage if saved_page == "Cookies" else self._advanced_subpage)
        elif saved_page:
            aliases = {
                "Downloads": "downloads", "Media": "media", "Privacy & Security": "privacy",
                "Engine": "engine", "Appearance": "appearance", "Notifications": "notifications", "About": "about",
            }
            self.set_visible_page_name(aliases.get(saved_page, saved_page))
        self.connect("notify::visible-page-name", self._settings_page_changed)
        threading.Thread(target=self._load_versions, name="fetch-settings-versions", daemon=True).start()

    def _open_subpage(self, page: Adw.NavigationPage, page_name: str) -> None:
        self.settings.set("settings_page", page_name)
        self.push_subpage(page)

    def _restore_subpage(self, page: Adw.NavigationPage) -> bool:
        self.push_subpage(page)
        return GLib.SOURCE_REMOVE

    def _load_versions(self) -> None:
        versions = {name: binary_version(name) for name in ("yt-dlp", "ffmpeg", "ffprobe", "deno")}
        GLib.idle_add(self._show_versions, versions)

    def _show_versions(self, versions: dict[str, str | None]) -> bool:
        for name, version in versions.items():
            detail = version or ("Not found" if name == "yt-dlp" else "Not found on PATH")
            if name == "yt-dlp":
                self.engine_version_row.set_subtitle(detail)
            else:
                row, icon = self.dependency_rows[name]
                row.set_subtitle(detail)
                icon.set_from_icon_name("emblem-ok-symbolic" if version else "dialog-warning-symbolic")
            self.about_version_rows[name].set_subtitle(detail)
        return GLib.SOURCE_REMOVE

    def _add_switch(self, group: Adw.PreferencesGroup, title: str, subtitle: str, key: str) -> Gtk.Switch:
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        switch = Gtk.Switch(active=bool(self.settings.values.get(key, False)), valign=Gtk.Align.CENTER)
        switch.connect("notify::active", self._switch_changed, key)
        row.add_suffix(switch)
        row.set_activatable_widget(switch)
        group.add(row)
        return switch

    def _add_text_setting(
        self, group: Adw.PreferencesGroup, title: str, subtitle: str, key: str, placeholder: str
    ) -> Gtk.Entry:
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        entry = Gtk.Entry(
            text=str(self.settings.values.get(key, "")),
            placeholder_text=placeholder,
            width_chars=24,
            valign=Gtk.Align.CENTER,
        )
        entry.connect("changed", self._entry_changed, key)
        entry.connect("activate", self._commit_entry, key)
        entry.connect("notify::has-focus", self._entry_focus_changed, key)
        row.add_suffix(entry)
        group.add(row)
        return entry

    def _entry_changed(self, entry: Gtk.Entry, key: str) -> None:
        previous = self._entry_timers.pop(key, None)
        if previous:
            GLib.source_remove(previous)
        self._entry_timers[key] = GLib.timeout_add(400, self._save_entry, entry, key)

    def _entry_focus_changed(self, entry: Gtk.Entry, _param: object, key: str) -> None:
        if not entry.has_focus():
            self._commit_entry(entry, key)

    def _commit_entry(self, entry: Gtk.Entry, key: str) -> None:
        timer = self._entry_timers.pop(key, None)
        if timer:
            GLib.source_remove(timer)
        self.settings.set(key, entry.get_text())

    def _save_entry(self, entry: Gtk.Entry, key: str) -> bool:
        self._entry_timers.pop(key, None)
        self.settings.set(key, entry.get_text())
        return GLib.SOURCE_REMOVE

    def _switch_changed(self, switch: Gtk.Switch, _param: object, key: str) -> None:
        self.settings.set(key, switch.get_active())

    def _yt_dlp_channel_changed(self, row: Adw.ComboRow, _param: object) -> None:
        index = min(row.get_selected(), len(self.yt_dlp_channels) - 1)
        self.settings.set("yt_dlp_channel", self.yt_dlp_channels[index])

    def _settings_page_changed(self, window: Adw.PreferencesWindow, _param: object) -> None:
        page_name = window.get_visible_page_name()
        if page_name:
            self.settings.set("settings_page", page_name)

    def _concurrency_changed(self, spin: Gtk.SpinButton) -> None:
        value = spin.get_value_as_int()
        self.settings.set("queue_concurrency", value)
        self.queue_manager.set_concurrency(value)

    def _retry_count_changed(self, spin: Gtk.SpinButton) -> None:
        self.settings.set("retry_count", spin.get_value_as_int())

    def _quality_preference_changed(self, row: Adw.ComboRow, _param: object) -> None:
        self.settings.set("quality_preference", "compatible" if row.get_selected() == 0 else "best")

    def _max_resolution_changed(self, row: Adw.ComboRow, _param: object) -> None:
        self.settings.set("max_resolution", self.resolution_values[row.get_selected()])

    def _theme_changed(self, row: Adw.ComboRow, _param: object) -> None:
        scheme = ("system", "light", "dark")[row.get_selected()]
        self.settings.set("color_scheme", scheme)
        apply_color_scheme(scheme)

    def _clipboard_changed(self, switch: Gtk.Switch, _param: object) -> None:
        active = switch.get_active()
        self.settings.set("clipboard_offer", active)
        if active:
            self.parent_window._enable_clipboard_offer()
        elif self.parent_window._clipboard_source_id:
            GLib.source_remove(self.parent_window._clipboard_source_id)
            self.parent_window._clipboard_source_id = None
            self.parent_window.clipboard_revealer.set_reveal_child(False)

    def _browser_cookies_changed(self, row: Adw.ComboRow, _param: object, choices: tuple[str, ...]) -> None:
        index = min(row.get_selected(), len(choices) - 1)
        self.settings.set("browser_cookies", choices[index])

    def _cookie_subtitle(self) -> str:
        path = str(self.settings.values.get("cookies_file", ""))
        return Path(path).name if path else "No file imported"

    def _import_cookies(self, _button: Gtk.Button) -> None:
        chooser = Gtk.FileChooserNative.new(
            "Import cookies.txt", self, Gtk.FileChooserAction.OPEN, "Import", "Cancel"
        )

        def response(dialog: Gtk.FileChooserNative, result: int) -> None:
            if result == Gtk.ResponseType.ACCEPT:
                source = dialog.get_file()
                try:
                    if not source:
                        raise ValueError("The selected file couldn’t be opened.")
                    _success, payload, _etag = source.load_contents(None)
                    if not payload or not (
                        b"# Netscape HTTP Cookie File" in payload[:4096]
                        or b"# HTTP Cookie File" in payload[:4096]
                    ):
                        raise ValueError("Choose a Netscape-format cookies.txt file.")
                    target = config_dir() / "cookies.txt"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_suffix(".txt.tmp")
                    temporary.write_bytes(payload)
                    import os
                    os.chmod(temporary, 0o600)
                    temporary.replace(target)
                    self.settings.set("cookies_file", str(target))
                    self.settings.set("use_cookies", True)
                    self.use_cookies_switch.set_active(True)
                    self.cookie_row.set_subtitle(target.name)
                    self.remove_cookies_button.set_sensitive(True)
                except (GLib.Error, OSError, ValueError) as error:
                    self.cookie_row.set_subtitle(f"Couldn’t import cookies: {error}")
            dialog.destroy()

        chooser.connect("response", response)
        chooser.show()

    def _remove_cookies(self, _button: Gtk.Button) -> None:
        target = config_dir() / "cookies.txt"
        try:
            target.unlink(missing_ok=True)
        except OSError as error:
            self.cookie_row.set_subtitle(f"Couldn’t remove cookies: {error}")
            return
        self.settings.set("cookies_file", "")
        self.settings.set("use_cookies", False)
        self.use_cookies_switch.set_active(False)
        self.cookie_row.set_subtitle("No file imported")

    def _update_yt_dlp(self, _button: Gtk.Button) -> None:
        self.update_button.set_sensitive(False)
        self.update_feedback.set_text("Checking the official yt-dlp release…")

        def work() -> None:
            try:
                version, changed = update_yt_dlp(str(self.settings.values.get("yt_dlp_channel", "stable")))
                message = f"Updated to {version}." if changed else f"yt-dlp {version} is already current."
                GLib.idle_add(self._update_finished, version, message, None)
            except Exception as error:
                GLib.idle_add(self._update_finished, "", "", str(error))

        threading.Thread(target=work, name="fetch-engine-update", daemon=True).start()

    def _update_finished(self, version: str, message: str, error: str | None) -> bool:
        self.update_button.set_sensitive(True)
        self.update_feedback.set_text(error or message)
        self.rollback_button.set_sensitive(previous_yt_dlp_path().is_file())
        if version:
            self.engine_version_row.set_subtitle(version)
            self.about_version_rows["yt-dlp"].set_subtitle(version)
            self.parent_window._update_engine_summary()
        return GLib.SOURCE_REMOVE

    def _rollback_yt_dlp(self, _button: Gtk.Button) -> None:
        self.rollback_button.set_sensitive(False)
        self.rollback_feedback.set_text("Restoring the previous yt-dlp…")

        def work() -> None:
            try:
                version = rollback_yt_dlp()
                GLib.idle_add(self._rollback_finished, version, None)
            except Exception as error:
                GLib.idle_add(self._rollback_finished, "", str(error))

        threading.Thread(target=work, name="fetch-engine-rollback", daemon=True).start()

    def _rollback_finished(self, version: str, error: str | None) -> bool:
        self.rollback_feedback.set_text(error or f"Restored yt-dlp {version}.")
        self.rollback_button.set_sensitive(previous_yt_dlp_path().is_file())
        if version:
            self.engine_version_row.set_subtitle(version)
            self.about_version_rows["yt-dlp"].set_subtitle(version)
            self.parent_window._update_engine_summary()
        return GLib.SOURCE_REMOVE

    def _update_deno(self, _button: Gtk.Button) -> None:
        self.deno_update_button.set_sensitive(False)
        self.deno_update_feedback.set_text("Checking the official Deno release…")

        def work() -> None:
            try:
                version, changed = update_deno()
                message = f"Updated Deno to {version}." if changed else f"Deno {version} is already current."
                GLib.idle_add(self._deno_update_finished, version, message, None)
            except Exception as error:
                GLib.idle_add(self._deno_update_finished, "", "", str(error))

        threading.Thread(target=work, name="fetch-deno-update", daemon=True).start()

    def _deno_update_finished(self, version: str, message: str, error: str | None) -> bool:
        self.deno_update_button.set_sensitive(True)
        self.deno_update_feedback.set_text(error or message)
        if version:
            self.deno_version_row.set_subtitle(version)
            self.about_version_rows["deno"].set_subtitle(version)
        return GLib.SOURCE_REMOVE

    def _choose_directory(self, _button: Gtk.Button, row: Adw.ActionRow) -> None:
        chooser = Gtk.FileChooserNative.new(
            "Choose Download Location", self, Gtk.FileChooserAction.SELECT_FOLDER, "Choose", "Cancel"
        )
        chooser.set_current_folder(Gio.File.new_for_path(str(self.settings.download_directory)))

        def response(dialog: Gtk.FileChooserNative, result: int) -> None:
            if result == Gtk.ResponseType.ACCEPT:
                folder = dialog.get_file()
                if folder and folder.get_path():
                    selected = Path(folder.get_path())
                    self.settings.set("download_directory", str(selected))
                    row.set_subtitle(str(selected))
            dialog.destroy()

        chooser.connect("response", response)
        chooser.show()
