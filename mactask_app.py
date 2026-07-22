#!/usr/bin/env python3
"""
MacTask - a native macOS macro recorder (TinyTask-style).

A clean Cocoa window (PyObjC) with Record / Play / Clear, a speed slider, a loop
toggle, and rebindable hotkeys (default: P = record, L = stop).

The input engine (pynput) runs in a SEPARATE process (mactask_worker.py). Mixing
pynput with an AppKit run loop crashes on macOS 15 (Text Input Source APIs must
run on the main thread); splitting them into two processes avoids that entirely.

Everything is in-memory only: nothing is written to disk.

Run:  python3 mactask_app.py
"""

import os
import sys
import json
import threading
import subprocess

try:
    import objc
    from Foundation import NSObject, NSTimer, NSMakeRect, NSURL
    from AppKit import (
        NSApplication, NSWindow, NSButton, NSTextField, NSSlider, NSFont,
        NSColor, NSApp, NSWorkspace,
    )
    from PyObjCTools import AppHelper
except ImportError:
    sys.exit("PyObjC not available. This app needs macOS's Cocoa bindings.")

try:
    from ApplicationServices import (
        AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt)
    HAVE_AX_PROMPT = True
except Exception:
    HAVE_AX_PROMPT = False

# Cocoa constants
NSWindowStyleMaskTitled = 1 << 0
NSWindowStyleMaskClosable = 1 << 1
NSWindowStyleMaskMiniaturizable = 1 << 2
NSBackingStoreBuffered = 2
NSApplicationActivationPolicyRegular = 0
NSTextAlignmentCenter = 1
NSControlStateValueOn = 1

ACCESSIBILITY_URL = ("x-apple.systempreferences:com.apple.preference."
                     "security?Privacy_Accessibility")
INPUT_MONITORING_URL = ("x-apple.systempreferences:com.apple.preference."
                        "security?Privacy_ListenEvent")
WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "mactask_worker.py")


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def _label(text, x, y, w, h, size=13, weight="regular", color=None,
           center=False):
    lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
    lbl.setStringValue_(text)
    lbl.setBezeled_(False)
    lbl.setDrawsBackground_(False)
    lbl.setEditable_(False)
    lbl.setSelectable_(False)
    if weight == "bold":
        lbl.setFont_(NSFont.boldSystemFontOfSize_(size))
    elif weight == "semibold":
        try:
            lbl.setFont_(NSFont.systemFontOfSize_weight_(size, 0.3))
        except Exception:
            lbl.setFont_(NSFont.boldSystemFontOfSize_(size))
    else:
        lbl.setFont_(NSFont.systemFontOfSize_(size))
    if color is not None:
        lbl.setTextColor_(color)
    if center:
        lbl.setAlignment_(NSTextAlignmentCenter)
    return lbl


def _tint(btn, color):
    try:
        btn.setBezelColor_(color)
    except Exception:
        pass


def _badge(text, x, y, w, h):
    f = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
    f.setStringValue_(text)
    f.setEditable_(False)
    f.setSelectable_(False)
    f.setBezeled_(True)
    f.setDrawsBackground_(True)
    f.setAlignment_(NSTextAlignmentCenter)
    f.setFont_(NSFont.boldSystemFontOfSize_(13))
    return f


