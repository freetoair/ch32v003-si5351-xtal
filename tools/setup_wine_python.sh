#!/usr/bin/env bash
# One-time: install a Windows Python into a private Wine prefix so that
# tools/build_app.sh windows can produce a .exe without a Windows machine.
# PyInstaller cannot cross-compile; this is the way around that.
set -euo pipefail
cd "$(dirname "$0")/.."
BUILD=$PWD/.build
export WINEPREFIX=$BUILD/wine
export WINEDEBUG=-all
PYVER=${PYVER:-3.11.9}
INST=$BUILD/python-$PYVER-amd64.exe

mkdir -p "$BUILD"
[ -f "$INST" ] || wget -q --show-progress -O "$INST" \
  "https://www.python.org/ftp/python/$PYVER/python-$PYVER-amd64.exe"

wineboot --init 2>/dev/null || true
wine "$INST" /quiet InstallAllUsers=1 TargetDir='C:\Python311' \
     PrependPath=1 Include_test=0 Include_doc=0 Include_launcher=0
echo "installed:"
wine 'C:\Python311\python.exe' -V
