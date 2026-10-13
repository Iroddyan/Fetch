from __future__ import annotations

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio

from . import APP_ID, APP_NAME
from .settings import Settings
from .window import FetchWindow


class FetchApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.settings = Settings()
        self.connect("activate", self.on_activate)

    def on_activate(self, _application: Adw.Application) -> None:
        window = self.props.active_window
        if window is None:
            window = FetchWindow(application=self, settings=self.settings)
        window.present()


def main() -> None:
    Adw.init()
    application = FetchApplication()
    application.run(None)
