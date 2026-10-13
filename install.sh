#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DATA_DIR=${XDG_DATA_HOME:-"${HOME:?HOME is not set}/.local/share"}
BIN_DIR=${XDG_BIN_HOME:-"${HOME:?HOME is not set}/.local/bin"}
APP_DIR="$DATA_DIR/fetch"
DESKTOP_DIR="$DATA_DIR/applications"
ICON_DIR="$DATA_DIR/icons/hicolor/scalable/apps"

mkdir -p "$APP_DIR" "$BIN_DIR" "$DESKTOP_DIR" "$ICON_DIR"
rm -rf "$APP_DIR/fetch"
cp -R "$ROOT/fetch" "$APP_DIR/fetch"
install -m 755 "$ROOT/packaging/fetch-launcher" "$BIN_DIR/fetch"

desktop_path="$DESKTOP_DIR/com.iroddyan.fetch.desktop"
exec_path=$(printf '%s' "$BIN_DIR/fetch" | sed 's/ /\\s/g')
sed "s|^Exec=.*|Exec=$exec_path|" "$ROOT/com.iroddyan.fetch.desktop" > "$desktop_path"
install -m 644 "$ROOT/icons/hicolor/scalable/apps/com.iroddyan.fetch.svg" "$ICON_DIR/com.iroddyan.fetch.svg"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DESKTOP_DIR" || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f "$DATA_DIR/icons/hicolor" || true
fi

printf 'Fetch installed for this user. Launch it from the app menu or run %s\n' "$BIN_DIR/fetch"
