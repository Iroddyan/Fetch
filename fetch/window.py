from __future__ import annotations

import threading
import shlex
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import APP_NAME, APP_VERSION, DEVELOPER_NAME
from .dependencies import update_deno, update_yt_dlp
from .engine import ErrorDetails, EngineError, FormatInfo, MediaInfo, _is_video_playlist_url, binary_version, find_binary, inspect_url
from .paths import config_dir, state_dir
from .queue import QueueItem, QueueManager
from .settings import Settings


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
            model=Gtk.StringList.new(["MP3", "FLAC", "M4A", "Opus"])
        )
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
        self.playlist_audio_format = Gtk.DropDown(model=Gtk.StringList.new(["MP3", "FLAC", "M4A", "Opus"]))
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
                "format_spec": "bestaudio/best" if audio_only else "bestvideo*+bestaudio/best",
                "audio_format": audio_formats[fmt_idx] if audio_only else "",
                "is_playlist": True,
                "playlist_items": playlist_items,
                "subtitle_languages": subtitles,
                "embed_subtitles": self.playlist_embed_subtitles_check.get_active(),
                "automatic_subtitles": self.playlist_auto_subtitles_check.get_active(),
                "embed_chapters": self.playlist_embed_chapters_check.get_active(),
            }
            options.update(self._advanced_options())
            return options

        audio_only = self.audio_only_check.get_active()
        selected = self.format_dropdown.get_selected()
        chosen = self._format_choices[selected] if selected < len(self._format_choices) else None
        format_spec = "bestaudio/best" if audio_only else (
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
        }
        options.update(self._advanced_options())
        return options

    def _advanced_options(self) -> dict[str, object]:
        custom_arguments = str(self.settings.values.get("custom_arguments", ""))
        try:
            shlex.split(custom_arguments)
        except ValueError as error:
            raise ValueError(f"Custom yt-dlp arguments have unmatched quotes: {error}") from error
        use_cookies = bool(self.settings.values.get("use_cookies", False))
        cookies_file = str(self.settings.values.get("cookies_file", "")) if use_cookies else ""
        browser_cookies = str(self.settings.values.get("browser_cookies", "")) if use_cookies and not cookies_file else ""
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
            if previous != item.status and item.status in {"completed", "failed"}:
                self._notify_job(item)
            self._last_job_states[item.id] = item.status
            self.queue_list.append(self._queue_row(item))
        return GLib.SOURCE_REMOVE

    def _notify_job(self, item: QueueItem) -> None:
        if not self.settings.values.get("notifications_enabled", True):
            return
        notification = Gio.Notification.new(
            "Download complete" if item.status == "completed" else "Download failed"
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
        progress.set_visible(item.status in {"running", "paused", "completed"})
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
        if item.status == "completed":
            folder_button = Gtk.Button(label="Show in Files", icon_name="folder-open-symbolic")
            folder_button.connect("clicked", self._open_download_folder)
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
                    version, _changed = update_yt_dlp()
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

    def _open_download_folder(self, _button: Gtk.Button) -> None:
        Gio.AppInfo.launch_default_for_uri(self.settings.download_directory.as_uri(), None)


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
                version, changed = update_yt_dlp()
                message = f"Updated to {version}." if changed else f"yt-dlp {version} is already current."
                GLib.idle_add(self._update_finished, version, message, None)
            except Exception as error:
                GLib.idle_add(self._update_finished, "", "", str(error))

        threading.Thread(target=work, name="fetch-engine-update", daemon=True).start()

    def _update_finished(self, version: str, message: str, error: str | None) -> bool:
        self.update_button.set_sensitive(True)
        self.update_feedback.set_text(error or message)
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
