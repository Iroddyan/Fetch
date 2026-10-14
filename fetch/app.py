from __future__ import annotations

import sys

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio, GLib, Gtk

from . import APP_ID, APP_NAME, APP_VERSION
from .cli import parse_cli_arguments
from .settings import Settings
from .window import FetchWindow


class FetchApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.settings = Settings()
        self.connect("activate", self.on_activate)
        self.add_main_option("version", ord("v"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE, "Show Fetch version", None)
        self.add_main_option("help", ord("h"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE, "Show help", None)
        self.connect("command-line", self.on_command_line)

    def on_activate(self, _application: Adw.Application) -> None:
        self._present_window()

    def _present_window(self) -> FetchWindow:
        window = self.props.active_window
        if window is None:
            window = FetchWindow(application=self, settings=self.settings)
        window.present()
        return window

    def on_command_line(self, _application: Adw.Application, command_line: Gio.ApplicationCommandLine) -> int:
        options = command_line.get_options_dict()
        if options.contains("version"):
            command_line.print_literal(f"{APP_NAME} {APP_VERSION}\n")
            return 0
        arguments = command_line.get_arguments()
        if arguments:
            arguments = arguments[1:]
        mode, urls, skipped = parse_cli_arguments([str(argument) for argument in arguments])
        if mode == "version":
            command_line.print_literal(f"{APP_NAME} {APP_VERSION}\n")
            return 0
        if mode == "help":
            command_line.print_literal("Usage: fetch [--version] [--help] [URL …]\n\nOpen Fetch or queue one or more http(s) media URLs.\n")
            return 0
        window = self._present_window()
        if urls:
            window.queue_urls(urls, skipped)
        return 0


def main() -> None:
    Adw.init()
    application = FetchApplication()
    application.run(sys.argv)
