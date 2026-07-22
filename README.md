# MacTask

A minimal, native **macro recorder for macOS** — a TinyTask-style tool that
records your mouse movement, clicks, scrolls, and keystrokes, then replays them
with **smooth, interpolated motion**. Click **Record**, do something, click
**Play**. Rebindable hotkeys (default **P** = record, **L** = stop), adjustable
speed, and looping.

> **In-memory only** — nothing is ever written to disk. The recording lives in
> RAM and is discarded when you quit.

---

## Download & run

### Option A — build the app bundle (double-clickable)

```bash
git clone https://github.com/<you>/MacTask.git
cd MacTask
./build_app.sh
open dist/MacTask.app
```

`build_app.sh` produces `dist/MacTask.app`. Drag it to **/Applications** if you
like. It runs on macOS's built-in Python (PyObjC is already included; the one
extra dependency, `pynput`, is auto-installed to your user site on first launch).

### Option B — run the script directly

```bash
python3 -m pip install -r requirements.txt
python3 mactask_app.py
```

There's also a no-window CLI: `python3 mactask.py` (F9 record/stop, F10 play).

---

## Grant permissions (required)

macOS blocks input capture/control until you approve it. The app shows a
**Grant Accessibility Access** button that opens the right pane. Enable MacTask
(or your terminal, if you ran the script) in **both**:

- **System Settings → Privacy & Security → Accessibility**
- **System Settings → Privacy & Security → Input Monitoring**

The window detects the grant automatically — no restart needed.

---

## Using it

| Control            | What it does                                       |
|--------------------|----------------------------------------------------|
| **● Record**       | start recording (turns into **■ Stop**)            |
| **▶ Play**         | replay the recording                               |
| **Clear**          | discard the current recording                      |
| **Speed**          | 0.25×–4× playback speed                            |
| **Loop**           | replay repeatedly until you stop                   |
| **Record / Stop hotkeys** | click, then press any key to rebind         |

You can also trigger recording from the keyboard: **P** to start, **L** to stop.

> While recording, the **Stop** hotkey ends the recording, so that key can't be
> captured as normal typing. Pick keys you won't need inside a macro, or use the
> buttons.

---

## How it works

MacTask runs as **two processes**:

- **`mactask_app.py`** — the Cocoa window (PyObjC), no input hooks.
- **`mactask_worker.py`** — the pynput record/replay engine, no AppKit.

They talk over a pipe with line-delimited JSON. This split is deliberate: on
macOS 15, pynput's keyboard mapping calls Text Input Source APIs that must run
on the main thread, which crashes (SIGTRAP) when pynput shares a process with an
AppKit run loop. Separating them avoids the conflict entirely.

**Smooth motion:** the mouse path is captured at ~125 Hz and, on playback, the
cursor is *interpolated between samples at 120 fps* while clicks/keys fire at
their exact timestamps — so motion is fluid instead of stepping frame-to-frame.

---

## Requirements

- macOS 11+
- Python 3.8+ (the system `python3` is fine)
- [`pynput`](https://pypi.org/project/pynput/) (auto-installed by the app)
- PyObjC (ships with macOS Python)

## Notes

- Coordinates are absolute screen pixels; replay assumes the same resolution and
  window layout as when you recorded.
- Keystrokes are held only in RAM while running — avoid recording passwords.

## License

MIT — see [LICENSE](LICENSE).
