#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VERSION=$(sed -n 's/^APP_VERSION = "\([^"]*\)"$/\1/p' "$ROOT/fetch/__init__.py")
if [ -z "$VERSION" ]; then
    echo "Could not read Fetch's version." >&2
    exit 1
fi

TOPDIR=${RPM_BUILD_TOPDIR:-"$HOME/rpmbuild"}
mkdir -p "$TOPDIR/SOURCES" "$TOPDIR/BUILD" "$TOPDIR/BUILDROOT" "$TOPDIR/RPMS" "$TOPDIR/SRPMS"
TEMP_DIR=$(mktemp -d)
trap 'rm -rf "$TEMP_DIR"' EXIT HUP INT TERM
SOURCE_ROOT="$TEMP_DIR/fetch-$VERSION"
mkdir -p "$SOURCE_ROOT"
cp -a "$ROOT/fetch" "$ROOT/packaging" "$ROOT/flatpak" "$ROOT/icons" "$ROOT/com.iroddyan.fetch.desktop" "$ROOT/run-fetch" "$ROOT/README.md" "$SOURCE_ROOT/"
find "$SOURCE_ROOT" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$SOURCE_ROOT/fetch" -type f -name '*.py' -exec chmod 0644 {} +
tar -C "$TEMP_DIR" -czf "$TOPDIR/SOURCES/fetch-$VERSION.tar.gz" "fetch-$VERSION"
rpmbuild -bb --define "_topdir $TOPDIR" "$ROOT/packaging/fetch.spec"
printf 'RPM packages are in %s/RPMS/noarch\n' "$TOPDIR"
