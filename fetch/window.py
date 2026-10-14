from __future__ import annotations

import os
import threading
import shlex
import time
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import APP_NAME
from .dependencies import is_flatpak, latest_yt_dlp_version, update_yt_dlp
from .engine import ErrorDetails, EngineError, FormatInfo, MediaInfo, _is_video_playlist_url, binary_version, find_binary, inspect_url
from .paths import state_dir
from .queue import QueueItem, QueueManager
from .settings import Settings, resolve_preset, should_check_yt_dlp_update
from .settings_pages import SettingsWindow


def apply_color_scheme(scheme: str) -> None:
    manager = Adw.StyleManager.get_default()
    selected = {
        "light": Adw.ColorScheme.FORCE_LIGHT,
        "dark": Adw.ColorScheme.FORCE_DARK,
    }.get(scheme, Adw.ColorScheme.DEFAULT)
    manager.set_color_scheme(selected)


def _duration_label(seconds: int | None) -> str:
    if seconds is None:
        return ""
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def _status_label(status: str) -> str:
    return {
        "waiting": "Waiting",
        "running": "Downloading",
        "paused": "Paused",
        "completed": "Completed",
        "completed_with_errors": "Completed with errors",
        "failed": "Failed",
        "interrupted": "Interrupted",
        "cancelled": "Cancelled",
    }.get(status, status.title())


class FetchWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application, settings: Settings) -> None:
        super().__init__(application=application, title=APP_NAME, default_width=820, default_height=880)
        self.settings = settings
        self.media_url: str | None = None
        self.media_info: MediaInfo | None = None
        self._format_choices: list[FormatInfo | None] = []
        self._playlist_checks: list[tuple[int, Gtk.CheckButton]] = []
        self._shutdown = False
        self._inspect_cancel_event: threading.Event | None = None
        self._last_job_states: dict[str, str] = {}
        self._updating_items: set[str] = set()
        self._inspect_error_details: ErrorDetails | None = None
        self._last_clipboard_url = ""
        self._clipboard_source_id: int | None = None
        self.queue_manager = QueueManager(lambda: settings.download_directory, settings.queue_concurrency)
        apply_color_scheme(settings.color_scheme)

        self.toolbar = Adw.ToolbarView()
        self.set_content(self.toolbar)
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=APP_NAME, css_classes=["title"]))
        settings_button = Gtk.Button(icon_name="emblem-system-symbolic", tooltip_text="Settings")
        settings_button.connect("clicked", self._open_settings)
        header.pack_end(settings_button)
        self.toolbar.add_top_bar(header)
        self.connect("close-request", self._close_requested)

        outer = Gtk.ScrolledWindow(vexpand=True)
        outer.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        clamp = Adw.Clamp(maximum_size=720, tightening_threshold=520)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.set_margin_top(26)
        content.set_margin_bottom(28)
        content.set_margin_start(24)
        content.set_margin_end(24)
        clamp.set_child(content)
        outer.set_child(clamp)
        self.toolbar.set_content(outer)

        intro = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        title = Gtk.Label(label="What would you like to download?", xalign=0)
        title.add_css_class("title-1")
        description = Gtk.Label(
            label="Paste a media link to inspect it, choose what you want, and add it to the queue.",
            xalign=0,
            wrap=True,
        )
        description.add_css_class("dim-label")
        intro.append(title)
        intro.append(description)
        content.append(intro)

        preset_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        preset_row.append(Gtk.Label(label="Preset", xalign=0))
        self.preset_dropdown = Gtk.DropDown(model=Gtk.StringList.new(["Music", "Video"]))
        self.preset_dropdown.set_selected(0 if settings.values.get("download_preset", "Music") == "Music" else 1)
        self.preset_dropdown.connect("notify::selected", self._preset_changed)
        preset_row.append(self.preset_dropdown)
        content.append(preset_row)

        self.update_banner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.update_banner.add_css_class("card")
        self.update_banner_label = Gtk.Label(label="", xalign=0, hexpand=True, wrap=True)
        self.update_banner_button = Gtk.Button(label="Update yt-dlp", valign=Gtk.Align.CENTER)
        self.update_banner_button.connect("clicked", self._update_from_banner)
        self.update_banner.append(self.update_banner_label)
        self.update_banner.append(self.update_banner_button)
        self.update_banner.set_visible(False)
        content.append(self.update_banner)

        url_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.url_entry = Gtk.Entry(hexpand=True, placeholder_text="Paste a URL")
        self.url_entry.set_activates_default(True)
        self.url_entry.connect("activate", self._inspect)
        self.url_entry.connect("changed", self._url_changed)
        self.inspect_button = Gtk.Button(label="Inspect", css_classes=["suggested-action"])
        self.inspect_button.connect("clicked", self._inspect)
        url_row.append(self.url_entry)
        url_row.append(self.inspect_button)
        self.cancel_inspect_button = Gtk.Button(label="Cancel")
        self.cancel_inspect_button.connect("clicked", self._cancel_inspection)
        self.cancel_inspect_button.set_visible(False)
        url_row.append(self.cancel_inspect_button)
        content.append(url_row)
        self.single_video_check = Gtk.CheckButton(label="Single video only")
        self.single_video_check.set_visible(False)
        content.append(self.single_video_check)
        self._single_video_overridden = False
        self.single_video_check.connect("toggled", self._single_video_toggled)

        self.status_label = Gtk.Label(label="", xalign=0, wrap=True, selectable=True)
        self.status_label.add_css_class("dim-label")
        content.append(self.status_label)
        self.inspect_error_action_button = Gtk.Button(label="", halign=Gtk.Align.START)
        self.inspect_error_action_button.set_visible(False)
        self.inspect_error_action_button.connect("clicked", self._inspect_error_action)
        content.append(self.inspect_error_action_button)

        self.clipboard_revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        clipboard_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.clipboard_label = Gtk.Label(label="A media link was copied.", xalign=0, hexpand=True)
        paste_button = Gtk.Button(label="Paste Link")
        paste_button.connect("clicked", self._paste_detected_link)
        clipboard_box.append(self.clipboard_label)
        clipboard_box.append(paste_button)
        self.clipboard_revealer.set_child(clipboard_box)
        content.append(self.clipboard_revealer)

        self.media_card = Adw.Bin()
        content.append(self.media_card)
        self.add_queue_button = Gtk.Button(label="Add to Queue", css_classes=["suggested-action", "pill"])
        self.add_queue_button.set_halign(Gtk.Align.START)
        self.add_queue_button.set_visible(False)
        self.add_queue_button.connect("clicked", self._add_to_queue)
        content.append(self.add_queue_button)

        queue_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        queue_title = Gtk.Label(label="Queue", xalign=0, hexpand=True)
        queue_title.add_css_class("title-2")
        self.start_queue_button = Gtk.Button(label="Start Queue")
        self.start_queue_button.connect("clicked", lambda *_: self.queue_manager.start())
        queue_header.append(queue_title)
        queue_header.append(self.start_queue_button)
        self.clear_queue_button = Gtk.Button(label="Clear Queue")
        self.clear_queue_button.connect("clicked", lambda *_: self.queue_manager.clear())
        queue_header.append(self.clear_queue_button)
        content.append(queue_header)
        self.queue_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.queue_list.add_css_class("boxed-list")
        content.append(self.queue_list)
        self.queue_empty_label = Gtk.Label(label="Your queue is empty.", xalign=0)
        self.queue_empty_label.add_css_class("dim-label")
        content.append(self.queue_empty_label)
        self.queue_manager.subscribe(self._render_queue)
        if settings.values.get("clipboard_offer", False):
            self._enable_clipboard_offer()

        engine_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        engine_row.set_margin_top(4)
        engine_row.add_css_class("dim-label")
        engine_row.append(Gtk.Image.new_from_icon_name("applications-system-symbolic"))
        self.engine_label = Gtk.Label(label="", xalign=0)
        engine_row.append(self.engine_label)
        content.append(engine_row)
        self._update_engine_summary()
        self._check_yt_dlp_update()

    def _close_requested(self, *_args: object) -> bool:
        self._shutdown = True
        if self._clipboard_source_id:
            GLib.source_remove(self._clipboard_source_id)
            self._clipboard_source_id = None
        self.queue_manager.shutdown(clear_queue=bool(self.settings.values.get("clear_queue_on_exit", True)))
        return False

    def _enable_clipboard_offer(self) -> None:
        if self._clipboard_source_id is None:
            self._clipboard_source_id = GLib.timeout_add_seconds(3, self._check_clipboard)

    def _check_clipboard(self) -> bool:
        if self._shutdown:
            return GLib.SOURCE_REMOVE
        display = Gdk.Display.get_default()
        if display:
            display.get_clipboard().read_text_async(None, self._clipboard_text_ready, None)
        return GLib.SOURCE_CONTINUE

    def _clipboard_text_ready(self, clipboard: Gdk.Clipboard, result: Gio.AsyncResult, _data: object) -> None:
        try:
            value = clipboard.read_text_finish(result) or ""
        except GLib.Error:
            return
        value = value.strip()
        if value.startswith(("https://", "http://")) and value != self._last_clipboard_url:
            self._last_clipboard_url = value
            self.clipboard_revealer.set_reveal_child(True)
        elif not value.startswith(("https://", "http://")):
            self.clipboard_revealer.set_reveal_child(False)

    def _paste_detected_link(self, _button: Gtk.Button) -> None:
        if self._last_clipboard_url:
            self.url_entry.set_text(self._last_clipboard_url)
            self.clipboard_revealer.set_reveal_child(False)
            self._inspect()

    def _open_settings(self, _button: Gtk.Button | None = None) -> None:
        self._open_settings_page("")

    def _open_settings_page(self, page: str) -> SettingsWindow:
        window = SettingsWindow(self, self.settings, self.queue_manager)
        self.settings_window = window
        window.present()
        root_pages = {
            "Downloads": "downloads", "Media": "media", "Privacy & Security": "privacy",
            "Cookies": "privacy", "Engine": "engine", "Appearance": "appearance",
            "Notifications": "notifications", "About": "about",
        }
        if page:
            window.set_visible_page_name(root_pages.get(page, page.lower()))
        if page == "Cookies":
            window.push_subpage(window._cookies_subpage)
        elif page == "Advanced":
            window.set_visible_page_name("engine")
            window.push_subpage(window._advanced_subpage)
        return window

    def _update_engine_summary(self) -> None:
        self.engine_label.set_text("Checking yt-dlp…")

        def work() -> None:
            version = binary_version("yt-dlp")
            GLib.idle_add(self.engine_label.set_text, f"yt-dlp {version}" if version else "yt-dlp isn’t installed")

        threading.Thread(target=work, name="fetch-version-summary", daemon=True).start()

    def _check_yt_dlp_update(self) -> None:
        if not self.settings.values.get("auto_check_yt_dlp", True):
            return
        now = int(time.time())
        last_check = int(self.settings.values.get("last_yt_dlp_check", 0))
        last_attempt = int(self.settings.values.get("last_yt_dlp_check_attempt", 0))
        if not should_check_yt_dlp_update(now, last_check, last_attempt):
            return
        channel = str(self.settings.values.get("yt_dlp_channel", "stable"))

        def work() -> None:
            try:
                latest = latest_yt_dlp_version(channel)
                if not latest:
                    raise RuntimeError("The latest yt-dlp version could not be determined.")
                self.settings.set("last_yt_dlp_check", now)
                current = binary_version("yt-dlp")
                latest_key = tuple(int(value) for value in latest.split(".") if value.isdigit())
                current_key = tuple(int(value) for value in (current or "").split(".") if value.isdigit())
                if current_key and latest_key > current_key:
                    GLib.idle_add(self._show_update_banner, latest)
            except Exception:
                self.settings.set("last_yt_dlp_check_attempt", now)
                return

        threading.Thread(target=work, name="fetch-update-check", daemon=True).start()

    def _show_update_banner(self, version: str) -> bool:
        self._available_yt_dlp_version = version
        self.update_banner_label.set_text(f"yt-dlp {version} is available.")
        self.update_banner_button.set_sensitive(True)
        self.update_banner_button.set_label("Update yt-dlp")
        self.update_banner.set_visible(True)
        return GLib.SOURCE_REMOVE

    def _update_from_banner(self, _button: Gtk.Button) -> None:
        if not getattr(self, "_available_yt_dlp_version", ""):
            return
        channel = str(self.settings.values.get("yt_dlp_channel", "stable"))
        self.update_banner_button.set_sensitive(False)
        self.update_banner_button.set_label("Updating…")

        def work() -> None:
            try:
                version, _changed = update_yt_dlp(channel)
                GLib.idle_add(self._banner_update_finished, version, None)
            except Exception as error:
                GLib.idle_add(self._banner_update_finished, "", str(error))

        threading.Thread(target=work, name="fetch-banner-update", daemon=True).start()

    def _banner_update_finished(self, version: str, error: str | None) -> bool:
        if error:
            self.update_banner_label.set_text(f"Couldn’t update yt-dlp: {error}")
            self.update_banner_button.set_sensitive(True)
            self.update_banner_button.set_label("Try Again")
        else:
            self.update_banner.set_visible(False)
            self.status_label.set_text(f"yt-dlp {version} is installed.")
            self._update_engine_summary()
        return GLib.SOURCE_REMOVE

    def _inspect(self, *_args: object) -> None:
        url = self.url_entry.get_text().strip()
        if not url:
            self.status_label.set_text("Paste a media link to get started.")
            self.url_entry.grab_focus()
            return
        self.media_url = None
        self.media_info = None
        self._inspect_error_details = None
        self.inspect_error_action_button.set_visible(False)
        self.add_queue_button.set_visible(False)
        self.media_card.set_child(None)
        self.inspect_button.set_sensitive(False)
        cancel_event = threading.Event()
        self._inspect_cancel_event = cancel_event
        self.cancel_inspect_button.set_label("Cancel")
        self.cancel_inspect_button.set_sensitive(True)
        self.cancel_inspect_button.set_visible(True)
        self.status_label.remove_css_class("error")
        self.status_label.add_css_class("dim-label")
        self.status_label.set_text("Inspecting link…")
        try:
            inspect_options = self._advanced_options()
        except ValueError as error:
            self._show_error(str(error))
            self.inspect_button.set_sensitive(True)
            self.cancel_inspect_button.set_visible(False)
            return

        def work() -> None:
            try:
                info, thumbnail = inspect_url(url, inspect_options, cancel_event=cancel_event)
                GLib.idle_add(self._show_media, info, thumbnail)
            except EngineError as error:
                if cancel_event.is_set():
                    GLib.idle_add(self._inspection_cancelled)
                else:
                    GLib.idle_add(self._show_error, error.details)
            except Exception as error:
                GLib.idle_add(self._show_error, ErrorDetails(f"Fetch couldn’t inspect this link: {error}"))
            finally:
                GLib.idle_add(self._finish_inspection)

        threading.Thread(target=work, name="fetch-inspect", daemon=True).start()

    def _url_changed(self, entry: Gtk.Entry) -> None:
        is_mixed_url = _is_video_playlist_url(entry.get_text().strip())
        self.single_video_check.set_visible(is_mixed_url)
        self._single_video_overridden = False
        self.single_video_check.set_active(is_mixed_url)
        self._single_video_overridden = False

    def _single_video_toggled(self, _check: Gtk.CheckButton) -> None:
        self._single_video_overridden = True

    def _cancel_inspection(self, _button: Gtk.Button) -> None:
        if self._inspect_cancel_event:
            self._inspect_cancel_event.set()
            self.cancel_inspect_button.set_sensitive(False)
            self.cancel_inspect_button.set_label("Stopping…")

    def _inspection_cancelled(self) -> bool:
        self.status_label.set_text("Inspection cancelled.")
        return GLib.SOURCE_REMOVE

    def _finish_inspection(self) -> bool:
        self.inspect_button.set_sensitive(True)
        self.cancel_inspect_button.set_visible(False)
        self._inspect_cancel_event = None
        return GLib.SOURCE_REMOVE

    def _show_media(self, info: MediaInfo, thumbnail: Path | None) -> bool:
        self.media_info = info
        self.media_url = info.webpage_url
        card = Gtk.Frame()
        card.add_css_class("card")
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.set_margin_top(14)
        body.set_margin_bottom(14)
        body.set_margin_start(14)
        body.set_margin_end(14)
        if thumbnail and thumbnail.is_file():
            picture = Gtk.Picture()
            picture.set_filename(str(thumbnail))
            picture.set_content_fit(Gtk.ContentFit.CONTAIN)
            picture.set_size_request(-1, 220)
            body.append(picture)
        title = Gtk.Label(label=info.title, xalign=0, wrap=True, selectable=True)
        title.add_css_class("title-3")
        body.append(title)
        details = [value for value in (info.uploader, _duration_label(info.duration)) if value]
        if details:
            subtitle = Gtk.Label(label=" · ".join(details), xalign=0, wrap=True, selectable=True)
            subtitle.add_css_class("dim-label")
            body.append(subtitle)

        if info.is_playlist:
            self._add_playlist_controls(body, info)
        else:
            self._add_media_controls(body, info)
        self._apply_preset_to_controls()
        card.set_child(body)
        self.media_card.set_child(card)
        self.status_label.set_text("Choose your options, then add to the queue.")
        self.inspect_error_action_button.set_visible(False)
        self.add_queue_button.set_label("Add Selected to Queue" if info.is_playlist else "Add to Queue")
        self.add_queue_button.set_visible(True)
        self.add_queue_button.set_sensitive(True)
        return GLib.SOURCE_REMOVE

    def _add_media_controls(self, body: Gtk.Box, info: MediaInfo) -> None:
        format_group = Adw.PreferencesGroup(title="Format")
        self._format_choices = [None, *info.formats]
        labels = ["Best quality · video + audio", *(item.label for item in info.formats)]
        self.format_dropdown = Gtk.DropDown(model=Gtk.StringList.new(labels), hexpand=True)
        format_row = Adw.ActionRow(title="Quality", subtitle="Choose a listed source format or let Fetch choose.")
        format_row.add_suffix(self.format_dropdown)
        format_group.add(format_row)
        body.append(format_group)

        audio_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.audio_only_check = Gtk.CheckButton(label="Audio only")
        audio_box.append(self.audio_only_check)
        audio_box.append(Gtk.Label(label="Save as", css_classes=["dim-label"]))
        self.audio_format_dropdown = Gtk.DropDown(
            model=Gtk.StringList.new(["MP3", "FLAC (lossless container, no quality gain from YouTube)", "M4A", "Opus"])
        )
        self.audio_format_dropdown.set_selected(2)
        audio_box.append(self.audio_format_dropdown)
        body.append(audio_box)
        self.audio_only_check.connect("toggled", self._audio_mode_changed)

        subtitle_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        subtitle_header = Gtk.Label(label="Subtitles and chapters", xalign=0)
        subtitle_header.add_css_class("heading")
        subtitle_box.append(subtitle_header)
        available = ", ".join(info.subtitles[:16]) if info.subtitles else "No subtitles listed"
        automatic = ", ".join(info.automatic_subtitles[:16]) if info.automatic_subtitles else "No auto-generated captions listed"
        subtitle_box.append(Gtk.Label(label=f"Available: {available}", xalign=0, wrap=True, css_classes=["dim-label"]))
        subtitle_box.append(Gtk.Label(label=f"Auto-generated: {automatic}", xalign=0, wrap=True, css_classes=["dim-label"]))
        sub_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.download_subtitles_check = Gtk.CheckButton(label="Download subtitles")
        self.subtitle_languages_entry = Gtk.Entry(placeholder_text="Languages, e.g. en,ja", hexpand=True)
        self.subtitle_languages_entry.set_sensitive(False)
        self.download_subtitles_check.connect(
            "toggled", lambda check: self.subtitle_languages_entry.set_sensitive(check.get_active())
        )
        sub_row.append(self.download_subtitles_check)
        sub_row.append(self.subtitle_languages_entry)
        subtitle_box.append(sub_row)
        self.auto_subtitles_check = Gtk.CheckButton(label="Include auto-generated captions")
        self.embed_subtitles_check = Gtk.CheckButton(label="Embed subtitles in the media file")
        self.embed_chapters_check = Gtk.CheckButton(label="Keep chapters")
        self.embed_chapters_check.set_active(True)
        subtitle_box.append(self.auto_subtitles_check)
        subtitle_box.append(self.embed_subtitles_check)
        subtitle_box.append(self.embed_chapters_check)
        body.append(subtitle_box)

    def _selected_preset(self) -> str:
        return "Music" if self.preset_dropdown.get_selected() == 0 else "Video"

    def _preset_changed(self, *_args: object) -> None:
        preset = self._selected_preset()
        self.settings.set("download_preset", preset)
        self._apply_preset_to_controls()

    def _apply_preset_to_controls(self) -> None:
        preset = resolve_preset(self._selected_preset())
        audio_format_index = ("mp3", "flac", "m4a", "opus").index(str(preset["audio_format"])) if preset["audio_only"] else 2
        if hasattr(self, "audio_only_check"):
            self.audio_only_check.set_active(bool(preset["audio_only"]))
            self.audio_format_dropdown.set_selected(audio_format_index)
        if hasattr(self, "playlist_audio_check"):
            self.playlist_audio_check.set_active(bool(preset["audio_only"]))
            self.playlist_audio_format.set_selected(audio_format_index)

    def _add_playlist_controls(self, body: Gtk.Box, info: MediaInfo) -> None:
        count = info.playlist_count or len(info.playlist_entries)
        body.append(Gtk.Label(label=f"Playlist · {count} items", xalign=0, css_classes=["heading"]))
        range_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        range_row.append(Gtk.Label(label="Playlist range", xalign=0))
        self.playlist_range_entry = Gtk.Entry(
            placeholder_text="Optional: 1:20:2 or 1,4,7", hexpand=True
        )
        range_row.append(self.playlist_range_entry)
        body.append(range_row)
        body.append(Gtk.Label(
            label="Enter a range to download by index, or select items below.",
            xalign=0,
            wrap=True,
            css_classes=["dim-label"],
        ))
        self._playlist_checks = []
        if info.playlist_entries:
            list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            list_box.add_css_class("boxed-list")
            shown = info.playlist_entries[:500]
            for entry in shown:
                check = Gtk.CheckButton(label=f"{entry.index}. {entry.title}")
                check.set_active(True)
                row = Gtk.ListBoxRow(child=check, activatable=False, selectable=False)
                list_box.append(row)
                self._playlist_checks.append((entry.index, check))
            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroller.set_min_content_height(min(260, max(120, len(shown) * 42)))
            scroller.set_max_content_height(260)
            scroller.set_child(list_box)
            body.append(scroller)
            if len(info.playlist_entries) > len(shown):
                body.append(Gtk.Label(
                    label=f"Showing the first {len(shown)} items. Use Playlist range to select items beyond this list.",
                    xalign=0,
                    wrap=True,
                    css_classes=["dim-label"],
                ))
        self.select_all_check = Gtk.CheckButton(label="Select all listed items")
        self.select_all_check.set_active(True)
        self.select_all_check.connect("toggled", self._select_all_playlist)
        body.append(self.select_all_check)
        options = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        options.append(Gtk.Label(label="Format", xalign=0))
        self.playlist_audio_check = Gtk.CheckButton(label="Audio only")
        options.append(self.playlist_audio_check)
        self.playlist_audio_format = Gtk.DropDown(model=Gtk.StringList.new([
            "MP3", "FLAC (lossless container, no quality gain from YouTube)", "M4A", "Opus",
        ]))
        self.playlist_audio_format.set_selected(2)
        options.append(self.playlist_audio_format)
        body.append(options)
        self.playlist_subtitles_check = Gtk.CheckButton(label="Download subtitles")
        self.playlist_languages_entry = Gtk.Entry(placeholder_text="Languages, e.g. en,ja", hexpand=True)
        self.playlist_languages_entry.set_sensitive(False)
        self.playlist_subtitles_check.connect(
            "toggled", lambda check: self.playlist_languages_entry.set_sensitive(check.get_active())
        )
        sub_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sub_row.append(self.playlist_subtitles_check)
        sub_row.append(self.playlist_languages_entry)
        body.append(sub_row)
        self.playlist_auto_subtitles_check = Gtk.CheckButton(label="Include auto-generated captions")
        self.playlist_embed_subtitles_check = Gtk.CheckButton(label="Embed subtitles")
        self.playlist_embed_chapters_check = Gtk.CheckButton(label="Keep chapters")
        self.playlist_embed_chapters_check.set_active(True)
        body.append(self.playlist_auto_subtitles_check)
        body.append(self.playlist_embed_subtitles_check)
        body.append(self.playlist_embed_chapters_check)

    def _audio_mode_changed(self, check: Gtk.CheckButton) -> None:
        active = check.get_active()
        self.format_dropdown.set_sensitive(not active)
        self.audio_format_dropdown.set_sensitive(active)

    def _select_all_playlist(self, check: Gtk.CheckButton) -> None:
        for _index, item_check in self._playlist_checks:
            item_check.set_active(check.get_active())

    def _selected_options(self) -> dict[str, object]:
        info = self.media_info
        if not info:
            return {}
        if info.is_playlist:
            range_spec = self.playlist_range_entry.get_text().strip()
            selected = [str(index) for index, check in self._playlist_checks if check.get_active()]
            playlist_items = range_spec or ",".join(selected)
            if not playlist_items and self._playlist_checks:
                raise ValueError("Select at least one playlist item or enter a playlist range.")
            if not playlist_items and not self._playlist_checks:
                raise ValueError("Enter a playlist range so Fetch knows which items to add.")
            audio_only = self.playlist_audio_check.get_active()
            fmt_idx = self.playlist_audio_format.get_selected()
            audio_formats = ("mp3", "flac", "m4a", "opus")
            subtitles = self.playlist_languages_entry.get_text().strip() if self.playlist_subtitles_check.get_active() else ""
            options = {
                "format_spec": "" if audio_only else "bestvideo*+bestaudio/best",
                "audio_format": audio_formats[fmt_idx] if audio_only else "",
                "is_playlist": True,
                "playlist_items": playlist_items,
                "subtitle_languages": subtitles,
                "embed_subtitles": self.playlist_embed_subtitles_check.get_active(),
                "automatic_subtitles": self.playlist_auto_subtitles_check.get_active(),
                "embed_chapters": self.playlist_embed_chapters_check.get_active(),
                "preset": self._selected_preset(),
            }
            options.update(self._advanced_options())
            preset = resolve_preset(self._selected_preset())
            if preset["embed_metadata"] is not None:
                options["embed_metadata"] = preset["embed_metadata"]
                options["embed_thumbnail"] = preset["embed_thumbnail"]
            return options

        audio_only = self.audio_only_check.get_active()
        selected = self.format_dropdown.get_selected()
        chosen = self._format_choices[selected] if selected < len(self._format_choices) else None
        format_spec = "" if audio_only else (
            f"{chosen.format_id}+bestaudio/best" if chosen and chosen.has_video and not chosen.has_audio
            else chosen.format_id if chosen else "bestvideo*+bestaudio/best"
        )
        audio_formats = ("mp3", "flac", "m4a", "opus")
        audio_index = self.audio_format_dropdown.get_selected()
        options = {
            "format_spec": format_spec,
            "audio_format": audio_formats[audio_index] if audio_only else "",
            "is_playlist": False,
            "subtitle_languages": self.subtitle_languages_entry.get_text().strip() if self.download_subtitles_check.get_active() else "",
            "embed_subtitles": self.embed_subtitles_check.get_active(),
            "automatic_subtitles": self.auto_subtitles_check.get_active(),
            "embed_chapters": self.embed_chapters_check.get_active(),
            "preset": self._selected_preset(),
        }
        options.update(self._advanced_options())
        preset = resolve_preset(self._selected_preset())
        if preset["embed_metadata"] is not None:
            options["embed_metadata"] = preset["embed_metadata"]
            options["embed_thumbnail"] = preset["embed_thumbnail"]
        return options

    def _advanced_options(self) -> dict[str, object]:
        custom_arguments = str(self.settings.values.get("custom_arguments", ""))
        try:
            shlex.split(custom_arguments)
        except ValueError as error:
            raise ValueError(f"Custom yt-dlp arguments have unmatched quotes: {error}") from error
        use_cookies = bool(self.settings.values.get("use_cookies", False))
        cookies_file = str(self.settings.values.get("cookies_file", "")) if use_cookies else ""
        browser_cookies = str(self.settings.values.get("browser_cookies", "")) if use_cookies and not cookies_file and not is_flatpak() else ""
        use_archive = bool(self.settings.values.get("download_archive", False))
        return {
            "use_yt_dlp_config": bool(self.settings.values.get("use_yt_dlp_config", False)),
            "cookies_file": cookies_file,
            "browser_cookies": browser_cookies,
            "proxy": str(self.settings.values.get("proxy", "")).strip(),
            "rate_limit": str(self.settings.values.get("rate_limit", "")).strip(),
            "retry_count": int(self.settings.values.get("retry_count", 10)),
            "archive_file": str(state_dir() / "download-archive.txt") if use_archive else "",
            "sponsorblock": bool(self.settings.values.get("sponsorblock", False)),
            "embed_metadata": bool(self.settings.values.get("embed_metadata", True)),
            "embed_thumbnail": bool(self.settings.values.get("embed_thumbnail", True)),
            "include_media_id": bool(self.settings.values.get("include_media_id", False)),
            "no_playlist": self.single_video_check.get_active(),
            "quality_preference": str(self.settings.values.get("quality_preference", "compatible")),
            "max_resolution": str(self.settings.values.get("max_resolution", "")),
            "custom_arguments": custom_arguments,
        }

    def _add_to_queue(self, _button: Gtk.Button) -> None:
        if not self.media_url or not self.media_info:
            return
        try:
            options = self._selected_options()
        except ValueError as error:
            self._show_error(str(error))
            return
        if self.media_info.is_playlist:
            url = self.media_url
            range_spec = str(options.get("playlist_items") or "")
            title = self.media_info.title + (f" · items {range_spec}" if range_spec else "")
        else:
            url = self.media_url
            title = self.media_info.title
        self.queue_manager.add(url, title, options)
        self.status_label.remove_css_class("error")
        self.status_label.add_css_class("dim-label")
        self.status_label.set_text("Added to the queue.")

    def _show_error(self, message: str | ErrorDetails) -> bool:
        details = message if isinstance(message, ErrorDetails) else ErrorDetails(message)
        self.status_label.remove_css_class("dim-label")
        self.status_label.add_css_class("error")
        self.status_label.set_text("\n\n".join(part for part in (details.message, details.hint) if part))
        self._inspect_error_details = details
        self.inspect_error_action_button.set_label(self._error_action_label(details.action))
        self.inspect_error_action_button.set_visible(details.action != "none")
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _error_action_label(action: str) -> str:
        return {
            "update_ytdlp": "Update yt-dlp…",
            "import_cookies": "Open Cookie Settings…",
            "install_ffmpeg": "Show ffmpeg Install Hint",
        }.get(action, "")

    def _inspect_error_action(self, _button: Gtk.Button) -> None:
        details = self._inspect_error_details
        if not details:
            return
        if details.action == "import_cookies":
            self._open_settings_page("Cookies")
        elif details.action == "update_ytdlp":
            window = self._open_settings_page("Engine")
            window._update_yt_dlp(self.inspect_error_action_button)
        elif details.action == "install_ffmpeg":
            self.status_label.set_text(details.hint)

    def _render_queue(self, items: list[QueueItem]) -> bool:
        if self._shutdown:
            return GLib.SOURCE_REMOVE
        child = self.queue_list.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.queue_list.remove(child)
            child = next_child
        self.queue_empty_label.set_visible(not items)
        self.start_queue_button.set_visible(any(item.status == "waiting" for item in items))
        self.clear_queue_button.set_sensitive(bool(items))
        for item in items:
            previous = self._last_job_states.get(item.id)
            if previous != item.status and item.status in {"completed", "completed_with_errors", "failed"}:
                self._notify_job(item)
            self._last_job_states[item.id] = item.status
            self.queue_list.append(self._queue_row(item))
        return GLib.SOURCE_REMOVE

    def _notify_job(self, item: QueueItem) -> None:
        if not self.settings.values.get("notifications_enabled", True):
            return
        notification = Gio.Notification.new(
            {
                "completed": "Download complete",
                "completed_with_errors": "Download finished with errors",
                "failed": "Download failed",
            }.get(item.status, "Download finished")
        )
        notification.set_body(item.title)
        self.get_application().send_notification(f"fetch-{item.id}", notification)

    def _queue_row(self, item: QueueItem) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow(activatable=False, selectable=False)
        layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        layout.set_margin_top(12)
        layout.set_margin_bottom(12)
        layout.set_margin_start(14)
        layout.set_margin_end(14)
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label=item.title, xalign=0, hexpand=True, wrap=True, ellipsize=3)
        title.add_css_class("heading")
        title_row.append(title)
        title_row.append(Gtk.Label(label=_status_label(item.status), xalign=1, css_classes=["dim-label"]))
        layout.append(title_row)
        progress = Gtk.ProgressBar(show_text=True)
        if item.progress_indeterminate:
            progress.pulse()
        else:
            progress.set_fraction(max(0.0, min(1.0, item.progress / 100.0)))
        detail = " · ".join(part for part in (item.speed, item.eta) if part)
        context = []
        if item.playlist_index and item.playlist_count:
            context.append(f"Item {item.playlist_index}/{item.playlist_count}")
        if item.stage:
            context.append(item.stage)
        context.extend(([] if item.progress_indeterminate else [f"{item.progress:.1f}%"]) + ([detail] if detail else []))
        progress.set_text(" · ".join(context) or _status_label(item.status))
        progress.set_visible(item.status in {"running", "paused", "completed", "completed_with_errors"})
        layout.append(progress)
        if item.error:
            error = Gtk.Label(label=item.error, xalign=0, wrap=True, selectable=True)
            error.add_css_class("error")
            layout.append(error)
        if item.error_hint:
            hint = Gtk.Label(label=item.error_hint, xalign=0, wrap=True, selectable=True)
            hint.add_css_class("dim-label")
            layout.append(hint)
        if item.error_action != "none":
            action = Gtk.Button(label=self._error_action_label(item.error_action), halign=Gtk.Align.START)
            action.set_sensitive(item.id not in self._updating_items)
            action.connect("clicked", lambda _button, key=item.id: self._queue_error_action(key))
            layout.append(action)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        if item.status == "running":
            button = Gtk.Button(label="Pause")
            button.connect("clicked", lambda _button, key=item.id: self.queue_manager.pause(key))
            buttons.append(button)
        elif item.status == "paused":
            button = Gtk.Button(label="Resume")
            button.connect("clicked", lambda _button, key=item.id: self.queue_manager.resume(key))
            buttons.append(button)
        elif item.status in {"failed", "interrupted", "cancelled"}:
            button = Gtk.Button(label="Retry")
            button.connect("clicked", lambda _button, key=item.id: self.queue_manager.retry(key))
            buttons.append(button)
        if item.status in {"running", "paused", "waiting"}:
            button = Gtk.Button(label="Cancel")
            button.connect("clicked", lambda _button, key=item.id: self.queue_manager.cancel(key))
            buttons.append(button)
        log_button = Gtk.Button(label="Log")
        log_button.set_sensitive(bool(item.logs or item.error))
        log_button.connect("clicked", lambda _button, value=item: self._show_text_window(f"Log · {value.title}", "\n".join(value.logs) or value.error))
        buttons.append(log_button)
        command_button = Gtk.Button(label="Command")
        command_button.set_sensitive(bool(item.command))
        command_button.connect("clicked", lambda _button, value=item: self._show_text_window("yt-dlp command", value.command))
        buttons.append(command_button)
        if item.status in {"completed", "completed_with_errors"}:
            folder_button = Gtk.Button(label="Show in folder", icon_name="folder-open-symbolic")
            folder_button.connect("clicked", lambda _button, path=item.output_path: self._show_output_path(path))
            buttons.append(folder_button)
        layout.append(buttons)
        row.set_child(layout)
        return row

    def _queue_error_action(self, item_id: str) -> None:
        item = next((entry for entry in self.queue_manager.items() if entry.id == item_id), None)
        if not item:
            return
        if item.error_action == "import_cookies":
            self._open_settings_page("Cookies")
        elif item.error_action == "install_ffmpeg":
            self.status_label.set_text(item.error_hint)
        elif item.error_action == "update_ytdlp" and item_id not in self._updating_items:
            self._updating_items.add(item_id)
            self.status_label.set_text("Updating yt-dlp before retrying this download…")

            def work() -> None:
                try:
                    version, _changed = update_yt_dlp(str(self.settings.values.get("yt_dlp_channel", "stable")))
                    message = f"yt-dlp {version} is ready; retrying the download."
                    GLib.idle_add(self._retry_after_engine_update, item_id, message, None)
                except Exception as error:
                    GLib.idle_add(self._retry_after_engine_update, item_id, "", str(error))

            threading.Thread(target=work, name="fetch-retry-after-update", daemon=True).start()

    def _retry_after_engine_update(self, item_id: str, message: str, error: str | None) -> bool:
        self._updating_items.discard(item_id)
        if error:
            self.status_label.set_text(f"Couldn’t update yt-dlp: {error}")
        else:
            self.status_label.set_text(message)
            self.queue_manager.retry(item_id)
        self._render_queue(self.queue_manager.items())
        return GLib.SOURCE_REMOVE

    def _show_text_window(self, title: str, value: str) -> None:
        window = Gtk.Window(title=title, transient_for=self, modal=True, default_width=720, default_height=500)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        text = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        text.get_buffer().set_text(value)
        scroll.set_child(text)
        box.append(scroll)
        close = Gtk.Button(label="Close", halign=Gtk.Align.END)
        close.connect("clicked", lambda *_: window.close())
        box.append(close)
        window.set_child(box)
        window.present()

    def _show_output_path(self, output_path: str) -> None:
        path = Path(output_path)
        if not path.is_file():
            self.status_label.set_text("The downloaded file could not be found.")
            return
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            file_descriptor = os.open(path, os.O_RDONLY)
            try:
                fd_list = Gio.UnixFDList.new()
                handle = fd_list.append(file_descriptor)
                bus.call_with_unix_fd_list_sync(
                    "org.freedesktop.portal.Desktop",
                    "/org/freedesktop/portal/desktop",
                    "org.freedesktop.portal.OpenURI",
                    "OpenDirectory",
                    GLib.Variant("(sha{sv})", ("", handle, {})),
                    GLib.VariantType.new("(o)"),
                    Gio.DBusCallFlags.NONE,
                    5000,
                    fd_list,
                    None,
                    None,
                )
            finally:
                os.close(file_descriptor)
            return
        except (GLib.Error, OSError, TypeError):
            pass
        try:
            bus.call_sync(
                "org.freedesktop.FileManager1",
                "/org/freedesktop/FileManager1",
                "org.freedesktop.FileManager1",
                "ShowItems",
                GLib.Variant("(ass)", ([path.as_uri()], "")),
                None,
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            )
            return
        except GLib.Error:
            Gio.AppInfo.launch_default_for_uri(path.parent.as_uri(), None)
