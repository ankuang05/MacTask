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
            "trusted": False, "recording": False, "playing": False,
            "count": 0, "record_key": "p", "stop_key": "l",
            "stop_play_key": "k", "binding": None,
        }
        self._binding_ui = None
        self._build_window()
        self._start_worker()
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.08, self, b"refresh:", None, True)
        return self

    # -------------------------------------------------- window --------------
    def _build_window(self):
        W, H = 360, 520
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
        self.status_lbl = _label("Ready", 24, H - 62, W - 48, 30,
                                 size=24, weight="bold", center=True)
        cv.addSubview_(self.status_lbl)
        self.count_lbl = _label("0 events", 24, H - 84, W - 48, 16,
                                size=12, color=subtle, center=True)
        cv.addSubview_(self.count_lbl)

        # --- Permission banner ---
        self.perm_lbl = _label("", 24, H - 110, W - 48, 16, size=12,
                               center=True)
        cv.addSubview_(self.perm_lbl)
        self.grant_btn = _button("Grant Accessibility Access", 60, H - 144,
                                 240, 30, self, b"grantClicked:")
        cv.addSubview_(self.grant_btn)

        # --- Primary controls ---
        self.record_btn = _button("● Record", 24, H - 196, 150, 40,
                                  self, b"recordClicked:",
                                  tint=NSColor.systemRedColor())
        cv.addSubview_(self.record_btn)
        self.play_btn = _button("▶ Play", 186, H - 196, 150, 40,
                                self, b"playClicked:",
                                tint=NSColor.systemGreenColor())
        cv.addSubview_(self.play_btn)

        self.clear_btn = _button("Clear recording", 24, H - 240, W - 48, 30,
                                 self, b"clearClicked:")
        cv.addSubview_(self.clear_btn)

        # --- Playback settings ---
        cv.addSubview_(_label("PLAYBACK", 24, H - 282, W - 48, 14,
                              size=11, weight="semibold", color=faint))
        cv.addSubview_(_label("Speed", 24, H - 308, 46, 20, color=subtle))
        self.speed = NSSlider.alloc().initWithFrame_(
            NSMakeRect(72, H - 310, 200, 22))
        self.speed.setMinValue_(0.25)
        self.speed.setMaxValue_(4.0)
        self.speed.setFloatValue_(1.0)
        self.speed.setTarget_(self)
        self.speed.setAction_(b"speedChanged:")
        cv.addSubview_(self.speed)
        self.speed_lbl = _label("1.00×", 280, H - 308, 56, 20, color=subtle)
        cv.addSubview_(self.speed_lbl)

        self.loop_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(22, H - 338, 200, 22))
        self.loop_btn.setButtonType_(3)  # checkbox
        self.loop_btn.setTitle_(" Loop until stopped")
        self.loop_btn.setFont_(NSFont.systemFontOfSize_(13))
        cv.addSubview_(self.loop_btn)

        # --- Hotkeys ---
        cv.addSubview_(_label("HOTKEYS", 24, H - 380, W - 48, 14,
                              size=11, weight="semibold", color=faint))
        cv.addSubview_(_label("Record", 24, H - 406, 60, 22, color=subtle))
        self.rec_key_btn = _button("P", 92, H - 408, 90, 26,
                                   self, b"bindRecord:")
        cv.addSubview_(self.rec_key_btn)
        cv.addSubview_(_label("Stop", 196, H - 406, 46, 22, color=subtle))
        self.stop_key_btn = _button("L", 246, H - 408, 90, 26,
                                    self, b"bindStop:")
        cv.addSubview_(self.stop_key_btn)

        cv.addSubview_(_label("Stop playback", 24, H - 440, 110, 22,
                              color=subtle))
        self.stopplay_key_btn = _button("K", 140, H - 442, 90, 26,
                                        self, b"bindStopPlay:")
        cv.addSubview_(self.stopplay_key_btn)

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

    def bindRecord_(self, sender):
        self._binding_ui = "record"
        self.rec_key_btn.setTitle_("press…")
        self._send(cmd="bind", which="record")

    def bindStop_(self, sender):
        self._binding_ui = "stop"
        self.stop_key_btn.setTitle_("press…")
        self._send(cmd="bind", which="stop")

    def bindStopPlay_(self, sender):
        self._binding_ui = "stopplay"
        self.stopplay_key_btn.setTitle_("press…")
        self._send(cmd="bind", which="stopplay")

    def grantClicked_(self, sender):
        if HAVE_AX_PROMPT:
            try:
                AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
            except Exception:
                pass
        try:
            NSWorkspace.sharedWorkspace().openURL_(
                NSURL.URLWithString_(ACCESSIBILITY_URL))
        except Exception:
            pass

    # -------------------------------------------------- refresh -------------
    def refresh_(self, timer):
        s = self.state
        trusted = s.get("trusted", False)
        recording = s.get("recording", False)
        playing = s.get("playing", False)

        if trusted:
            self.perm_lbl.setStringValue_("")
            self.grant_btn.setHidden_(True)
        else:
            self.perm_lbl.setTextColor_(NSColor.systemRedColor())
            self.perm_lbl.setStringValue_(
                "Needs Accessibility permission to run")
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

        # Play/Stop is usable during playback so you can stop a loop.
        self.play_btn.setEnabled_(trusted and not recording)
        self.record_btn.setEnabled_(trusted and not playing)
        self.clear_btn.setEnabled_(trusted and not (playing or recording))

        if s.get("binding") is None:
            self._binding_ui = None
        if self._binding_ui != "record":
            self.rec_key_btn.setTitle_(s.get("record_key", "p").upper())
        if self._binding_ui != "stop":
            self.stop_key_btn.setTitle_(s.get("stop_key", "l").upper())
        if self._binding_ui != "stopplay":
            self.stopplay_key_btn.setTitle_(s.get("stop_play_key", "k").upper())

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
