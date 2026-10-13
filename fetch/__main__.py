from __future__ import annotations

import sys
from pathlib import Path


MIN_GTK = (4, 8)
MIN_ADW = (1, 4)


def _os_release() -> dict[str, str]:
    values = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                values[key] = value.strip().strip('"').lower()
    except OSError:
        pass
    return values


def _install_command() -> str:
    release = _os_release()
    ids = {release.get("ID", ""), *release.get("ID_LIKE", "").split()}
    if ids & {"fedora", "rhel", "centos", "rocky", "almalinux"}:
        return "sudo dnf install python3-gobject gtk4 libadwaita"
    if ids & {"debian", "ubuntu", "linuxmint", "pop"}:
        return "sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1"
    if "arch" in ids or ids & {"manjaro", "endeavouros"}:
        return "sudo pacman -S python-gobject gtk4 libadwaita"
    if ids & {"opensuse-tumbleweed", "opensuse-leap", "opensuse", "suse", "sles"}:
        return "sudo zypper install python3-gobject gtk4 typelib-1_0-Gtk-4_0 libadwaita-1-0 typelib-1_0-Adw-1"
    return "Install PyGObject, GTK 4.8 or later, and libadwaita 1.4 or later with your distribution’s package manager."


def _show_startup_error(message: str) -> None:
    print(message, file=sys.stderr)
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk

        Gtk.init()
        loop = GLib.MainLoop()
        dialog = Gtk.MessageDialog(
            transient_for=None,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text="Fetch can’t start",
            secondary_text=message,
        )
        dialog.connect("response", lambda *_args: (dialog.destroy(), loop.quit()))
        dialog.present()
        loop.run()
    except (ImportError, ValueError, RuntimeError):
        pass


def main() -> None:
    try:
        from . import app

        gtk_version = (app.Gtk.get_major_version(), app.Gtk.get_minor_version())
        adw_version = (app.Adw.get_major_version(), app.Adw.get_minor_version())
        if gtk_version < MIN_GTK or adw_version < MIN_ADW:
            raise RuntimeError(
                f"Fetch requires GTK 4.8 or later and libadwaita 1.4 or later; this system has GTK {gtk_version[0]}.{gtk_version[1]} and libadwaita {adw_version[0]}.{adw_version[1]}.\n\n{_install_command()}"
            )
        app.main()
    except (ImportError, ValueError) as error:
        _show_startup_error(
            "Fetch needs PyGObject, GTK 4.8 or later, and libadwaita 1.4 or later. "
            f"The desktop libraries could not be loaded: {error}\n\n{_install_command()}"
        )
    except RuntimeError as error:
        _show_startup_error(f"{error}")


main()
