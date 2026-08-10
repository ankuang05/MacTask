#!/usr/bin/env python3
"""
MacTask input worker.

Runs the pynput recording/playback engine in its OWN process (no AppKit), so it
never hits the macOS "Text Input Source on a background thread" crash that
happens when pynput shares a process with a Cocoa run loop.

Cross-platform: pynput drives input on macOS and Windows alike. The only
platform-specific part is permissions — macOS gates input capture behind
Accessibility + Input Monitoring, Windows gates nothing — so on Windows the
permission checks below simply report "granted".

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

IS_MAC = sys.platform == "darwin"

try:
    from ApplicationServices import AXIsProcessTrusted
except Exception:
    def AXIsProcessTrusted():
        return True     # not a macOS concept — nothing to grant

# Input Monitoring (a.k.a. "listen event access") is REQUIRED to capture
# keyboard events on macOS; mouse events work with Accessibility alone. This is
# a separate permission, so keyboard recording silently fails without it.
try:
    from Quartz import (CGPreflightListenEventAccess,
                        CGRequestListenEventAccess)
    HAVE_LISTEN_API = True
except Exception:
    HAVE_LISTEN_API = False

    def CGPreflightListenEventAccess():
        return True

    def CGRequestListenEventAccess():
        return True


def input_monitoring_ok():
    try:
        return bool(CGPreflightListenEventAccess())
    except Exception:
        return True

MOVE_INTERVAL = 0.008   # ~125 Hz mouse-move capture (finer than the eye needs)
PLAYBACK_FPS = 120      # cursor is interpolated at this rate for smooth motion


def key_repr(key):
    # Store the character when available, but ALWAYS keep the virtual keycode
    # (vk) too. On some layouts/input sources macOS returns no char for number
    # (and other) keys; the vk lets us still replay them by physical key.
    name = getattr(key, "name", None)      # only special Keys have .name
    vk = getattr(key, "vk", None)
    if name is not None:
        return {"key": name, "special": True, "vk": vk}
    char = getattr(key, "char", None)
    return {"key": char, "special": False, "vk": vk}


def key_label(key):
    char = getattr(key, "char", None)
    if char is not None and char.isprintable() and char.strip():
        return char.lower()
    return getattr(key, "name", None) or str(key)


class Recorder:
    def __init__(self):
        self.events = []
        self.start = None
        self._offset = 0.0        # time base so appended takes continue the timeline
        self._last_move = 0.0
        self.active = False

    def begin(self, append=False):
        # Appending continues the existing recording instead of overwriting it;
        # the buffer is only wiped by clear().
        if append and self.events:
            self._offset = self.events[-1]["t"] + 0.5   # small gap between takes
        else:
            self.events = []
            self._offset = 0.0
        self.start = time.perf_counter()
        self._last_move = 0.0
        self.active = True

    def stop(self):
        self.active = False

    def clear(self):
        self.events = []
        self._offset = 0.0

    def _add(self, e):
        e["t"] = round(self._offset + (time.perf_counter() - self.start), 4)
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
        if e.get("special"):
            return getattr(keyboard.Key, e["key"], None)
        # Prefer the physical key code (vk). Games like Roblox read the hardware
        # key code, not the character a synthetic event carries, so pressing by
        # vk makes the key actually register in games. Fall back to the char
        # only when no vk was captured (older/synthetic events).
        vk = e.get("vk")
        if vk is not None:
            try:
                return keyboard.KeyCode.from_vk(vk)
            except Exception:
                pass
        return e.get("key")

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
        self.play_key = "o"
        self.stop_play_key = "k"
        self._binding = None
        self.playing = False
        self._stop_play = threading.Event()
        self._quit = False
        self._mouse_started = False
        self._kb_started = False
        self._play_speed = 1.0
        self._play_loop = False
        self._lock = threading.Lock()

    # -------- input listeners (pynput) --------
    def start_mouse(self):
        self.mouse_listener = mouse.Listener(
            on_move=self.recorder.on_move,
            on_click=self.recorder.on_click,
            on_scroll=self.recorder.on_scroll)
        self.mouse_listener.start()
        self._mouse_started = True

    def start_keyboard(self):
        # Only start once Input Monitoring is granted, otherwise the tap is
        # created but receives no key events (the silent-keyboard bug).
        self.kb_listener = keyboard.Listener(
            on_press=self._on_press, on_release=self._on_release)
        self.kb_listener.start()
        self._kb_started = True

    def _on_press(self, key):
        if self._binding is not None:
            label = key_label(key)
            if self._binding == "record":
                self.record_key = label
            elif self._binding == "stop":
                self.stop_key = label
            elif self._binding == "play":
                self.play_key = label
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
                self.recorder.begin(append=True)
            elif label == self.play_key:
                self._start_play(self._play_speed, self._play_loop)

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
                if self.recorder.active:
                    self.recorder.stop()
                else:
                    self.recorder.begin(append=True)
        elif cmd == "record":
            if not self.playing and not self.recorder.active:
                self.recorder.begin(append=True)
        elif cmd == "stop":
            if self.recorder.active:
                self.recorder.stop()
        elif cmd == "clear":
            if not self.recorder.active and not self.playing:
                self.recorder.clear()
        elif cmd == "config":
            self._play_speed = float(msg.get("speed", self._play_speed))
            self._play_loop = bool(msg.get("loop", self._play_loop))
        elif cmd == "play":
            self._play_speed = float(msg.get("speed", self._play_speed))
            self._play_loop = bool(msg.get("loop", self._play_loop))
            self._start_play(self._play_speed, self._play_loop)
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
            "platform": sys.platform,
            "trusted": bool(AXIsProcessTrusted()),
            "input": input_monitoring_ok(),
            "recording": self.recorder.active,
            "playing": self.playing,
            "count": len(self.recorder.events),
            "record_key": self.record_key,
            "stop_key": self.stop_key,
            "play_key": self.play_key,
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
    # Frozen Windows builds get a block-buffered stdout by default, which would
    # stall state updates in the pipe until the buffer fills.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    worker = Worker()
    threading.Thread(target=stdin_reader, args=(worker,), daemon=True).start()

    # Trigger the Input Monitoring prompt up front so the app appears in the
    # System Settings list (needed for keyboard capture). macOS only.
    if IS_MAC:
        try:
            CGRequestListenEventAccess()
        except Exception:
            pass

    out = sys.stdout
    while not worker._quit:
        # Mouse capture needs Accessibility; keyboard capture needs Input
        # Monitoring. Start each listener only once its permission is granted.
        if AXIsProcessTrusted() and not worker._mouse_started:
            try:
                worker.start_mouse()
            except Exception:
                pass
        if input_monitoring_ok() and not worker._kb_started:
            try:
                worker.start_keyboard()
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
