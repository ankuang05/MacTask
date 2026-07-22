#!/usr/bin/env python3
"""
MacTask input worker.

Runs the pynput recording/playback engine in its OWN process (no AppKit), so it
never hits the macOS "Text Input Source on a background thread" crash that
happens when pynput shares a process with a Cocoa run loop.

Protocol (newline-delimited JSON over stdin/stdout):
  GUI -> worker (stdin):
    {"cmd": "toggle_record"}
    {"cmd": "record"} / {"cmd": "stop"}
    {"cmd": "play", "speed": 1.0, "loop": false}
    {"cmd": "stop_play"}
    {"cmd": "clear"}
    {"cmd": "bind", "which": "record"|"stop"}
    {"cmd": "quit"}
  worker -> GUI (stdout), ~10x/sec:
    {"type": "state", "trusted": bool, "recording": bool, "playing": bool,
     "count": int, "record_key": "p", "stop_key": "l", "binding": null|"record"}

Everything is in-memory only; nothing is written to disk.
"""

import sys
import json
import time
import bisect
import threading

from pynput import mouse, keyboard

try:
    from ApplicationServices import AXIsProcessTrusted
except Exception:
    def AXIsProcessTrusted():
        return True

MOVE_INTERVAL = 0.008   # ~125 Hz mouse-move capture (finer than the eye needs)
PLAYBACK_FPS = 120      # cursor is interpolated at this rate for smooth motion


def key_repr(key):
    char = getattr(key, "char", None)
    if char is not None:
        return {"key": char, "special": False}
    name = getattr(key, "name", None)
    if name is not None:
        return {"key": name, "special": True}
    return {"key": str(key), "special": True}


def key_label(key):
    char = getattr(key, "char", None)
    if char is not None and char.isprintable() and char.strip():
        return char.lower()
    return getattr(key, "name", None) or str(key)


class Recorder:
    def __init__(self):
        self.events = []
        self.start = None
        self._last_move = 0.0
        self.active = False

    def begin(self):
        self.events = []
        self.start = time.perf_counter()
        self._last_move = 0.0
        self.active = True

    def stop(self):
        self.active = False

    def _add(self, e):
        e["t"] = round(time.perf_counter() - self.start, 4)
        self.events.append(e)

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

    def on_key(self, key, action):
        if not self.active:
            return
        self._add({"type": "key", "action": action, **key_repr(key)})


class Player:
    """Smooth playback: the cursor is *interpolated* between recorded move
    samples at a high frame rate, while clicks/keys/scrolls fire at their exact
    timestamps. This keeps motion fluid even if capture was coarse."""

    _BUTTONS = {"left": mouse.Button.left, "right": mouse.Button.right,
                "middle": mouse.Button.middle}

    def __init__(self, events, speed=1.0, fps=PLAYBACK_FPS):
        self.speed = max(speed, 0.05)
        self.dt = 1.0 / max(fps, 1)
        self.mouse = mouse.Controller()
        self.kb = keyboard.Controller()
        # Separate the continuous mouse path from discrete actions.
        self.move_t = []
        self.move_xy = []
        self.actions = []
        for e in events:
            if e["type"] == "move":
                self.move_t.append(e["t"])
                self.move_xy.append((e["x"], e["y"]))
            else:
                self.actions.append(e)
        self.actions.sort(key=lambda e: e["t"])
        self.total = events[-1]["t"] if events else 0.0

    def _resolve_key(self, e):
        if e["special"]:
            return getattr(keyboard.Key, e["key"], None)
        return e["key"]

    def _pos_at(self, t):
        mt = self.move_t
        if not mt:
            return None
        if t <= mt[0]:
            return self.move_xy[0]
        if t >= mt[-1]:
            return self.move_xy[-1]
        i = bisect.bisect_right(mt, t)
        t0, t1 = mt[i - 1], mt[i]
        (x0, y0), (x1, y1) = self.move_xy[i - 1], self.move_xy[i]
        frac = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
        return (x0 + (x1 - x0) * frac, y0 + (y1 - y0) * frac)

    def _dispatch(self, e):
        t = e["type"]
        if t == "click":
            self.mouse.position = (e["x"], e["y"])
            btn = self._BUTTONS.get(e["button"], mouse.Button.left)
            (self.mouse.press if e["pressed"] else self.mouse.release)(btn)
        elif t == "scroll":
            self.mouse.scroll(e["dx"], e["dy"])
        elif t == "key":
            key = self._resolve_key(e)
            if key is None:
                return
            (self.kb.press if e["action"] == "press" else self.kb.release)(key)

    def play_once(self, should_stop):
        start = time.perf_counter()
        n = len(self.actions)
        ai = 0
        while True:
            if should_stop():
                return
            elapsed = (time.perf_counter() - start) * self.speed
            # Fire any discrete actions that are now due.
            while ai < n and self.actions[ai]["t"] <= elapsed:
                self._dispatch(self.actions[ai])
                ai += 1
            # Glide the cursor to its interpolated position.
            pos = self._pos_at(elapsed)
            if pos is not None:
                self.mouse.position = (int(round(pos[0])), int(round(pos[1])))
            if elapsed >= self.total and ai >= n:
                break
            time.sleep(self.dt)


