#!/bin/sh
set -eu

DATA_DIR=${XDG_DATA_HOME:-"${HOME:?HOME is not set}/.local/share"}
BIN_DIR=${XDG_BIN_HOME:-"${HOME:?HOME is not set}/.local/bin"}
DESKTOP_DIR="$DATA_DIR/applications"
ICON_DIR="$DATA_DIR/icons/hicolor/scalable/apps"

rm -rf "$DATA_DIR/fetch"
rm -f "$BIN_DIR/fetch" "$DESKTOP_DIR/com.iroddyan.fetch.desktop" "$ICON_DIR/com.iroddyan.fetch.svg"
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DESKTOP_DIR" || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f "$DATA_DIR/icons/hicolor" || true
fi
printf 'Fetch has been removed from this user account.\n'
