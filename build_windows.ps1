# Build MacTask.exe - a single, double-clickable Windows binary.
#
# Produces dist\MacTask.exe with Python and pynput bundled inside, so people
# downloading it need nothing installed.
#
# Usage (PowerShell):   .\build_windows.ps1

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

python -m pip install --upgrade pip
python -m pip install pynput pyinstaller

# One file, no console window. The Cocoa front-end and its PyObjC imports are
# excluded: they are macOS-only and would just produce missing-module noise.
python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name MacTask `
    --exclude-module mactask_app `
    --exclude-module objc `
    --exclude-module AppKit `
    --exclude-module Foundation `
    --exclude-module Quartz `
    --exclude-module ApplicationServices `
    --exclude-module PyObjCTools `
    mactask_main.py

Write-Host ""
Write-Host "Done: $(Join-Path $PSScriptRoot 'dist\MacTask.exe')"
