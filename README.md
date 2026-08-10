# MacTask

A minimal macro recorder for **macOS and Windows** — a TinyTask-style tool that
records your mouse movement, clicks, scrolls, and keystrokes, then replays them
with **smooth, interpolated motion**. Click **Record**, do something, click
**Play**. Rebindable hotkeys (default **P** = record, **L** = stop), adjustable
speed, and looping.

> **In-memory only** — nothing is ever written to disk. The recording lives in
> RAM and is discarded when you quit.

The name is a leftover from when it was macOS-only; it runs on both now.

---

## Download

Grab the build for your machine from the
**[latest release](https://github.com/ankuang05/MacTask/releases/latest)** —
Python is bundled inside, so there is nothing to install.

| Your machine | Download | What you get |
|---|---|---|
| Windows 10 / 11 (64-bit) | `MacTask-Windows-x64.zip` | `MacTask.exe` — unzip, double-click |
| Mac with Apple Silicon (M1–M4) | `MacTask-macOS-AppleSilicon.zip` | `MacTask.app` — unzip, drag to Applications |
| Mac with Intel | `MacTask-macOS-Intel.zip` | `MacTask.app` — unzip, drag to Applications |

Not sure which Mac you have?  → **Apple menu → About This Mac**. "Apple M1/M2/M3/M4"
means Apple Silicon; "Intel" means the Intel build.

### First launch

The builds are unsigned (code-signing certificates cost money), so each OS
warns once:

- **Windows** — SmartScreen shows "Windows protected your PC". Click **More
  info → Run anyway**.
- **macOS** — Gatekeeper says the app "cannot be opened". **Right-click the app
  → Open → Open**, or allow it under **System Settings → Privacy & Security**.

### Then, on macOS only: grant permissions

macOS blocks input capture and control until you approve it. The app shows a
**Grant Access** button that opens the right pane. Enable MacTask in **both**:

- **System Settings → Privacy & Security → Accessibility**
- **System Settings → Privacy & Security → Input Monitoring**

The window detects the grant automatically — no restart needed. **Windows needs
none of this**; it starts working immediately.

---

## Using it

| Control            | What it does                                       |
|--------------------|----------------------------------------------------|
| **● Record**       | start recording (turns into **■ Stop**)            |
| **▶ Play**         | replay the recording (turns into **■ Stop**)       |
| **Clear**          | discard the current recording                      |
| **Speed**          | 0.25×–4× playback speed                            |
| **Loop**           | replay repeatedly until you stop                   |
| **Change**         | click, then press any key to rebind that hotkey    |

Default hotkeys: **P** record · **L** stop recording · **O** play · **K** stop
playback. Recording *appends* to what you already have; only **Clear** wipes it.

> While recording, the **Stop** hotkey ends the recording, so that key can't be
> captured as normal typing. Pick keys you won't need inside a macro, or use the
> buttons.

---

## Run from source

Works the same on both platforms:

```bash
git clone https://github.com/ankuang05/MacTask.git
cd MacTask
python3 -m pip install -r requirements.txt
python3 mactask_main.py
```

On Windows use `python` instead of `python3`. `mactask_main.py` picks the right
window for your OS; pass `--tk` to force the cross-platform one.

There's also a no-window CLI: `python3 mactask.py` (F9 record/stop, F10 play).

## Build it yourself

```bash
./build_macos.sh          # macOS  -> dist/MacTask.app (self-contained)
.\build_windows.ps1       # Windows -> dist\MacTask.exe (self-contained)
```

`build_app.sh` is a lighter macOS alternative that borrows the system Python
instead of bundling one. Pushing a `v*` tag runs
[`.github/workflows/release.yml`](.github/workflows/release.yml), which builds
all three downloads and attaches them to a GitHub release:

```bash
git tag v1.1.0 && git push origin v1.1.0
```

---

## How it works

MacTask runs as **two processes**:

- a **window** — `mactask_app.py` (native Cocoa via PyObjC) on macOS,
  `mactask_gui.py` (Tk) on Windows. Neither one touches input hooks.
- **`mactask_worker.py`** — the pynput record/replay engine, no GUI toolkit.

They talk over a pipe with line-delimited JSON (`worker_link.py`). This split is
deliberate: on macOS 15, pynput's keyboard mapping calls Text Input Source APIs
that must run on the main thread, which crashes (SIGTRAP) when pynput shares a
process with an AppKit run loop. Separating them avoids the conflict entirely —
and as a bonus, both windows drive the *same* engine over the same protocol, so
the two platforms can't drift apart on behaviour.

**Smooth motion:** the mouse path is captured at ~125 Hz and, on playback, the
cursor is *interpolated between samples at 120 fps* while clicks/keys fire at
their exact timestamps — so motion is fluid instead of stepping frame-to-frame.

---

## Requirements

Only if you're running from source — the downloads bundle all of this.

- macOS 11+ or Windows 10+
- Python 3.8+
- [`pynput`](https://pypi.org/project/pynput/)
- PyObjC (macOS only, for the native window and permission checks)

## Notes

- Coordinates are absolute screen pixels; replay assumes the same resolution and
  window layout as when you recorded.
- Keystrokes are held only in RAM while running — avoid recording passwords.
- On Windows, replaying into an app that runs as administrator requires MacTask
  to run as administrator too — Windows blocks synthetic input from a
  lower-privilege process.

## License

MIT — see [LICENSE](LICENSE).
