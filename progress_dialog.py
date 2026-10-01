"""A small modal window showing a long task's progress -- writing a G-code
file, loading a large one -- while the task itself runs on a worker thread, so
the app keeps responding, the user can see it's still working, and can cancel.
"""
import threading
import time
import tkinter as tk
from tkinter import ttk
import ttkbootstrap as tb


class Cancelled(Exception):
    """The user cancelled the task."""


def _duration(seconds):
    if seconds < 60:
        return f"{max(1, round(seconds))} s"
    return f"{round(seconds / 60)} min"


class ProgressTask:
    """Runs work(progress) on a worker thread behind a progress window.

    `work` reports through progress(fraction=None, phase=None): fraction is
    0-1 of the whole task (None while unknown: the bar then just keeps
    moving), phase a short text of what's happening. progress() raises
    Cancelled once the user has cancelled, so the work stops at its next
    report. `work` must not touch Tk -- only the callbacks run on the GUI
    thread:
      on_done(result)  -- may return (heading, detail): the window then stays
                          open showing that until closed; otherwise it closes
      on_error(exc)    -- after the window has closed
      on_cancel()      -- after the window has closed (optional)
    `finishing` is shown while on_done runs, for when that takes a moment
    itself (on the GUI thread, which can't update the window meanwhile).
    """

    POLL_MS = 50

    def __init__(self, app, title, work, on_done, on_error, on_cancel=None, finishing=None):
        self.app = app
        self._finishing = finishing
        self._work, self._on_done, self._on_error, self._on_cancel = work, on_done, on_error, on_cancel
        self._cancel = threading.Event()
        self._report = (None, "Starting…")  # latest (fraction, phase), written by the worker
        self._outcome = None                # ("done", result) / ("error", exc) / ("cancelled", None)
        self._started = time.monotonic()
        self._finished = False
        self._build(title)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self.win.after(self.POLL_MS, self._poll)

    # --- Window ---

    def _build(self, title):
        root = self.app.root
        self.win = tb.Toplevel(title=title, resizable=(False, False), iconphoto=None)
        self.win.withdraw()
        self.win.transient(root)
        self.win.protocol("WM_DELETE_WINDOW", self._close_or_cancel)
        self.win.bind("<Escape>", lambda e: self._close_or_cancel())
        f = ttk.Frame(self.win, padding=(20, 16))
        f.pack(fill=tk.BOTH, expand=True)
        f.columnconfigure(0, weight=1)
        ttk.Label(f, text=title, style="PageTitle.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        self.phase = ttk.Label(f, text="Starting…")
        self.phase.grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 6))
        self.bar = tb.Progressbar(f, length=420, maximum=1000, bootstyle="primary")
        self.bar.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.detail = ttk.Label(f, text="", style="Settings.TLabel", wraplength=420, justify="left")
        self.detail.grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.button = ttk.Button(f, text="Cancel", style="primary.Outline.TButton", width=10,
                                 command=self._close_or_cancel)
        self.button.grid(row=3, column=1, sticky="e", pady=(10, 0), padx=(12, 0))
        # Shown once Tk is idle: placing it needs its size, and working that out
        # on the spot would first run whatever the main window has pending
        # (e.g. a redraw) before the task could even start.
        self.win.after_idle(self._place)

    def _place(self):
        # Centered over the main window, which stays visible behind it.
        root = self.app.root
        try:
            self.win.update_idletasks()
            x = root.winfo_rootx() + (root.winfo_width() - self.win.winfo_reqwidth()) // 2
            y = root.winfo_rooty() + (root.winfo_height() - self.win.winfo_reqheight()) // 3
            self.win.geometry(f"+{max(0, x)}+{max(0, y)}")
            self.win.deiconify()
            self.win.lift()
            self.win.grab_set()  # the settings can't change under a task that uses them
            self.button.focus_set()
        except tk.TclError:
            pass  # already closed (a task that was done at once)

    def _show(self, fraction, phase):
        if phase:
            self.phase.configure(text=phase)
        if fraction is None:
            if str(self.bar.cget("mode")) != "indeterminate":
                self.bar.configure(mode="indeterminate")
                self.bar.start(15)
            self.detail.configure(text="")
            return
        if str(self.bar.cget("mode")) != "determinate":
            self.bar.stop()
            self.bar.configure(mode="determinate")
        fraction = min(1.0, max(0.0, fraction))
        self.bar.configure(value=fraction * 1000)
        text = f"{fraction * 100:.0f} %"
        elapsed = time.monotonic() - self._started
        if 0.02 <= fraction < 1 and elapsed > 1.0:
            text += f"  ·  about {_duration(elapsed * (1 - fraction) / fraction)} left"
        self.detail.configure(text=text)

    def _close_or_cancel(self):
        if self._finished:
            self._close()
        elif not self._cancel.is_set():
            self._cancel.set()
            self.button.configure(text="Cancelling…", state="disabled")

    def _close(self):
        try:
            self.win.grab_release()
            self.win.destroy()
        except tk.TclError:
            pass

    # --- Worker ---

    def _progress(self, fraction=None, phase=None):
        # Called by the work on the worker thread.
        if self._cancel.is_set():
            raise Cancelled()
        previous = self._report
        self._report = (fraction, phase or previous[1])

    def _run(self):
        try:
            self._outcome = ("done", self._work(self._progress))
        except Cancelled:
            self._outcome = ("cancelled", None)
        except Exception as e:  # handed to on_error on the GUI thread
            self._outcome = ("error", e)

    def _poll(self):
        outcome = self._outcome
        if outcome is None:
            self._show(*self._report)
            self.win.after(self.POLL_MS, self._poll)
            return
        self._finished = True
        kind, value = outcome
        if kind == "done":
            if self._finishing:
                self.button.configure(state="disabled")
                self.phase.configure(text=self._finishing)
                self.detail.configure(text="")
                self.win.update_idletasks()  # paint it before on_done takes the GUI thread
            shown = self._on_done(value)
            if shown:
                heading, detail = shown
                self.bar.stop()
                self.bar.configure(mode="determinate", value=1000, bootstyle="success")
                self.phase.configure(text=heading)
                self.detail.configure(text=detail)
                self.button.configure(text="Close", style="primary.TButton", state="normal")
                self.win.bind("<Return>", lambda e: self._close())
                self.button.focus_set()
            else:
                self._close()
        elif kind == "error":
            self._close()
            self._on_error(value)
        else:
            self._close()
            if self._on_cancel is not None:
                self._on_cancel()
