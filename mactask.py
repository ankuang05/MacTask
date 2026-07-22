#!/usr/bin/env python3
"""
MacTask - a TinyTask-style macro recorder for macOS (in-memory only).

Records mouse movement, clicks, scrolls, and keyboard input, then replays it.
Everything lives in RAM: nothing is written to disk, and the moment you quit
the program the recording is gone. No files, no history, no persistence.

Run it:
    python3 mactask.py

Hotkeys:
    F9   start / stop recording
    F10  replay what you just recorded
    ESC  quit (recording is discarded)

Requires: pynput  (pip install pynput)
macOS needs Accessibility + Input Monitoring permissions - see README.
"""

import sys
import time

try:
    from pynput import mouse, keyboard
except ImportError:
    sys.exit(
        "Missing dependency 'pynput'.\n"
        "Install it with:  python3 -m pip install pynput"
    )

# Control keys that drive the app - never recorded into the macro.
REC_KEY = "f9"
PLAY_KEY = "f10"
QUIT_KEY = "esc"
MOVE_INTERVAL = 0.03  # min seconds between recorded mouse-move samples


def key_repr(key):
    """Serialize a pynput key into a small dict (char vs special key)."""
    char = getattr(key, "char", None)
    if char is not None:
        return {"key": char, "special": False}
    name = getattr(key, "name", None)
    if name is not None:
        return {"key": name, "special": True}
    return {"key": str(key), "special": True}


class Recorder:
    """Captures mouse + keyboard events into an in-memory list."""

    def __init__(self):
        self.events = []
        self.start = None
        self._last_move = 0.0
        self.active = False

    def begin(self):
        self.events = []          # drop anything from before - no memory kept
        self.start = time.perf_counter()
        self._last_move = 0.0
        self.active = True

    def stop(self):
        self.active = False

    def _add(self, event):
        event["t"] = round(time.perf_counter() - self.start, 4)
        self.events.append(event)

    # --- mouse ---
    def on_move(self, x, y):
        if not self.active:
            return
        now = time.perf_counter()
        if now - self._last_move < MOVE_INTERVAL:
            return
        self._last_move = now
        self._add({"type": "move", "x": x, "y": y})

    def on_click(self, x, y, button, pressed):
        if not self.active:
            return
        self._add({"type": "click", "x": x, "y": y,
                   "button": button.name, "pressed": pressed})

    def on_scroll(self, x, y, dx, dy):
        if not self.active:
            return
        self._add({"type": "scroll", "x": x, "y": y, "dx": dx, "dy": dy})

    # --- keyboard (fed from the main listener, control keys filtered out) ---
    def on_key(self, key, action):
        if not self.active:
            return
        self._add({"type": "key", "action": action, **key_repr(key)})


class Player:
    """Replays an in-memory event list, preserving original timing."""

    _BUTTONS = {
        "left": mouse.Button.left,
        "right": mouse.Button.right,
        "middle": mouse.Button.middle,
    }

    def __init__(self, events):
        self.events = events
        self.mouse = mouse.Controller()
        self.kb = keyboard.Controller()

    def _resolve_key(self, event):
        if event["special"]:
            return getattr(keyboard.Key, event["key"], None)
        return event["key"]

    def _dispatch(self, event):
        etype = event["type"]
        if etype == "move":
            self.mouse.position = (event["x"], event["y"])
        elif etype == "click":
            self.mouse.position = (event["x"], event["y"])
            btn = self._BUTTONS.get(event["button"], mouse.Button.left)
            (self.mouse.press if event["pressed"] else self.mouse.release)(btn)
        elif etype == "scroll":
            self.mouse.scroll(event["dx"], event["dy"])
        elif etype == "key":
            key = self._resolve_key(event)
            if key is None:
                return
            (self.kb.press if event["action"] == "press" else self.kb.release)(key)

    def play(self):
        prev_t = 0.0
        for event in self.events:
            delay = event["t"] - prev_t
            if delay > 0:
                time.sleep(delay)
            prev_t = event["t"]
            self._dispatch(event)


def main():
    print(
        "MacTask - in-memory macro recorder (nothing is saved to disk)\n"
        f"  {REC_KEY.upper()}   start / stop recording\n"
        f"  {PLAY_KEY.upper()}  replay the recording\n"
        f"  {QUIT_KEY.upper()}  quit (recording is discarded)\n"
    )

    recorder = Recorder()
    mouse_listener = mouse.Listener(
        on_move=recorder.on_move,
        on_click=recorder.on_click,
        on_scroll=recorder.on_scroll,
    )
    mouse_listener.start()

    def toggle_record():
        if recorder.active:
            recorder.stop()
            print(f"[REC] Stopped - {len(recorder.events)} events in memory.")
        else:
            recorder.begin()
            print(f"[REC] Recording... press {REC_KEY.upper()} again to stop.")

    def replay():
        if recorder.active:
            print("Stop recording first (F9).")
            return
        if not recorder.events:
            print("Nothing recorded yet.")
            return
        print("[PLAY] Replaying in 1s...")
        time.sleep(1.0)
        Player(recorder.events).play()
        print("[PLAY] Done.")

    def on_press(key):
        name = getattr(key, "name", None)
        if name == REC_KEY:
            toggle_record()
            return
        if recorder.active:
            recorder.on_key(key, "press")
            return
        if name == PLAY_KEY:
            replay()
        elif name == QUIT_KEY:
            print("Quitting - recording discarded.")
            return False

    def on_release(key):
        if recorder.active and getattr(key, "name", None) != REC_KEY:
            recorder.on_key(key, "release")

    with keyboard.Listener(on_press=on_press, on_release=on_release) as kl:
        kl.join()

    mouse_listener.stop()


if __name__ == "__main__":
    main()
