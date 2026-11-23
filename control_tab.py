"""The Control tab: the winding machine, live.

On the left: the connection, jogging and homing, and the tank calibration
(the laser on the X carriage finds where the tank's straight section really
begins and ends, and the Plan's Chuck X-Offset and Straight Length are worked
out from that). On the right: the job running on the machine -- pause, resume,
cancel, emergency stop -- and the live view of its file, which can be
scrubbed back and forth while the red LIVE line keeps marking where the
machine is.

Nothing here is needed by a running wind (see machine_link): the tab only
watches, and only sends what the user clicks.
"""
import os
import re
import tempfile
import time
import tkinter as tk
from tkinter import ttk, messagebox
import machine_link
import theme
import winding
from machine_link import Link, MachineStatus
from plan_tab import format_hms
from progress_dialog import ProgressTask
from ttkbootstrap.widgets.tooltip import ToolTip
from view_tab import ViewTab, read_gcode, SETUP

# Where files running on the machine are kept once downloaded for the live view.
CACHE_DIR = os.path.join(tempfile.gettempdir(), "cfrp_winder_machine_files")
TICK_MS = 100           # how often the GUI picks up the connection's news
LOAD_RETRY_SECONDS = 5.0
JOG_STEPS = (0.1, 1.0, 10.0, 100.0)
CAL_STEPS = (-10.0, -1.0, -0.1, 0.1, 1.0, 10.0)  # the Calibration panel's X moves (mm)
MAX_QUEUED_JOGS = 3     # clicks beyond this, while the machine is still busy, are dropped
LEFT_WIDTH = 330


def _tip(text, *widgets):
    # The same hover explanations as on the settings page.
    for widget in widgets:
        ToolTip(widget, text=text, wraplength=300, delay=400)


def _fmt_step(value):
    return f"{value:g}"


class _Segmented:
    """A row of buttons of which one is selected (the jog step). Plain Tk
    buttons with colors set directly, like the G-Code Preview's 3D toggle:
    ttkbootstrap's Toolbutton styles render unreliably."""

    def __init__(self, parent, app, values, var, fmt, tip):
        self.app, self.var = app, var
        self.frame = ttk.Frame(parent)
        self.buttons = {}
        for value in values:
            b = tk.Button(self.frame, text=fmt(value), relief="flat", bd=1, padx=7, pady=1, cursor="hand2",
                          font=("TkDefaultFont", 8, "bold"), command=lambda v=value: self._pick(v))
            b.pack(side=tk.LEFT, padx=(0, 2))
            self.buttons[value] = b
            _tip(tip, b)
        self.restyle()

    def set_enabled(self, enabled):
        for b in self.buttons.values():
            b.configure(state="normal" if enabled else "disabled")

    def _pick(self, value):
        self.var.set(value)
        self.restyle()

    def restyle(self):
        colors = self.app.root.style.colors
        for value, b in self.buttons.items():
            on = abs(value - self.var.get()) < 1e-9
            bg, fg = (colors.primary, "white") if on else theme.OFF_TOGGLE[self.app.theme_mode]
            b.configure(bg=bg, fg=fg, activebackground=bg, activeforeground=fg,
                        disabledforeground=self.app.canvas_palette["auto_text"])


class LiveView(ViewTab):
    """The G-Code Preview, for the file running on the machine: it follows
    the machine (LIVE) until it's scrubbed, played or jumped elsewhere, and
    the timeline is a bar with a red line where the machine is. Unlike the
    Plan's preview it never touches the settings: it only shows."""

    BAR_HEIGHT = 34

    def __init__(self, parent, app):
        self.follow = True
        self.live_index = None
        self._own_write = False
        self._last_index = 0
        self.visible = False
        super().__init__(parent, app)
        self.timeline_var.trace_add("write", self._on_timeline_moved)

    def setup_ui(self):
        super().setup_ui()
        controls = self.timeline_slider.master
        # The plain slider is replaced by the bar (it stays, hidden, bound to
        # the timeline as the preview expects).
        self.timeline_slider.pack_forget()
        self.bar = tk.Canvas(self.parent, height=self.BAR_HEIGHT, highlightthickness=0, bd=0, cursor="hand2")
        self.bar.pack(fill=tk.X, pady=(6, 0), before=controls)
        self.bar.bind("<Configure>", lambda e: self.draw_bar())
        self.bar.bind("<Button-1>", self._scrub)
        self.bar.bind("<B1-Motion>", self._scrub)
        self.live_btn = tk.Button(controls, text="● LIVE", command=self.go_live, relief="flat", bd=1, padx=10, pady=3,
                                  cursor="hand2", font=("TkDefaultFont", 9, "bold"))
        self.live_btn.pack(side=tk.LEFT, padx=(0, 10), before=self.rotate_toggle_btn)
        _tip("Back to where the machine is now: the view follows it again. Greyed out while you look elsewhere "
             "in the program (scrubbing, playing, jumping).", self.live_btn)
        _tip("The whole program. Filled: wound so far. The red line is where the machine is; click or drag "
             "anywhere to look at the program there, past or future.", self.bar)
        self._update_live_button()

    # --- Following the machine ---

    def _on_timeline_moved(self, *args):
        try:
            index = self.timeline_var.get()
        except tk.TclError:
            return
        if not self._own_write and index != self._last_index and self.follow:
            self.follow = False
            self._update_live_button()
        self._last_index = index
        self.draw_bar()

    def _set_index(self, index):
        self._own_write = True
        try:
            self.timeline_var.set(index)
        finally:
            self._own_write = False

    def set_live(self, index):
        """Where the machine is: a move index, or None when not known."""
        if index == self.live_index:
            return
        self.live_index = index
        self._update_live_button()
        if self.follow and index is not None and self.gcode_commands:
            self._set_index(index)
            if self.visible:
                self.redraw_view()
        elif self.visible:
            self.draw_bar()

    def go_live(self):
        if self.live_index is None:
            return
        self.stop_play()
        self.follow = True
        self._set_index(self.live_index)
        self._update_live_button()
        self.redraw_view()

    def _update_live_button(self):
        palette, off = self.app.canvas_palette, theme.OFF_TOGGLE[self.app.theme_mode][0]
        if self.live_index is None:
            bg, fg, state = off, palette["auto_text"], "disabled"
        elif self.follow:
            bg, fg, state = theme.LIVE_RED, "white", "normal"
        else:
            bg, fg, state = off, palette["muted"], "normal"
        self.live_btn.configure(bg=bg, fg=fg, activebackground=bg, activeforeground=fg, state=state,
                                disabledforeground=fg)

    def _scrub(self, event):
        n = len(self.gcode_commands)
        if n < 2:
            return
        w = max(1, self.bar.winfo_width() - 2 * self._BAR_PAD)
        index = round((event.x - self._BAR_PAD) / w * (n - 1))
        self.stop_play()
        self.timeline_var.set(max(0, min(n - 1, index)))
        self.redraw_view()

    # --- Files ---

    def _load(self, data):
        self._own_write = True
        try:
            super()._load(data)
        finally:
            self._own_write = False
        self._last_index = 0

    def show_file(self, label, data):
        """Shows a file read by read_gcode() -- without restoring its settings."""
        self.stop_play()
        self.gcode_path_var.set(label)
        self._load(data)
        self.live_index = None
        self.follow = True
        self._update_live_button()
        self.redraw_view()

    def clear(self, text):
        self.stop_play()
        self.gcode_commands, self.data = [], None
        self.cmd_cycles, self.cmd_times, self.cmd_layups, self.cycle_starts = [], [], [], {}
        self.gcode_path_var.set(text)
        self.layup_var.set("")
        self.live_index = None
        self.follow = True
        self._update_live_button()
        self.redraw_view()

    # --- Drawing ---

    _BAR_PAD = 10

    def redraw_view(self):
        super().redraw_view()
        if not self.gcode_commands:
            self.coord_var.set("X:   –  Y:  –  A:        –")
            self.time_at_pos_var.set("--:--:--")
        self.draw_bar()

    def reapply_viewport_colors(self):
        super().reapply_viewport_colors()
        self._update_live_button()
        self.draw_bar()

    def draw_bar(self):
        c = self.bar
        c.delete("all")
        colors = self.app.root.style.colors
        c.configure(bg=colors.bg)
        w, h = c.winfo_width(), self.BAR_HEIGHT
        if w < 50:
            return
        pad, n = self._BAR_PAD, len(self.gcode_commands)
        x0, x1 = pad, w - pad
        ty = h - 11  # the track's center
        c.create_rectangle(x0, ty - 4, x1, ty + 4, fill=theme.OFF_TOGGLE[self.app.theme_mode][0], outline=colors.border)
        if n < 2:
            return
        x_of = lambda i: x0 + (x1 - x0) * i / (n - 1)
        # Layup boundaries, as small ticks under the track.
        layups = self.cmd_layups
        for i in range(1, n):
            if layups[i] != layups[i - 1] and layups[i] != SETUP:
                x = x_of(i)
                c.create_line(x, ty + 5, x, ty + 9, fill=self.app.canvas_palette["muted"])
        live = self.live_index
        if live is not None:
            c.create_rectangle(x0, ty - 4, x_of(live), ty + 4, fill=colors.primary, outline="")
        try:
            view = min(n - 1, max(0, self.timeline_var.get()))
        except tk.TclError:
            view = 0
        xv = x_of(view)
        c.create_oval(xv - 6, ty - 6, xv + 6, ty + 6, fill=colors.bg, outline=colors.fg, width=2)
        if live is not None:
            xl = x_of(live)
            c.create_line(xl, 12, xl, h - 2, fill=theme.LIVE_RED, width=2)
            anchor = "w" if xl < x0 + 20 else "e" if xl > x1 - 20 else "center"
            c.create_text(xl, 6, text="LIVE", fill=theme.LIVE_RED, font=("TkDefaultFont", 7, "bold"), anchor=anchor)


