#!/bin/bash
#
# Build MacTask.app - a self-contained macOS app bundle.
#
# Python, PyObjC and pynput are bundled inside, so whoever downloads it needs
# nothing installed. For a lightweight bundle that borrows the system Python
# instead, see build_app.sh.
#
# Usage:   ./build_macos.sh        -> creates dist/MacTask.app
#
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

python3 -m pip install --upgrade pyinstaller pynput pyobjc-framework-Quartz

python3 -m PyInstaller --noconfirm --clean --windowed \
    --name MacTask \
    --osx-bundle-identifier com.mactask.app \
    mactask_main.py

echo
echo "Done: $HERE/dist/MacTask.app"
echo "Open it with:  open \"$HERE/dist/MacTask.app\""