def _button(title, x, y, w, h, target, action, tint=None):
    b = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
    b.setTitle_(title)
    b.setBezelStyle_(1)  # rounded
    b.setTarget_(target)
    b.setAction_(action)
    b.setFont_(NSFont.systemFontOfSize_(13))
    if tint is not None:
        _tint(b, tint)
    return b


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class Controller(NSObject):

    def init(self):
        self = objc.super(Controller, self).init()
        if self is None:
            return None
        self.state = {
            "trusted": False, "input": False, "recording": False,
            "playing": False, "count": 0, "record_key": "p", "stop_key": "l",
            "play_key": "o", "stop_play_key": "k", "binding": None,
        }
        self._binding_ui = None
        self._build_window()
        self._start_worker()
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.08, self, b"refresh:", None, True)
        return self

    # -------------------------------------------------- window --------------
    def _build_window(self):
        W, H = 360, 600
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskMiniaturizable)
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H), style, NSBackingStoreBuffered, False)
        win.setTitle_("MacTask")
        win.center()
        win.setDelegate_(self)
        self.window = win
        cv = win.contentView()

        subtle = NSColor.secondaryLabelColor()
        faint = NSColor.tertiaryLabelColor()

        # --- Status block ---
        self.status_lbl = _label("Ready", 24, H - 56, W - 48, 30,
                                 size=24, weight="bold", center=True)
        cv.addSubview_(self.status_lbl)
        self.count_lbl = _label("0 events", 24, H - 80, W - 48, 16,
                                size=12, color=subtle, center=True)
        cv.addSubview_(self.count_lbl)

        # --- Permission banner ---
        self.perm_lbl = _label("", 24, H - 106, W - 48, 16, size=12,
                               center=True)
        cv.addSubview_(self.perm_lbl)
        self.grant_btn = _button("Grant Access", 60, H - 140, 240, 30,
                                 self, b"grantClicked:")
        cv.addSubview_(self.grant_btn)

        # --- Primary controls ---
        self.record_btn = _button("● Record", 24, H - 192, 150, 40,
                                  self, b"recordClicked:",
                                  tint=NSColor.systemRedColor())
        cv.addSubview_(self.record_btn)
        self.play_btn = _button("▶ Play", 186, H - 192, 150, 40,
                                self, b"playClicked:",
                                tint=NSColor.systemGreenColor())
        cv.addSubview_(self.play_btn)

        self.clear_btn = _button("Clear recording", 24, H - 236, W - 48, 30,
                                 self, b"clearClicked:")
        cv.addSubview_(self.clear_btn)

        # --- Playback settings ---
        cv.addSubview_(_label("PLAYBACK", 24, H - 278, W - 48, 14,
                              size=11, weight="semibold", color=faint))
        cv.addSubview_(_label("Speed", 24, H - 304, 46, 20, color=subtle))
        self.speed = NSSlider.alloc().initWithFrame_(
            NSMakeRect(72, H - 306, 200, 22))
        self.speed.setMinValue_(0.25)
        self.speed.setMaxValue_(4.0)
        self.speed.setFloatValue_(1.0)
        self.speed.setTarget_(self)
        self.speed.setAction_(b"speedChanged:")
        cv.addSubview_(self.speed)
        self.speed_lbl = _label("1.00×", 280, H - 304, 56, 20, color=subtle)
        cv.addSubview_(self.speed_lbl)

        self.loop_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(22, H - 334, 220, 22))
        self.loop_btn.setButtonType_(3)  # checkbox
        self.loop_btn.setTitle_(" Loop until stopped")
        self.loop_btn.setFont_(NSFont.systemFontOfSize_(13))
        self.loop_btn.setTarget_(self)
        self.loop_btn.setAction_(b"loopChanged:")
        cv.addSubview_(self.loop_btn)

        # --- Hotkeys ---
        cv.addSubview_(_label("HOTKEYS", 24, H - 376, W - 48, 14,
                              size=11, weight="semibold", color=faint))
        cv.addSubview_(_label("Click Change, then press the new key.",
                              24, H - 396, W - 48, 14, size=11, color=faint))

        rows = [
            ("Record",    "record_key",    "p", b"bindRecord:",   "record"),
            ("Stop rec",  "stop_key",      "l", b"bindStop:",     "stop"),
            ("Play",      "play_key",      "o", b"bindPlay:",     "play"),
            ("Stop play", "stop_play_key", "k", b"bindStopPlay:", "stopplay"),
        ]
        self.key_badges = {}      # tag -> (badge, state_key, default)
        self.change_btns = []
        y0 = H - 424
        for i, (name, skey, default, action, tag) in enumerate(rows):
            y = y0 - i * 34
            cv.addSubview_(_label(name, 24, y, 86, 24, color=subtle))
            badge = _badge(default.upper(), 116, y, 60, 24)
            cv.addSubview_(badge)
            self.key_badges[tag] = (badge, skey, default)
            btn = _button("Change", 186, y - 1, 150, 26, self, action)
            cv.addSubview_(btn)
            self.change_btns.append(btn)

        cv.addSubview_(_label("In-memory only — nothing is saved to disk.",
                              24, 18, W - 48, 16, size=11, color=faint,
                              center=True))
        win.makeKeyAndOrderFront_(None)

    # -------------------------------------------------- worker IPC ----------
    def _start_worker(self):
        self.proc = subprocess.Popen(
            [sys.executable, WORKER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            bufsize=1, universal_newlines=True)
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                if msg.get("type") == "state":
                    self.state = msg
            except Exception:
                pass

    def _send(self, **msg):
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except Exception:
            pass

    # -------------------------------------------------- actions -------------
    def recordClicked_(self, sender):
        self._send(cmd="toggle_record")

    def playClicked_(self, sender):
        if self.state.get("playing"):
            self._send(cmd="stop_play")
        else:
            self._send(cmd="play", speed=self.speed.floatValue(),
                       loop=self.loop_btn.state() == NSControlStateValueOn)

    def clearClicked_(self, sender):
        self._send(cmd="clear")

    def speedChanged_(self, sender):
        self.speed_lbl.setStringValue_(f"{self.speed.floatValue():.2f}×")
        self._sync_config()

    def loopChanged_(self, sender):
        self._sync_config()

    def _sync_config(self):
        # Keep the worker's playback settings in sync so the Play hotkey uses
        # the current slider/loop values.
        self._send(cmd="config", speed=self.speed.floatValue(),
                   loop=self.loop_btn.state() == NSControlStateValueOn)

    def _begin_bind(self, tag):
        self._binding_ui = tag
        badge = self.key_badges[tag][0]
        badge.setStringValue_("press…")
        self._send(cmd="bind", which=tag)

    def bindRecord_(self, sender):
        self._begin_bind("record")

    def bindStop_(self, sender):
        self._begin_bind("stop")

    def bindPlay_(self, sender):
        self._begin_bind("play")

    def bindStopPlay_(self, sender):
        self._begin_bind("stopplay")

    def grantClicked_(self, sender):
        need_ax = not self.state.get("trusted", False)
        need_input = not self.state.get("input", False)
        if need_ax and HAVE_AX_PROMPT:
            try:
                AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
            except Exception:
                pass
        # Open whichever pane is still missing (Accessibility first).
        url = ACCESSIBILITY_URL if need_ax else INPUT_MONITORING_URL
        if not need_ax and not need_input:
            url = ACCESSIBILITY_URL
        try:
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url))
        except Exception:
            pass

    # -------------------------------------------------- refresh -------------
    def refresh_(self, timer):
        s = self.state
        trusted = s.get("trusted", False)
        has_input = s.get("input", False)
        recording = s.get("recording", False)
        playing = s.get("playing", False)
        ready = trusted and has_input

        if ready:
            self.perm_lbl.setStringValue_("")
            self.grant_btn.setHidden_(True)
        else:
            if not trusted and not has_input:
                msg = "Enable Accessibility + Input Monitoring"
            elif not trusted:
                msg = "Enable Accessibility (for mouse & playback)"
            else:
                msg = "Enable Input Monitoring (for keyboard)"
            self.perm_lbl.setTextColor_(NSColor.systemRedColor())
            self.perm_lbl.setStringValue_(msg)
            self.grant_btn.setHidden_(False)

        if recording:
            self.status_lbl.setStringValue_("Recording")
            self.status_lbl.setTextColor_(NSColor.systemRedColor())
            self.record_btn.setTitle_("■ Stop")
        elif playing:
            self.status_lbl.setStringValue_("Playing")
            self.status_lbl.setTextColor_(NSColor.systemGreenColor())
            self.record_btn.setTitle_("● Record")
        else:
            self.status_lbl.setStringValue_("Ready")
            self.status_lbl.setTextColor_(NSColor.labelColor())
            self.record_btn.setTitle_("● Record")

        # Play button doubles as a Stop button while playing.
        if playing:
            self.play_btn.setTitle_("■ Stop")
            _tint(self.play_btn,NSColor.systemRedColor())
        else:
            self.play_btn.setTitle_("▶ Play")
            _tint(self.play_btn,NSColor.systemGreenColor())

        self.count_lbl.setStringValue_(f"{s.get('count', 0)} events")

        # Recording needs both permissions; playback needs Accessibility.
        # Play/Stop stays usable during playback so you can stop a loop.
        self.play_btn.setEnabled_(trusted and not recording)
        self.record_btn.setEnabled_(ready and not playing)
        self.clear_btn.setEnabled_(ready and not (playing or recording))

        if s.get("binding") is None:
            self._binding_ui = None
        for tag, (badge, skey, default) in self.key_badges.items():
            if self._binding_ui != tag:
                badge.setStringValue_(s.get(skey, default).upper())
        # Rebinding captures a keypress, which needs Input Monitoring.
        for btn in self.change_btns:
            btn.setEnabled_(has_input)

    # -------------------------------------------------- shutdown ------------
    def windowWillClose_(self, notification):
        try:
            self._send(cmd="quit")
            self.proc.terminate()
        except Exception:
            pass
        NSApp.terminate_(self)


def main():
    if not os.path.exists(WORKER):
        sys.exit(f"Worker not found: {WORKER}")
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    Controller.alloc().init()
    app.activateIgnoringOtherApps_(True)
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
