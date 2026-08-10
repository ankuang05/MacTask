#!/usr/bin/env python3
"""
Launcher + JSON pipe for the MacTask input worker.

Both front-ends use this: the Cocoa window on macOS (`mactask_app.py`) and the
Tk window everywhere else (`mactask_gui.py`). It deliberately imports nothing
from pynput or AppKit — the entire reason the worker is a separate process is
that the GUI process must never load the input engine.

Protocol is newline-delimited JSON; see mactask_worker.py for the message set.
"""

import os
import sys
import json
import subprocess
import threading

IS_WINDOWS = sys.platform.startswith("win")

# Keep a console window from flashing behind the GUI on Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if IS_WINDOWS else 0


def worker_command():
    """The command that starts the worker process."""
    if getattr(sys, "frozen", False):
        # Under PyInstaller there is no separate .py to run and sys.executable
        # is the bundled app itself, so re-launch it with a flag that
        # mactask_main.py routes straight to the worker.
        return [sys.executable, "--worker"]
    here = os.path.dirname(os.path.abspath(__file__))
    return [sys.executable, os.path.join(here, "mactask_worker.py")]


class WorkerLink:
    """Owns the worker process and the latest state it reported."""

    DEFAULT_STATE = {
        "trusted": False, "input": False, "recording": False, "playing": False,
        "count": 0, "record_key": "p", "stop_key": "l", "play_key": "o",
        "stop_play_key": "k", "binding": None,
    }

    def __init__(self):
        # Replaced wholesale by the reader thread; readers always see a
        # consistent snapshot without needing a lock.
        self.state = dict(self.DEFAULT_STATE)
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen(
            worker_command(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            bufsize=1, universal_newlines=True,
            creationflags=_NO_WINDOW)
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("type") == "state":
                self.state = msg

    def send(self, **msg):
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except Exception:
            pass

    def close(self):
        try:
            self.send(cmd="quit")
            self.proc.terminate()
        except Exception:
            pass