class ControlTab:
    def __init__(self, parent, app):
        self.parent, self.app = parent, app
        self.link = Link()
        self.address_var = tk.StringVar(value=machine_link.DEFAULT_ADDRESS)
        self.step_var = tk.DoubleVar(value=10.0)
        self.laser_offset_var = tk.DoubleVar(value=machine_link.LASER_OFFSET_X)
        # Files this app sent (remote name -> local path): the live view reads
        # them from here instead of downloading them again.
        self.local_copies = {}
        self._last = None
        self._live_name = None   # the file the live view shows (or is loading)
        self._live = None        # (commands, times, offsets) of that file, once loaded
        self._retry_at = 0.0
        self._laser_on = False
        self._cal = None         # the tank calibration under way (see _cal_*)
        # The endless A rotation (a file the machine runs, see machine_link):
        # {feed, direction, state} while there is one -- state "starting" (being
        # sent), "printing", "paused" or "stopping" -- worked out from the
        # machine's status with every update (so it's there for one started
        # elsewhere, too), else None.
        self._rotation = None
        self._rot_phase = None   # "starting" / "stopping" until the machine shows it
        self._rot_request = None  # (feed, direction) being started
        self.rotate_feed_var = tk.DoubleVar(value=machine_link.ROTATE_FEED)
        self._rotate_dir = 1
        self._busy_note = ""
        self._build()
        self._apply(*self.link.snapshot())
        self.app.root.after(TICK_MS, self._tick)

    # --- Layout ---

    def _build(self):
        LBL, FRM, PAD = "Settings.TLabel", "Settings.TLabelframe", (8, 6)
        root = self.parent
        left = ttk.Frame(root, width=LEFT_WIDTH)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))
        left.pack_propagate(False)
        right = ttk.Frame(root)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._muted = []  # small grey texts, recolored with the theme

        def muted(parent, text="", **kw):
            label = ttk.Label(parent, text=text, style=LBL, wraplength=LEFT_WIDTH - 30, justify="left", **kw)
            self._muted.append(label)
            return label

        # 1. Connection
        conn = ttk.LabelFrame(left, text="Machine Connection", padding=PAD, style=FRM)
        conn.pack(fill=tk.X, pady=(0, 6))
        conn.columnconfigure(1, weight=1)
        addr_label = ttk.Label(conn, text="Address", style=LBL)
        addr_label.grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.addr_entry = ttk.Entry(conn, textvariable=self.address_var, style="Settings.TEntry",
                                    font=("TkDefaultFont", 8))
        self.addr_entry.grid(row=0, column=1, sticky="ew")
        self.addr_entry.bind("<Return>", lambda e: self._toggle_connection())
        self.connect_btn = ttk.Button(conn, text="Connect", width=11, style="primary.TButton",
                                      command=self._toggle_connection)
        self.connect_btn.grid(row=0, column=2, sticky="e", padx=(6, 0))
        status_row = ttk.Frame(conn)
        status_row.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        self.conn_dot = tk.Label(status_row, text="●", font=("TkDefaultFont", 10), bd=0, padx=0)
        self.conn_dot.pack(side=tk.LEFT)
        self.conn_text = ttk.Label(status_row, text="", style=LBL)
        self.conn_text.pack(side=tk.LEFT, padx=(4, 0))
        self.conn_detail = muted(conn)
        self.conn_detail.grid(row=2, column=0, columnspan=3, sticky="w")
        _tip("The winding machine's network address (Mainsail's). A port can be added (e.g. "
             "192.168.0.199:7125 for Moonraker's own). Press Enter to connect.", addr_label, self.addr_entry)
        _tip("Connects to the machine, or disconnects. Connecting only watches: a running wind never depends on "
             "this PC -- it can disconnect, crash or connect again at any time, and other PCs can watch at once.",
             self.connect_btn)

        # 2. Manual Control
        jog = ttk.LabelFrame(left, text="Manual Control", padding=PAD, style=FRM)
        jog.pack(fill=tk.X, pady=(0, 6))
        for col in (1, 2):
            jog.columnconfigure(col, weight=1, uniform="jog")
        step_label = ttk.Label(jog, text="Step", style=LBL)
        step_label.grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.steps = _Segmented(jog, self.app, JOG_STEPS, self.step_var, _fmt_step,
                                "How far each jog click moves: mm for X and Y, degrees for A.")
        self.steps.frame.grid(row=0, column=1, columnspan=2, sticky="w", pady=(0, 4))
        _tip("How far each jog click moves: mm for X and Y, degrees for A.", step_label)
        self.position_var = tk.StringVar(value="X –   Y –")
        self.jog_buttons, self.home_buttons = [], []
        rows = (("X", "◀  X−", "X+  ▶", "Along the tank: X− toward the chuck (X0), X+ away from it."),
                ("Y", "Y−  away", "Y+  in", "The eye's distance to the tank: Y− pulls it back (Y0 is fully "
                                            "retracted), Y+ moves it in toward the tank. Mind the tank!"),
                ("A", "⟲  A−", "A+  ⟳", "Turns the mandrel by the step in degrees. Not while a wind is "
                                        "paused: the fiber is attached."))
        for r, (axis, minus, plus, tip) in enumerate(rows, start=1):
            label = ttk.Label(jog, text=axis, style="SettingsHeader.TLabel")
            label.grid(row=r, column=0, sticky="w")
            for col, (text, sign) in enumerate(((minus, -1), (plus, 1)), start=1):
                b = ttk.Button(jog, text=text, style="primary.Outline.TButton",
                               command=lambda a=axis, s=sign: self._jog(a, s))
                b.grid(row=r, column=col, sticky="ew", padx=(0 if col == 1 else 3, 0), pady=1)
                self.jog_buttons.append((axis, b))
                _tip(tip, b)
            _tip(tip, label)
        homes = ttk.Frame(jog)
        homes.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        homes.columnconfigure((0, 1, 2), weight=1, uniform="home")
        for col, (text, axes, tip) in enumerate((
                ("Home X", "X", "Homes X only (G28 X). With Y homed, Y is pulled fully back first."),
                ("Home Y", "Y", "Homes Y only (G28 Y)."),
                ("Home All", "", "Homes every axis (G28), as a wind with homing does."))):
            b = ttk.Button(homes, text=text, style="primary.Outline.TButton", command=lambda a=axes: self._home(a))
            b.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 3, 0))
            self.home_buttons.append(b)
            _tip(tip, b)
        extras = ttk.Frame(jog)
        extras.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(3, 0))
        extras.columnconfigure(0, weight=2, uniform="extras")
        extras.columnconfigure(1, weight=1, uniform="extras")
        self.retract_btn = ttk.Button(extras, text="Retract Y fully (Y0)", style="primary.Outline.TButton",
                                      command=self._retract)
        self.retract_btn.grid(row=0, column=0, sticky="ew")
        _tip("Pulls the eye all the way back from the tank (Y0).", self.retract_btn)
        self.laser_btn = tk.Button(extras, text="Laser", relief="flat", bd=1, padx=6, pady=5, cursor="hand2",
                                   font=("TkDefaultFont", 9, "bold"), command=self._toggle_laser)
        self.laser_btn.grid(row=0, column=1, sticky="nsew", padx=(3, 0))
        _tip("Turns the laser on the X carriage on or off (LASER_ON / LASER_OFF). The machine doesn't report "
             "the laser's state: this shows what was sent last.", self.laser_btn)
        motors = ttk.Frame(jog)
        motors.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(3, 0))
        motors.columnconfigure((0, 1), weight=1, uniform="motors")
        self.motors_btn = ttk.Button(motors, text="Motors Off (All)", style="primary.Outline.TButton",
                                     command=lambda: self._motors_off(xy_only=False))
        self.motors_btn.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        _tip("Turns every motor off (M84): the carriage and the mandrel can then be moved by hand. The axes "
             "need homing again afterwards.", self.motors_btn)
        self.xy_off_btn = ttk.Button(motors, text="X/Y Off (keep A)", style="primary.Outline.TButton",
                                     command=lambda: self._motors_off(xy_only=True))
        self.xy_off_btn.grid(row=0, column=1, sticky="ew", padx=(3, 0))
        _tip("Turns only the X and Y motors off (SET_STEPPER_ENABLE ... ENABLE=0 for "
             + " and ".join(machine_link.XY_STEPPERS) + "): the carriage can be pushed by hand while the "
             "mandrel stays held. X and Y need homing again afterwards.", self.xy_off_btn)
        pos = ttk.Label(jog, textvariable=self.position_var, font=("TkFixedFont", 9))
        pos.grid(row=7, column=0, columnspan=3, sticky="w", pady=(6, 0))
        _tip("Where the carriage is (X, Y in mm), and which axes are homed.", pos)
        self.jog_note = muted(jog)
        self.jog_note.grid(row=8, column=0, columnspan=3, sticky="w")

        # 3. Rotate: A on and on, until stopped.
        rot = ttk.LabelFrame(left, text="Rotate", padding=PAD, style=FRM)
        rot.pack(fill=tk.X, pady=(0, 6))
        rot.columnconfigure(1, weight=1)
        feed_label = ttk.Label(rot, text="Feed (°/min)", style=LBL)
        feed_label.grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.rotate_feed_entry = ttk.Entry(rot, textvariable=self.rotate_feed_var, width=8, style="Settings.TEntry",
                                           font=("TkDefaultFont", 8))
        self.rotate_feed_entry.grid(row=0, column=1, sticky="w")
        _tip("How fast the mandrel turns, as an A feed rate in degrees per minute (5500 = 15.3 rpm).",
             feed_label, self.rotate_feed_entry)
        self.rotate_dir_btn = tk.Button(rot, relief="flat", bd=1, padx=8, pady=2, cursor="hand2",
                                        font=("TkDefaultFont", 9, "bold"), command=self._flip_rotation)
        self.rotate_dir_btn.grid(row=0, column=2, sticky="e")
        _tip("The direction it turns: A+ or A−. Click to flip it (while stopped).", self.rotate_dir_btn)
        self.rotate_btn = ttk.Button(rot, text="▶  Start Rotation", style="success.TButton",
                                     command=self._toggle_rotation)
        self.rotate_btn.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 0), ipady=2)
        _tip("Starts turning the mandrel on and on (A declared 0 first, with G92 A0), or stops it. It runs as a "
             "file on the machine, like a wind: Pause / Resume above, in Mainsail or on the machine work on it, "
             "and it keeps turning if this PC drops out (for up to " + f"{machine_link.ROTATE_HOURS} h). "
             "It stops within a couple of seconds, as a wind pauses. Everything else is locked while it turns.",
             self.rotate_btn)
        self.rotate_note = muted(rot)
        self.rotate_note.grid(row=2, column=0, columnspan=3, sticky="w")

        # 4. Calibration: start it, move X onto each edge, confirm (or abort).
        cal = ttk.LabelFrame(left, text="Calibration", padding=PAD, style=FRM)
        cal.pack(fill=tk.X, pady=(0, 6))
        cal.columnconfigure(0, weight=1)
        self.cal_btn = ttk.Button(cal, text="Start Tank Calibration", style="info.TButton", command=self._cal_start)
        self.cal_btn.grid(row=0, column=0, sticky="ew", ipady=2)
        _tip("Measures where the tank really is: pulls Y fully back, turns the laser on and moves to where the "
             "Plan expects the straight section to begin, then to where it expects it to end. At each, move X "
             "with the buttons below until the laser dot sits exactly on the edge, and confirm. Chuck X-Offset "
             "and Straight Length are then worked out from the two points and entered on the Plan tab (in cyan, "
             "as calibrated). Manual Control is locked meanwhile.", self.cal_btn)
        self.cal_step = ttk.Label(cal, text="", style=LBL, wraplength=LEFT_WIDTH - 30, justify="left")
        self.cal_step.grid(row=1, column=0, sticky="w", pady=(4, 0))
        moves = ttk.Frame(cal)
        moves.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        self.cal_jog_buttons = []
        for col, distance in enumerate(CAL_STEPS):
            moves.columnconfigure(col, weight=1, uniform="calmove")
            text = f"{distance:+g}".replace("-", "−")
            b = ttk.Button(moves, text=text, style="primary.Outline.TButton", padding=(2, 4),
                           command=lambda d=distance: self._cal_jog(d))
            b.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 2, 0))
            self.cal_jog_buttons.append(b)
            _tip(f"Moves X by {distance:+g} mm, to bring the laser dot onto the edge. Only while calibrating.", b)
        cal_buttons = ttk.Frame(cal)
        cal_buttons.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        cal_buttons.columnconfigure((0, 1), weight=1, uniform="cal")
        self.confirm_btn = ttk.Button(cal_buttons, text="✓  Confirm Point", style="success.TButton",
                                      command=self._cal_confirm)
        self.confirm_btn.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        self.cal_cancel_btn = ttk.Button(cal_buttons, text="✕  Abort", style="danger.Outline.TButton",
                                         command=self._cal_cancel)
        self.cal_cancel_btn.grid(row=0, column=1, sticky="ew", padx=(3, 0))
        _tip("The laser dot sits exactly on the edge: take the carriage's position as this end of the straight "
             "section.", self.confirm_btn)
        _tip("Stops the calibration (and turns the laser off). Nothing is changed on the Plan tab.",
             self.cal_cancel_btn)
        off_row = ttk.Frame(cal)
        off_row.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        off_label = ttk.Label(off_row, text="Laser Offset (mm)", style=LBL)
        off_label.pack(side=tk.LEFT)
        self.offset_entry = ttk.Entry(off_row, textvariable=self.laser_offset_var, width=7, style="Settings.TEntry",
                                      font=("TkDefaultFont", 8))
        self.offset_entry.pack(side=tk.RIGHT)
        _tip("How far the laser dot sits from the eye, toward −X (mm): with the carriage at X, the dot is at "
             "X minus this.", off_label, self.offset_entry)
        self.cal_result = muted(cal)
        self.cal_result.grid(row=5, column=0, sticky="w", pady=(4, 0))
        self._cal_show()

        # 5. The job and its live view
        job = ttk.LabelFrame(right, text="Live View", padding=(10, 8))
        job.pack(fill=tk.BOTH, expand=True)
        strip = ttk.Frame(job)
        strip.pack(fill=tk.X, pady=(0, 6))
        self.badge = tk.Label(strip, text="OFFLINE", font=("TkDefaultFont", 9, "bold"), padx=10, pady=3, bd=0)
        self.badge.pack(side=tk.LEFT)
        _tip("What the machine is doing: winding, paused, idle, ... -- or that it isn't connected.", self.badge)
        self.job_name = ttk.Label(strip, text="", font=("TkDefaultFont", 10, "bold"))
        self.job_name.pack(side=tk.LEFT, padx=(10, 0))
        self.estop_btn = tk.Button(strip, text="⚠  E-STOP", command=self._estop, relief="flat", bd=0, padx=16, pady=4,
                                   cursor="hand2", font=("TkDefaultFont", 10, "bold"))
        self.estop_btn.pack(side=tk.RIGHT, padx=(8, 0))
        _tip("Emergency stop: halts every motor at once (Moonraker's emergency stop, not queued behind other "
             "commands). Klipper then needs a Firmware Restart and the axes rehoming; the wind can't be resumed. "
             "Not a substitute for the machine's own E-stop switch.", self.estop_btn)
        self.cancel_btn = ttk.Button(strip, text="■  Cancel Wind", style="danger.Outline.TButton",
                                     command=self._cancel)
        self.cancel_btn.pack(side=tk.RIGHT, padx=(8, 0))
        _tip("Stops the wind for good (CANCEL_PRINT), after the few moves already on their way. To continue "
             "it later, use Export Partial G-Code on the Plan tab.", self.cancel_btn)
        self.pause_btn = ttk.Button(strip, text="⏸  Pause", width=11, style="warning.TButton",
                                    command=self._pause_resume)
        self.pause_btn.pack(side=tk.RIGHT)
        _tip("Pauses the wind (PAUSE) -- within Max Move Time plus the moves already queued -- or resumes it "
             "(RESUME), e.g. once the fiber is attached.", self.pause_btn)
        self.restart_btn = ttk.Button(strip, text="Firmware Restart", style="primary.Outline.TButton",
                                      command=self._firmware_restart)
        _tip("Restarts Klipper after an emergency stop or an error (FIRMWARE_RESTART). The axes need homing "
             "again afterwards.", self.restart_btn)
        info = ttk.Frame(job)
        info.pack(fill=tk.X, pady=(0, 6))
        self.progress = ttk.Progressbar(info, maximum=1000, style="success.Horizontal.TProgressbar")
        self.progress.pack(fill=tk.X)
        _tip("How much of the wind is done, by its planned winding time.", self.progress)
        line = ttk.Frame(info)
        line.pack(fill=tk.X, pady=(4, 0))
        self.job_message = ttk.Label(line, text="", style=LBL)
        self.job_message.pack(side=tk.LEFT)
        _tip("The machine's status line (M117): the layup and cycle it's winding.", self.job_message)
        self.job_times = ttk.Label(line, text="", font=("TkFixedFont", 9))
        self.job_times.pack(side=tk.RIGHT)
        _tip("Time wound so far (as the machine counts it, pauses not included), the planned time left from "
             "where the machine is, and the share done.", self.job_times)
        view_frame = ttk.Frame(job)
        view_frame.pack(fill=tk.BOTH, expand=True)
        self.live_view = LiveView(view_frame, self.app)
        self.live_view.clear("Not connected")
        self.restyle()

    # --- Showing it ---

    def on_show(self):
        self.live_view.visible = True
        self.live_view.redraw_view()

    def on_hide(self):
        self.live_view.visible = False

    def on_theme_changed(self):
        self.live_view.reapply_viewport_colors()
        self.restyle()

    def restyle(self):
        # The plain Tk widgets' colors (ttk ones follow the theme by themselves).
        colors = self.app.root.style.colors
        self.steps.restyle()
        self.estop_btn.configure(bg=theme.ESTOP_RED, fg="white", activebackground=theme.ESTOP_RED_ACTIVE,
                                 activeforeground="white", disabledforeground="#f3c1b8")
        for label in self._muted:
            label.configure(foreground=self.app.canvas_palette["muted"])
        self._laser_style()
        self._rotate_dir_style()
        self.conn_dot.configure(bg=colors.bg)
        self._last = None  # repaint the status

    def _laser_style(self):
        # Greyed out like the rest while locked, whether it's on or not.
        on = self._laser_on
        locked = str(self.laser_btn.cget("state")) == "disabled"
        bg, fg = (theme.LIVE_RED, "white") if on and not locked else theme.OFF_TOGGLE[self.app.theme_mode]
        self.laser_btn.configure(text="Laser ON" if on else "Laser OFF", bg=bg, fg=fg, activebackground=bg,
                                 activeforeground=fg, disabledforeground=self.app.canvas_palette["auto_text"])

    # --- Status ---

    def _tick(self):
        try:
            self.link.drain()
            snapshot = self.link.snapshot() + (self.link.pending, self._busy_note)
            if snapshot != self._last:
                self._last = snapshot
                self._apply(*snapshot[:3])
        finally:
            self.app.root.after(TICK_MS, self._tick)

    def status(self):
        """The latest MachineStatus while connected, else None."""
        state, status, _ = self.link.snapshot()
        return status if state == Link.ONLINE else None

    def _apply(self, state, status, error):
        colors, muted = self.app.root.style.colors, self.app.canvas_palette["muted"]
        self._rotation = self._rotation_now(status if state == Link.ONLINE else None)
        online = state == Link.ONLINE and status is not None
        # Connection
        self.connect_btn.configure(text="Connect" if state == Link.OFFLINE else "Disconnect",
                                   style="primary.TButton" if state == Link.OFFLINE else "primary.Outline.TButton")
        if state == Link.OFFLINE:
            dot, text, detail = muted, "Not connected", "Watching only: nothing is sent until you click."
        elif state == Link.CONNECTING:
            dot, text, detail = colors.warning, f"Connecting to {self.link.address}…", error
        elif state == Link.LOST:
            dot, text, detail = colors.danger, "Connection lost — retrying…", (
                "The machine carries on by itself. " + (error or ""))
        elif status.ready:
            client = self.link.client
            dot, text, detail = colors.success, "Online · Klipper ready", client.base if client else ""
        else:
            dot, text, detail = colors.warning, f"Online · Klipper {status.klippy_state}", status.state_message
        self.conn_dot.configure(fg=dot)
        self.conn_text.configure(text=text)
        self.conn_detail.configure(text=detail.strip()[:300])
        # The job
        badge, badge_bg, badge_fg = self._badge(state, status)
        self.badge.configure(text=badge, bg=badge_bg, fg=badge_fg)
        self.app.set_control_badge(badge_bg if state != Link.OFFLINE else None, self._tab_note(state, status))
        shown = status if status is not None else MachineStatus()
        active = shown.active and state in (Link.ONLINE, Link.LOST)
        self.job_name.configure(text=shown.filename if active or shown.print_state in ("complete", "cancelled",
                                                                                           "error") else "")
        message = shown.message if active else ""
        if online and not status.ready:
            message = status.state_message.splitlines()[0] if status.state_message else ""
        self.job_message.configure(text=message[:120])
        self.pause_btn.configure(text="▶  Resume" if shown.print_state == "paused" else "⏸  Pause",
                                 style="success.TButton" if shown.print_state == "paused" else "warning.TButton",
                                 state="normal" if online and status.active else "disabled")
        self.cancel_btn.configure(state="normal" if online and status.active else "disabled")
        self.estop_btn.configure(state="normal" if online else "disabled")
        if online and status.klippy_state in ("shutdown", "error"):
            self.restart_btn.pack(side=tk.RIGHT, padx=(8, 0), before=self.pause_btn)
        else:
            self.restart_btn.pack_forget()
        if status is not None and status.position:
            pos = status.live_position if len(status.live_position) >= 2 else status.position
            homed = status.homed_axes.upper() or "none"
            self.position_var.set(f"X {pos[0]:8.1f}   Y {pos[1]:6.1f}   homed: {homed}")
        else:
            self.position_var.set("X –   Y –")
        # (A brief loss of connection doesn't end it: its commands just fail meanwhile.)
        if self._cal is not None and online and (status.active or not status.ready):
            self._cal = None
            self._cal_note("Calibration stopped: the machine isn't free any more.")
        self._cal_show()
        # The live view
        self._sync_live(state, status)

    def _badge(self, state, status):
        colors, muted = self.app.root.style.colors, self.app.canvas_palette["muted"]
        if state == Link.OFFLINE:
            return "OFFLINE", muted, "white"
        if state == Link.CONNECTING:
            return "CONNECTING", colors.warning, colors.dark
        if state == Link.LOST:
            return "NO CONNECTION", colors.danger, "white"
        if status.klippy_state in ("shutdown", "error"):
            return status.klippy_state.upper(), colors.danger, "white"
        if not status.ready:
            return f"KLIPPER {status.klippy_state.upper()}", colors.warning, colors.dark
        if status.print_state == "printing" and machine_link.parse_rotation(status.filename):
            return "ROTATING", colors.success, "white"
        return {
            "printing": ("WINDING", colors.success, "white"),
            "paused": ("PAUSED", colors.warning, colors.dark),
            "complete": ("COMPLETE", colors.info, "white"),
            "cancelled": ("CANCELLED", muted, "white"),
            "error": ("ERROR", colors.danger, "white"),
        }.get(status.print_state, ("IDLE", colors.primary, "white"))

    def _tab_note(self, state, status):
        # A few words next to the Control tab's name.
        if state == Link.OFFLINE:
            return ""
        if state != Link.ONLINE or status is None:
            return "offline" if state == Link.LOST else "…"
        if status.active and machine_link.parse_rotation(status.filename):
            return "rotating" if status.print_state == "printing" else "paused"
        if status.active and self._live is not None:
            return f"{self._percent():.0f} %" + (" paused" if status.print_state == "paused" else "")
        return status.print_state if status.active else ""

    # --- The live view ---

    def _percent(self):
        index = self.live_view.live_index
        times = self._live[1] if self._live else None
        if index is None or not times or times[-1] <= 0:
            return 0.0
        return 100.0 * times[index] / times[-1]

    def _sync_live(self, state, status):
        view = self.live_view
        if state == Link.OFFLINE:
            if self._live_name is not None:
                self._live_name, self._live = None, None
                view.clear("Not connected")
            self._show_times(None)
            return
        if status is None:
            return  # still connecting: keep whatever is shown
        name = status.filename if status.filename and status.print_state != "standby" else ""
        rotation = machine_link.parse_rotation(name)
        if rotation is not None:
            # A rotation: nothing to draw (or download).
            if name != self._live_name:
                self._live_name, self._live = name, None
                feed, direction = rotation
                view.clear(f"Rotating the mandrel ({'A+' if direction > 0 else 'A−'} at {feed:g} °/min): "
                           "no wind to show.")
            self._show_times(status)
            return
        if name != self._live_name:
            if time.monotonic() < self._retry_at and name:
                return
            self._live_name, self._live = name, None
            if not name:
                view.clear("No wind on the machine")
            elif state == Link.ONLINE:
                self._load_live(name, status.file_size)
        if self._live is None:
            self._show_times(status)
            return
        commands, times, offsets = self._live
        if status.print_state == "complete":
            index = len(commands) - 1
        else:
            queued = machine_link.queued_move(offsets, status.file_position)
            index = machine_link.live_move(commands, times, queued, status.live_position)
        view.set_live(max(0, index) if commands else None)
        self._show_times(status)

    def _show_times(self, status):
        if status is None:
            self.progress.configure(value=0)
            self.job_times.configure(text="")
            return
        if self._live is not None and self.live_view.live_index is not None:
            times = self._live[1]
            index = self.live_view.live_index
            share = times[index] / times[-1] if times and times[-1] > 0 else 0.0
            left = format_hms(max(0.0, times[-1] - times[index]))
        else:
            share, left = status.progress, "--:--:--"
        if not status.filename or status.print_state == "standby":
            self.progress.configure(value=0)
            self.job_times.configure(text="")
            return
        if machine_link.parse_rotation(status.filename):
            self.progress.configure(value=0)
            self.job_times.configure(text=f"turning {format_hms(status.print_duration)}")
            return
        self.progress.configure(value=1000 * share)
        self.job_times.configure(text=f"wound {format_hms(status.print_duration)} · left {left} · {100 * share:3.0f} %")

    def _load_live(self, name, size):
        self.live_view.clear(f"Loading {name} from the machine…")
        local = self.local_copies.get(name)

        def work(client):
            path = None
            if local and os.path.exists(local) and (not size or os.path.getsize(local) == size):
                path = local
            else:
                os.makedirs(CACHE_DIR, exist_ok=True)
                path = os.path.join(CACHE_DIR, re.sub(r"[^\w.\-]", "_", name))
                if not (size and os.path.exists(path) and os.path.getsize(path) == size):
                    client.download(name, path)
            return name, read_gcode(path), machine_link.move_offsets(path)

        def done(result):
            loaded, data, offsets = result
            if loaded != self._live_name:
                return  # the machine has moved on to another file meanwhile
            if len(offsets) != len(data.commands):
                offsets = []  # can't be matched up: the bar then shows no LIVE line
            self.live_view.show_file("", data)
            self._live = (data.commands, data.times, offsets) if offsets else None
            self._last = None  # place the LIVE line at once

        def failed(e):
            if self._live_name == name:
                self.live_view.clear(f"Couldn't load {name} from the machine: {machine_link.describe(e)}")
                self._live_name, self._retry_at = None, time.monotonic() + LOAD_RETRY_SECONDS

        if not self.link.run(work, on_done=done, on_error=failed, background=True):
            self._live_name = None

    # --- Connection ---

    def _toggle_connection(self):
        state, _, _ = self.link.snapshot()
        if state == Link.OFFLINE:
            address = self.address_var.get().strip()
            if not address:
                messagebox.showerror("Error", "Enter the machine's address first.")
                return
            self.link.connect(address)
        else:
            self._rot_phase = None
            self.link.disconnect()
            self._cal = None
            self._cal_show()
        self._last = None

    # --- Commands ---

    def _report_error(self, what):
        def show(e):
            text = machine_link.describe(e)
            self.app.set_status(f"{what} failed: {text}")
            messagebox.showerror("Machine", f"{what} failed:\n{text}")
        return show

    def _gcode(self, script, what, on_done=None, max_pending=None, on_error=None):
        ok = self.link.run(lambda c: c.gcode(script), on_done=on_done, on_error=on_error or self._report_error(what),
                           max_pending=max_pending)
        if not ok and max_pending is None:
            messagebox.showerror("Machine", "Not connected to the machine.")
        return ok

    def _jog(self, axis, sign):
        status = self.status()
        if status is None or not status.can_jog or self._cal is not None:
            return
        self._send_jog(axis, sign * self.step_var.get())

    def _cal_jog(self, distance):
        status = self.status()
        if self._cal is None or self._cal["phase"] not in ("start", "end") or status is None or not status.idle:
            return
        self._send_jog("X", distance)

    def _send_jog(self, axis, distance):
        script = f"G91\nG1 {axis}{distance:.3f} F{machine_link.JOG_FEED[axis]:.0f}\nG90"

        def failed(e):
            # A refused move (e.g. out of range) leaves Klipper in relative mode:
            # back to absolute, which every file assumes.
            self.link.run(lambda c: c.gcode("G90"))
            self.app.set_status(f"Jog {axis} refused: {machine_link.describe(e)}")
        self._gcode(script, f"Jog {axis}", max_pending=MAX_QUEUED_JOGS, on_error=failed)

    def _home(self, axes):
        status = self.status()
        if status is None or not status.idle:
            return
        script = f"G28 {axes}".strip()
        if axes == "X" and status.homed("y"):
            script = f"G90\nG1 Y0 F{machine_link.RETRACT_FEED_Y:.0f}\n" + script
        self.app.set_status(f"Homing{' ' + axes if axes else ''}…")
        self._gcode(script, "Homing", on_done=lambda r: self.app.set_status("Homed."))

    def _retract(self):
        status = self.status()
        if status is None or not status.idle:
            return
        self._gcode(f"G90\nG1 Y0 F{machine_link.RETRACT_FEED_Y:.0f}", "Retracting Y")

    def _motors_off(self, xy_only):
        status = self.status()
        if status is None or not status.idle or self._cal is not None or self._rotation is not None:
            return
        if xy_only:
            script = "\n".join(f"SET_STEPPER_ENABLE STEPPER={name} ENABLE=0" for name in machine_link.XY_STEPPERS)
            what, done = "Turning X/Y off", "X and Y motors off (A still held)."
        else:
            script, what, done = "M84", "Turning the motors off", "All motors off."
        self._gcode(script, what, on_done=lambda r: self.app.set_status(done))

    def _flip_rotation(self):
        if self._rotation is None:
            self._rotate_dir = -self._rotate_dir
            self._rotate_dir_style()

    def _rotation_now(self, status):
        found = (machine_link.parse_rotation(status.filename)
                 if status is not None and status.active else None)
        if self._rot_phase == "starting" and found is not None:
            self._rot_phase = None  # it's running
        if self._rot_phase == "stopping" and found is None:
            self._rot_phase = None  # it has stopped
        if found is None and self._rot_phase is None:
            return None
        feed, direction = found or self._rot_request or (0.0, 1)
        return {"feed": feed, "direction": direction, "state": self._rot_phase or status.print_state}

    def _refresh_rotation(self):
        self._rotation = self._rotation_now(self.status())
        self._cal_show()

    def _toggle_rotation(self):
        if self._rotation is not None:
            self._stop_rotation()
            return
        status = self.status()
        if status is None or not status.idle or self._cal is not None:
            return
        try:
            feed = float(self.rotate_feed_var.get())
        except (tk.TclError, ValueError):
            feed = 0.0
        if not feed > 0:
            messagebox.showerror("Rotate", "Enter a feed rate above 0 (degrees per minute).")
            return
        direction = self._rotate_dir
        name = machine_link.rotation_filename(feed, direction)

        def work(client):
            os.makedirs(CACHE_DIR, exist_ok=True)
            path = os.path.join(CACHE_DIR, name)
            machine_link.write_rotation_file(path, feed, direction)
            client.upload(path, name)
            client.start(name)

        def failed(e):
            self._rot_phase = None
            self._refresh_rotation()
            self._report_error("Starting the rotation")(e)
        if not self.link.run(work, on_done=lambda r: self.app.set_status(f"Rotating A at {feed:g} °/min."),
                             on_error=failed, background=True):
            messagebox.showerror("Machine", "Not connected to the machine.")
            return
        self._rot_phase, self._rot_request = "starting", (feed, direction)
        self.app.set_status("Starting the rotation…")
        self._refresh_rotation()

    def _stop_rotation(self):
        rotation = self._rotation
        if rotation is None or rotation["state"] not in ("printing", "paused"):
            return

        def failed(e):
            self._rot_phase = None
            self._refresh_rotation()
            self._report_error("Stopping the rotation")(e)
        def stop(client):
            client.cancel()
            client.gcode("M117")  # clears "Rotating ..." from the machine's status line
        if not self.link.run(stop, on_error=failed, background=True):
            messagebox.showerror("Machine", "Not connected: stop it in Mainsail or with the machine's E-stop.")
            return
        self._rot_phase = "stopping"
        self.app.set_status("Stopping the rotation…")
        self._refresh_rotation()

    def _toggle_laser(self):
        status = self.status()
        if status is None or not status.can_jog or self._cal is not None:
            return
        on = not self._laser_on
        self._set_laser(on)

    def _set_laser(self, on, quiet=False):
        def done(result):
            self._laser_on = on
            self._laser_style()
        handler = (lambda e: self.app.set_status(f"Laser: {e}")) if quiet else self._report_error(
            "Turning the laser " + ("on" if on else "off"))
        self.link.run(lambda c: c.gcode("LASER_ON" if on else "LASER_OFF"), on_done=done, on_error=handler)

    def _pause_resume(self):
        status = self.status()
        if status is None or not status.active:
            return
        if status.print_state == "paused":
            self.link.run(lambda c: c.resume(), on_error=self._report_error("Resume"), background=True)
            self.app.set_status("Resuming…")
        else:
            self.link.run(lambda c: c.pause(), on_error=self._report_error("Pause"), background=True)
            self.app.set_status("Pausing…")

    def _cancel(self):
        status = self.status()
        if status is None or not status.active:
            return
        if machine_link.parse_rotation(status.filename) is None and not messagebox.askyesno("Cancel Wind", "Cancel the wind on the machine?\n\nIt stops after the few moves "
                                   "already on their way and can't be resumed from here (Export Partial G-Code "
                                   "continues it later).", icon="warning", default="no"):
            return
        self.link.run(lambda c: c.cancel(), on_error=self._report_error("Cancel"), background=True)
        self.app.set_status("Cancelling the wind…")

    def _estop(self):
        # No confirmation: an emergency stop has to be one click.
        if not self.link.run(lambda c: c.emergency_stop(), on_error=self._report_error("Emergency stop"),
                             background=True):
            messagebox.showerror("Machine", "Not connected: use the machine's own E-stop switch.")
            return
        self._cal = None
        self._cal_show()
        self.app.set_status("EMERGENCY STOP sent.")

    def _firmware_restart(self):
        self.link.run(lambda c: c.firmware_restart(), on_error=self._report_error("Firmware restart"),
                      background=True)
        self.app.set_status("Restarting Klipper…")

    # --- Send & Wind (from the Plan tab) ---

    def send_blocker(self):
        """Why a new wind can't be sent now, or None if it can."""
        state, status, _ = self.link.snapshot()
        if state != Link.ONLINE or status is None:
            return "Not connected to the machine. Connect on the Control tab first."
        if not status.ready:
            return f"Klipper isn't ready ({status.klippy_state}). Check the machine on the Control tab."
        if status.active:
            return f"The machine is already running a file ({status.filename}, {status.print_state})."
        if self._cal is not None:
            return "A tank calibration is under way on the Control tab."
        if self._rotation is not None:
            return "The mandrel is rotating (Control tab): stop it first."
        return None

    def send_file(self, path):
        """Uploads a generated file, then asks whether to start it."""
        client = self.link.client
        if client is None:
            messagebox.showerror("Send & Wind", "Lost the connection to the machine. The file is saved; send it "
                                 "again once connected.")
            return
        name = os.path.basename(path)

        def work(progress):
            progress(0.0, f"Uploading {name}…")
            client.upload(path, name, lambda f: progress(f))
            return name

        ProgressTask(self.app, "Sending to the Machine", work, on_done=lambda n: self._sent(path, n),
                     on_error=lambda e: messagebox.showerror("Send & Wind", f"The upload failed:\n{machine_link.describe(e)}"),
                     on_cancel=lambda: self.app.set_status("Upload cancelled. Nothing was started."))

    def _sent(self, path, name):
        self.local_copies[name] = path
        self.app.set_status(f"Uploaded to the machine: {name}")
        # (Deferred: the progress window is still closing.)
        self.app.root.after(10, lambda: self._ask_start(name))

    def _ask_start(self, name):
        blocker = self.send_blocker()
        if blocker:
            messagebox.showwarning("Send & Wind", f"Uploaded, but not started:\n{blocker}")
            return
        if not messagebox.askyesno("Send & Wind", f"Start winding {name} on the machine now?\n\nIt runs from the "
                                   "machine's own copy from here on: this PC can disconnect at any time.",
                                   icon="question"):
            self.app.set_status(f"Uploaded, not started: {name} (start it here or in Mainsail).")
            return

        def started(result):
            self.app.set_status(f"Winding {name}.")
            self.app.show_tab("control")
        self.link.run(lambda c: c.start(name), on_done=started, on_error=self._report_error("Starting the wind"),
                      background=True)

    # --- Tank calibration ---
    # Phases: "moving" (to the next point), "start" / "end" (the user jogs the
    # laser dot onto that end of the straight section, then confirms).

    def _cal_note(self, text):
        self.cal_result.configure(text=text)

    def _update_manual(self):
        # Manual Control: per what the machine is doing -- and all of it locked
        # while a calibration is under way (that has its own X buttons).
        status = self.status()
        calibrating = self._cal is not None
        rotating = self._rotation is not None
        busy = calibrating or rotating
        can_jog = status is not None and status.can_jog and not busy
        for axis, b in self.jog_buttons:
            ok = can_jog and not (axis == "A" and status.print_state == "paused")
            b.configure(state="normal" if ok else "disabled")
        idle = status is not None and status.idle and not busy
        for b in self.home_buttons + [self.retract_btn, self.motors_btn, self.xy_off_btn]:
            b.configure(state="normal" if idle else "disabled")
        self.laser_btn.configure(state="normal" if can_jog else "disabled")
        self._laser_style()
        self.steps.set_enabled(can_jog)
        if status is None:
            note = ""
        elif rotating:
            note = "Locked while the mandrel turns."
        elif calibrating:
            note = "Locked while calibrating: move X with the Calibration panel's buttons."
        elif status.print_state == "printing":
            note = "Locked while winding: jogging would slip in between the file's moves."
        elif status.print_state == "paused":
            note = "Paused: Resume returns the carriage to where it paused. A stays locked."
        else:
            note = self._busy_note or ("Busy…" if self.link.pending else "")
        self.jog_note.configure(text=note)
        self._rotate_show(status)

    def _rotate_show(self, status):
        rotation = self._rotation
        state = rotation["state"] if rotation else None
        free = status is not None and status.idle and self._cal is None
        if state is None:
            self.rotate_btn.configure(text="▶  Start Rotation", style="success.TButton",
                                      state="normal" if free else "disabled")
        elif state in ("starting", "stopping"):
            self.rotate_btn.configure(text="Starting…" if state == "starting" else "Stopping…",
                                      style="danger.TButton", state="disabled")
        else:
            self.rotate_btn.configure(text="■  Stop Rotation", style="danger.TButton",
                                      state="normal" if status is not None else "disabled")
        settable = free and rotation is None
        self.rotate_feed_entry.configure(state="normal" if settable else "disabled")
        self.rotate_dir_btn.configure(state="normal" if settable else "disabled")
        self._rotate_dir_style()
        if rotation is None:
            note = ""
        else:
            how = (f"{'A+' if rotation['direction'] > 0 else 'A−'} at {rotation['feed']:g} °/min "
                   f"({rotation['feed'] / 360:.1f} rpm)")
            note = {"starting": f"Sending the rotation ({how}) to the machine…",
                    "stopping": "Stopping: running out the moves already queued…",
                    "paused": f"Paused ({how}). Resume above, in Mainsail or on the machine."}.get(
                state, f"Turning {how}. Pause / Resume above, in Mainsail or on the machine.")
        self.rotate_note.configure(text=note)

    def _rotate_dir_style(self):
        locked = str(self.rotate_dir_btn.cget("state")) == "disabled"
        bg, fg = theme.OFF_TOGGLE[self.app.theme_mode]
        if locked:
            fg = self.app.canvas_palette["auto_text"]
        self.rotate_dir_btn.configure(text="Direction  A+" if self._rotate_dir > 0 else "Direction  A−", bg=bg, fg=fg,
                                      activebackground=bg, activeforeground=fg, disabledforeground=fg)
    def _cal_show(self):
        self._update_manual()
        cal = self._cal
        status = self.status()
        idle = status is not None and status.idle
        texts = {
            None: "",
            "moving": "Moving… (Y fully retracted first)",
            "start": "1 / 2 · Move X until the laser dot sits exactly where the straight section begins (chuck "
                     "side), then confirm.",
            "end": "2 / 2 · Move X until the laser dot sits exactly where the straight section ends (far side), "
                   "then confirm.",
            "measuring": "Reading the position…",
        }
        self.cal_step.configure(text=texts[cal["phase"]] if cal else "")
        self.cal_btn.configure(state="normal" if idle and cal is None and self._rotation is None else "disabled")
        pointing = cal is not None and cal["phase"] in ("start", "end")
        self.confirm_btn.configure(state="normal" if pointing and idle and not self.link.pending else "disabled")
        for b in self.cal_jog_buttons:
            b.configure(state="normal" if pointing and idle else "disabled")
        self.offset_entry.configure(state="disabled" if cal is not None or self._rotation is not None else "normal")
        self.cal_cancel_btn.configure(state="normal" if cal is not None else "disabled")

    def _cal_targets_ok(self, *xs):
        bad = [x for x in xs if not 0 <= x <= winding.MAX_X]
        if bad:
            messagebox.showerror("Calibrate Tank", f"The carriage would have to go to X {bad[0]:.1f}, outside its "
                                 f"travel (0–{winding.MAX_X:g}). Check Chuck X-Offset, Straight Length and the "
                                 "Laser Offset.")
            return False
        return True

    def _cal_start(self):
        status = self.status()
        if status is None or not status.idle or self._rotation is not None:
            return
        try:
            job = self.app.build_job()
            offset = float(self.laser_offset_var.get())
        except (winding.SettingsError, tk.TclError, ValueError):
            messagebox.showerror("Calibrate Tank", "The Plan's settings (or the Laser Offset) don't all hold valid "
                                 "numbers right now.")
            return
        start, end = machine_link.straight_section(job)
        if not (job.straight_length > 0 and self._cal_targets_ok(start + offset, end + offset)):
            return
        home = "" if status.homed("xy") else "home X and Y, "
        if not messagebox.askokcancel(
                "Calibrate Tank",
                f"The machine will {home}pull Y fully back (Y0), move the carriage to X {start + offset:.1f} "
                f"and turn the laser on there, so the dot lands where the Plan expects the straight section to "
                f"begin (X {start:.1f}).\n\nMake sure nothing is in the way.", icon="warning"):
            return
        self._cal = {"phase": "moving", "job": job, "offset": offset, "plan": (start, end), "start": None}
        self._cal_note("")
        self._cal_move(start + offset, "start", home=not status.homed("xy"))

    def _cal_move(self, x, next_phase, home=False):
        # To carriage X `x`: the laser off on the way, Y fully back before
        # any X travel, and the laser on only once the carriage is there.
        cal = self._cal
        script = "\n".join(["LASER_OFF", "G90"] + (["G28 X Y"] if home else []) + [
            f"G1 Y0 F{machine_link.RETRACT_FEED_Y:.0f}", "M400",
            f"G1 X{x:.3f} F{machine_link.TRAVEL_FEED_X:.0f}", "M400", "LASER_ON"])
        self._laser_on = False
        self._laser_style()

        def done(result):
            if self._cal is cal:
                cal["phase"] = next_phase
                self._laser_on = True
                self._laser_style()
                self._cal_show()

        def failed(e):
            if self._cal is cal:
                self._cal = None
                self._cal_show()
            self._report_error("Calibration move")(e)
        cal["phase"] = "moving"
        self._cal_show()
        self._gcode(script, "Calibration move", on_done=done, on_error=failed)

    def _cal_confirm(self):
        cal = self._cal
        if cal is None or cal["phase"] not in ("start", "end"):
            return
        which = cal["phase"]

        def measure(client):
            client.gcode("M400")  # the carriage at rest where the jogs left it
            return client.status().position[0]

        def measured(x):
            if self._cal is not cal:
                return
            point = x - cal["offset"]
            if which == "start":
                cal["start"] = point
                # The far end, shifted by how far off the chuck side was.
                plan_start, plan_end = cal["plan"]
                target = plan_end + (point - plan_start) + cal["offset"]
                if not self._cal_targets_ok(target):
                    self._cal = None
                    self._cal_show()
                    return
                self._cal_note(f"Straight section begins at X {point:.2f}.")
                self._cal_move(target, "end")
            else:
                self._cal_finish(cal, point)

        def failed(e):
            if self._cal is cal:
                cal["phase"] = which
                self._cal_show()
            self._report_error("Reading the position")(e)
        cal["phase"] = "measuring"
        self._cal_show()
        self.link.run(measure, on_done=measured, on_error=failed)

    def _cal_finish(self, cal, end):
        start = cal["start"]
        try:
            values = machine_link.calibrated_settings(cal["job"], start, end)
        except ValueError as e:
            messagebox.showerror("Calibrate Tank", f"{e}\nMeasured: X {start:.2f} and X {end:.2f}.")
            cal["phase"] = "end"
            self._cal_show()
            return
        self._cal = None
        self._set_laser(False, quiet=True)
        self.app.apply_calibration(start, end)
        self._cal_note(f"Straight section X {start:.2f} – {end:.2f}: Straight Length {values['straight_length']:.2f} "
                       f"mm, Chuck X-Offset {values['chuck_offset']:.2f} mm. Entered on the Plan tab.")
        self.app.set_status("Tank calibrated: Chuck X-Offset and Straight Length entered on the Plan tab.")
        self._cal_show()

    def _cal_cancel(self):
        if self._cal is None:
            return
        self._cal = None
        if self.status() is not None:
            self._set_laser(False, quiet=True)
        self._cal_note("Calibration cancelled.")
        self._cal_show()
