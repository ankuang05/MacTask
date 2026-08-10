#!/usr/bin/env python3
"""
MacTask - cross-platform window (Tk).

This is the front-end used on Windows (and anywhere that isn't macOS). It is a
thin view over `mactask_worker.py`: every bit of recording and playback logic
lives in the worker process, and this window only sends commands and renders
the state the worker reports. macOS has a native Cocoa front-end instead
(`mactask_app.py`), talking the exact same JSON protocol, so the two windows
can never drift apart on behaviour.

Everything is in-memory only: nothing is written to disk.

Run:  python mactask_gui.py
"""

import sys
import subprocess
import tkinter as tk
from tkinter import ttk

from worker_link import WorkerLink

IS_MAC = sys.platform == "darwin"

ACCESSIBILITY_URL = ("x-apple.systempreferences:com.apple.preference."
                     "security?Privacy_Accessibility")
INPUT_MONITORING_URL = ("x-apple.systempreferences:com.apple.preference."
                        "security?Privacy_ListenEvent")

POLL_MS = 80

RED = "#d9382f"
GREEN = "#1f8b4c"
SUBTLE = "#6b6b6b"
FAINT = "#909090"

HOTKEY_ROWS = [
    # label,        state key,       default, bind tag
    ("Record",      "record_key",    "p",     "record"),
    ("Stop rec",    "stop_key",      "l",     "stop"),
    ("Play",        "play_key",      "o",     "play"),
    ("Stop play",   "stop_play_key", "k",     "stopplay"),
]


