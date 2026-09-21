#!/usr/bin/env bash
# Build a standalone si5351-tool executable. No Python needed on the target.
#
#   ./tools/build_app.sh linux      -> dist/si5351-tool          (this machine)
#   ./tools/build_app.sh windows    -> dist/si5351-tool.exe      (via Wine)
#
# PyInstaller cannot cross-compile, so the Windows build runs a Windows Python
# under Wine. Everything lands in .build/ and dist/, both gitignored.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD
BUILD=$ROOT/.build
TARGET=${1:-linux}

case $TARGET in
linux)
  VENV=$BUILD/venv-linux
  [ -d "$VENV" ] || python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet pyinstaller PyQt5 pyserial
  "$VENV/bin/pyinstaller" \
      --onefile --windowed --clean --noconfirm \
      --name si5351-tool \
      --paths tools \
      --distpath "$ROOT/dist" \
      --workpath "$BUILD/work-linux" \
      --specpath "$BUILD" \
      tools/si5351_tool.py
  echo "-> $ROOT/dist/si5351-tool"
  ;;
windows)
  # PyInstaller cannot cross-compile, so the Windows build runs inside an image
  # that carries Wine plus a Windows Python (tobix/pywine). No Windows machine
  # and no host Wine setup needed: Ubuntu 22.04 ships Wine 6.0, too old to
  # install a modern Windows Python directly.
  #
  # The container runs as root because its Wine prefix is root-owned; the build
  # hands the artifacts back to the calling user at the end.
  command -v docker >/dev/null || { echo "docker is required for the Windows build" >&2; exit 1; }
  mkdir -p "$ROOT/dist" "$BUILD/work-win"
  docker run --rm \
      -v "$ROOT:/src" -w /src \
      -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
      tobix/pywine:3.11 \
      sh -euc '
        export WINEDEBUG=-all
        wine python -m pip install --quiet --no-warn-script-location pyinstaller PyQt5 pyserial
        wine python -m PyInstaller \
            --onefile --windowed --clean --noconfirm \
            --name si5351-tool \
            --paths tools \
            --distpath /src/dist \
            --workpath /src/.build/work-win \
            --specpath /src/.build \
            tools/si5351_tool.py
        chown -R "$HOST_UID:$HOST_GID" /src/dist /src/.build
      '
  [ -f "$ROOT/dist/si5351-tool.exe" ] || { echo "build produced no .exe" >&2; exit 1; }
  echo "-> $ROOT/dist/si5351-tool.exe"
  ;;
*)
  echo "usage: $0 [linux|windows]" >&2
  exit 2
  ;;
esac