class Worker:
    def __init__(self):
        self.recorder = Recorder()
        self.record_key = "p"
        self.stop_key = "l"
        self.stop_play_key = "k"
        self._binding = None
        self.playing = False
        self._stop_play = threading.Event()
        self._quit = False
        self._listeners_started = False
        self._play_speed = 1.0
        self._play_loop = False
        self._lock = threading.Lock()

    # -------- input listeners (pynput) --------
    def start_listeners(self):
        self.mouse_listener = mouse.Listener(
            on_move=self.recorder.on_move,
            on_click=self.recorder.on_click,
            on_scroll=self.recorder.on_scroll)
        self.kb_listener = keyboard.Listener(
            on_press=self._on_press, on_release=self._on_release)
        self.mouse_listener.start()
        self.kb_listener.start()
        self._listeners_started = True

    def _on_press(self, key):
        if self._binding is not None:
            label = key_label(key)
            if self._binding == "record":
                self.record_key = label
            elif self._binding == "stop":
                self.stop_key = label
            elif self._binding == "stopplay":
                self.stop_play_key = label
            self._binding = None
            return
        if self.playing:
            # The only hotkey that works mid-playback is "stop playback".
            if key_label(key) == self.stop_play_key:
                self._stop_play.set()
            return
        label = key_label(key)
        if self.recorder.active:
            if label == self.stop_key:
                self.recorder.stop()
                return
            self.recorder.on_key(key, "press")
        else:
            if label == self.record_key:
                self.recorder.begin()

    def _on_release(self, key):
        if self.playing or self._binding is not None:
            return
        if self.recorder.active and key_label(key) != self.stop_key:
            self.recorder.on_key(key, "release")

    # -------- commands from GUI --------
    def handle(self, msg):
        cmd = msg.get("cmd")
        if cmd == "toggle_record":
            if not self.playing:
                (self.recorder.stop if self.recorder.active
                 else self.recorder.begin)()
        elif cmd == "record":
            if not self.playing and not self.recorder.active:
                self.recorder.begin()
        elif cmd == "stop":
            if self.recorder.active:
                self.recorder.stop()
        elif cmd == "clear":
            if not self.recorder.active and not self.playing:
                self.recorder.events = []
        elif cmd == "play":
            self._start_play(msg.get("speed", 1.0), bool(msg.get("loop")))
        elif cmd == "stop_play":
            self._stop_play.set()
        elif cmd == "bind":
            self._binding = msg.get("which")
        elif cmd == "quit":
            self._quit = True

    def _start_play(self, speed, loop):
        if self.playing or self.recorder.active or not self.recorder.events:
            return
        self.playing = True
        self._play_speed = speed
        self._play_loop = loop
        self._stop_play.clear()
        threading.Thread(target=self._play_worker, daemon=True).start()

    def _play_worker(self):
        events = list(self.recorder.events)
        player = Player(events, speed=self._play_speed)
        try:
            while not self._stop_play.is_set():
                player.play_once(self._stop_play.is_set)
                if not self._play_loop:
                    break
        finally:
            self.playing = False

    # -------- state emit --------
    def state(self):
        return {
            "type": "state",
            "trusted": bool(AXIsProcessTrusted()),
            "recording": self.recorder.active,
            "playing": self.playing,
            "count": len(self.recorder.events),
            "record_key": self.record_key,
            "stop_key": self.stop_key,
            "stop_play_key": self.stop_play_key,
            "binding": self._binding,
        }


def stdin_reader(worker):
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            worker.handle(json.loads(line))
        except Exception:
            pass


def main():
    worker = Worker()
    threading.Thread(target=stdin_reader, args=(worker,), daemon=True).start()

    out = sys.stdout
    while not worker._quit:
        if AXIsProcessTrusted() and not worker._listeners_started:
            try:
                worker.start_listeners()
            except Exception:
                pass
        try:
            out.write(json.dumps(worker.state()) + "\n")
            out.flush()
        except Exception:
            break
        time.sleep(0.1)


if __name__ == "__main__":
    main()