class App:

    def __init__(self, root):
        self.root = root
        self.link = WorkerLink()
        self.link.start()
        self._binding_ui = None     # tag awaiting a keypress, or None
        self._bind_acked = False    # worker has echoed that request back

        root.title("MacTask")
        root.geometry("360x560")
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build()
        self._poll()

    # ------------------------------------------------------------ layout ----
    def _build(self):
        pad = {"padx": 18}
        outer = ttk.Frame(self.root, padding=(0, 14, 0, 10))
        outer.pack(fill="both", expand=True)

        # --- Status block ---
        self.status_lbl = tk.Label(outer, text="Ready",
                                   font=("TkDefaultFont", 22, "bold"))
        self.status_lbl.pack(**pad)
        self.count_lbl = tk.Label(outer, text="0 events", fg=SUBTLE,
                                  font=("TkDefaultFont", 10))
        self.count_lbl.pack(**pad)

        # --- Permission banner (macOS only; Windows needs no grant) ---
        self.perm_lbl = tk.Label(outer, text="", fg=RED,
                                 font=("TkDefaultFont", 10), wraplength=320)
        self.grant_btn = ttk.Button(outer, text="Grant Access",
                                    command=self._grant)

        # --- Primary controls ---
        row = ttk.Frame(outer)
        row.pack(fill="x", pady=(14, 0), **pad)
        self.record_btn = tk.Button(row, text="● Record", fg="white",
                                    bg=RED, activebackground=RED,
                                    activeforeground="white",
                                    relief="flat", height=2,
                                    command=self._record)
        self.record_btn.pack(side="left", expand=True, fill="x", padx=(0, 5))
        self.play_btn = tk.Button(row, text="▶ Play", fg="white",
                                  bg=GREEN, activebackground=GREEN,
                                  activeforeground="white",
                                  relief="flat", height=2,
                                  command=self._play)
        self.play_btn.pack(side="left", expand=True, fill="x", padx=(5, 0))

        self.clear_btn = ttk.Button(outer, text="Clear recording",
                                    command=lambda: self.link.send(cmd="clear"))
        self.clear_btn.pack(fill="x", pady=(8, 0), **pad)

        # --- Playback settings ---
        self._section(outer, "PLAYBACK", (18, 4))
        speed_row = ttk.Frame(outer)
        speed_row.pack(fill="x", **pad)
        tk.Label(speed_row, text="Speed", fg=SUBTLE).pack(side="left")
        self.speed_lbl = tk.Label(speed_row, text="1.00×", fg=SUBTLE,
                                  width=6, anchor="e")
        self.speed_lbl.pack(side="right")
        self.speed = tk.DoubleVar(value=1.0)
        ttk.Scale(speed_row, from_=0.25, to=4.0, variable=self.speed,
                  command=self._speed_changed).pack(
                      side="left", fill="x", expand=True, padx=8)

        self.loop = tk.BooleanVar(value=False)
        ttk.Checkbutton(outer, text="Loop until stopped", variable=self.loop,
                        command=self._sync_config).pack(
                            anchor="w", pady=(8, 0), **pad)

        # --- Hotkeys ---
        self._section(outer, "HOTKEYS", (18, 0))
        tk.Label(outer, text="Click Change, then press the new key.",
                 fg=FAINT, font=("TkDefaultFont", 9)).pack(anchor="w", **pad)

        self.key_badges = {}        # tag -> (badge widget, state key, default)
        self.change_btns = []
        for label, skey, default, tag in HOTKEY_ROWS:
            row = ttk.Frame(outer)
            row.pack(fill="x", pady=3, **pad)
            tk.Label(row, text=label, fg=SUBTLE, width=9,
                     anchor="w").pack(side="left")
            badge = tk.Label(row, text=default.upper(), width=7,
                             relief="solid", borderwidth=1,
                             font=("TkDefaultFont", 10, "bold"))
            badge.pack(side="left", padx=(0, 10), ipady=2)
            btn = ttk.Button(row, text="Change",
                             command=lambda t=tag: self._begin_bind(t))
            btn.pack(side="left", fill="x", expand=True)
            self.key_badges[tag] = (badge, skey, default)
            self.change_btns.append(btn)

        tk.Label(outer, text="In-memory only — nothing is saved to disk.",
                 fg=FAINT, font=("TkDefaultFont", 9)).pack(side="bottom",
                                                           pady=(10, 0))

    def _section(self, parent, text, pady):
        tk.Label(parent, text=text, fg=FAINT,
                 font=("TkDefaultFont", 9, "bold")).pack(anchor="w", padx=18,
                                                         pady=pady)

    # ----------------------------------------------------------- actions ----
    def _record(self):
        self.link.send(cmd="toggle_record")

    def _play(self):
        if self.link.state.get("playing"):
            self.link.send(cmd="stop_play")
        else:
            self.link.send(cmd="play", speed=self.speed.get(),
                           loop=self.loop.get())

    def _speed_changed(self, _value=None):
        self.speed_lbl.config(text=f"{self.speed.get():.2f}×")
        self._sync_config()

    def _sync_config(self):
        # Keep the worker's playback settings current so the Play *hotkey*
        # uses the slider/loop values shown in the window.
        self.link.send(cmd="config", speed=self.speed.get(),
                       loop=self.loop.get())

    def _begin_bind(self, tag):
        self._binding_ui = tag
        self.key_badges[tag][0].config(text="press…")
        self.link.send(cmd="bind", which=tag)

    def _grant(self):
        state = self.link.state
        need_ax = not state.get("trusted", False)
        url = ACCESSIBILITY_URL if need_ax else INPUT_MONITORING_URL
        try:
            subprocess.Popen(["open", url])
        except Exception:
            pass

    # ----------------------------------------------------------- refresh ----
    def _poll(self):
        s = self.link.state
        trusted = s.get("trusted", False)
        has_input = s.get("input", False)
        recording = s.get("recording", False)
        playing = s.get("playing", False)
        ready = trusted and has_input

        # Only macOS can be un-ready; elsewhere the worker always reports
        # granted and the banner never appears.
        if ready:
            self.perm_lbl.pack_forget()
            self.grant_btn.pack_forget()
        else:
            if not trusted and not has_input:
                msg = "Enable Accessibility + Input Monitoring"
            elif not trusted:
                msg = "Enable Accessibility (for mouse & playback)"
            else:
                msg = "Enable Input Monitoring (for keyboard)"
            self.perm_lbl.config(text=msg)
            self.perm_lbl.pack(padx=18, pady=(8, 4))
            self.grant_btn.pack(padx=60, fill="x")

        if recording:
            self.status_lbl.config(text="Recording", fg=RED)
            self.record_btn.config(text="■ Stop")
        elif playing:
            self.status_lbl.config(text="Playing", fg=GREEN)
            self.record_btn.config(text="● Record")
        else:
            self.status_lbl.config(text="Ready", fg="black")
            self.record_btn.config(text="● Record")

        # Play doubles as Stop while playing, so a loop can be interrupted.
        if playing:
            self.play_btn.config(text="■ Stop", bg=RED,
                                 activebackground=RED)
        else:
            self.play_btn.config(text="▶ Play", bg=GREEN,
                                 activebackground=GREEN)

        self.count_lbl.config(text=f"{s.get('count', 0)} events")

        # Recording needs both permissions; playback needs Accessibility only.
        self._enable(self.play_btn, trusted and not recording)
        self._enable(self.record_btn, ready and not playing)
        self.clear_btn.state(
            ["!disabled"] if ready and not (playing or recording)
            else ["disabled"])

        # The worker echoes a pending rebind back as `binding`. Until that echo
        # arrives the local request has to be trusted, or the very next poll
        # would wipe the "press…" prompt and leave no sign we're waiting.
        reported = s.get("binding")
        if reported == self._binding_ui:
            self._bind_acked = True
        if self._bind_acked and reported is None:
            self._binding_ui = None
            self._bind_acked = False
        pending = self._binding_ui or reported
        for tag, (badge, skey, default) in self.key_badges.items():
            badge.config(text="press…" if tag == pending
                         else str(s.get(skey, default)).upper())
        # Rebinding captures a keypress, which needs Input Monitoring.
        for btn in self.change_btns:
            btn.state(["!disabled"] if has_input else ["disabled"])

        self.root.after(POLL_MS, self._poll)

    @staticmethod
    def _enable(button, on):
        button.config(state="normal" if on else "disabled")

    # ---------------------------------------------------------- shutdown ----
    def _on_close(self):
        self.link.close()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
