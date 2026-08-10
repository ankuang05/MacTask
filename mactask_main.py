#!/usr/bin/env python3
"""
MacTask entry point.

One script that picks the right piece to run, so the packaged builds on macOS
and Windows can share a single PyInstaller target:

    mactask_main.py            -> the window (Cocoa on macOS, Tk elsewhere)
    mactask_main.py --tk       -> force the Tk window
    mactask_main.py --worker   -> run the input worker instead

The `--worker` mode exists because a frozen build has no loose .py files to
spawn: `worker_link.py` re-launches this same executable with that flag.
"""

import sys


def _run_worker():
    import mactask_worker
    mactask_worker.main()


def _run_gui():
    if sys.platform == "darwin" and "--tk" not in sys.argv:
        try:
            import mactask_app
        except ImportError:
            pass            # PyObjC missing — fall through to the Tk window
        else:
            return mactask_app.main()
    import mactask_gui
    mactask_gui.main()


def main():
    if "--worker" in sys.argv:
        _run_worker()
    else:
        _run_gui()


if __name__ == "__main__":
    main()
