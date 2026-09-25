import tkinter as tk
from tkinter import ttk
import tkinter.font as tkfont
import math
import threading
import queue
import time
import theme
import winding

# (label, settings key) for every entry field, grouped as they appear in the
# settings panel. Machine/Tank/Winding Settings are global to the whole program;
# LAYUP_FIELDS are edited per layup through the layup switcher.
MACHINE_FIELDS = [
    ("Chuck X-Offset (mm)", "chuck_offset"),
    ("Eye Arm Length (mm)", "eye_arm_length"),
    ("Eye Width (mm)", "eye_width"),
    ("Min Spacing (Safety) (mm)", "min_spacing"),
    ("Max Rotation Speed (mm/s)", "max_surface_speed"),
    ("Max Move Time (s)", "max_move_time"),
]
TANK_FIELDS = [("Tank Length (mm)", "tank_length"), ("Tank Diameter (mm)", "tank_diameter"), ("End Cap Diameter (mm)", "end_cap_diameter")]
WINDING_FIELDS = [("Bandwidth / Tow Width (mm)", "bandwidth"), ("Turnaround Zone (mm)", "turnaround_zone")]
LAYUP_FIELDS = [
    ("Number of Cycles", "passes"),
    ("Pattern Number (Strands per Cycle)", "pattern_number"),
    ("Winding Angle (Degrees)", "wind_angle"),
    ("Turnaround / Dwell Angle (Deg)", "turnaround_angle"),
]
START_X_FIELD = ("Start Wind at X (mm)", "start_x")
FIELD_LABELS = {key: label for label, key in MACHINE_FIELDS + [START_X_FIELD] + TANK_FIELDS + WINDING_FIELDS + LAYUP_FIELDS}

# Entry fields use the same compact size as their labels (a ttk style can't
# set an entry's font; it has to be given to the widget itself).
FIELD_FONT = ("TkDefaultFont", 8)
LEGEND_FONT = ("TkDefaultFont", 8)
LEGEND_FONT_ACTIVE = ("TkDefaultFont", 8, "bold")
SWATCH_W, SWATCH_H = 24, 12


def format_hms(seconds):
    h, m = divmod(int(seconds), 3600); m, s = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _set_if_changed(var, value):
    # Setting a watched variable triggers a redraw; skip it when nothing changes.
    try:
        current = var.get()
    except tk.TclError:
        current = None
    if current != value:
        var.set(value)


class _BackgroundCalc:
    """Runs fn(key) on a worker thread, for the newest requested key only, and
    hands the outcome to on_result(key, result, error) on the GUI thread (from
    poll()). Only one calculation is ever in flight; a request made while one
    runs just overwrites the single pending slot -- which is exactly how an
    outdated queued calculation gets "cancelled": it's replaced before it ever
    starts. Each request gets a unique token, and only the newest request's
    outcome is delivered; anything else that arrives is stale and discarded."""

    def __init__(self, fn, on_result):
        self._fn, self._on_result = fn, on_result
        self._queue = queue.Queue()
        self._thread = None
        self._next_token = 0
        self._wanted = None
        self.pending = None   # (token, key) waiting for the worker
        self.inflight = None  # (token, key) being calculated right now

    @property
    def busy(self):
        return self._thread is not None and self._thread.is_alive()

    def request(self, key):
        if self.inflight is not None and self.inflight[1] == key:
            # Already being calculated (e.g. a value was changed and changed
            # straight back) -- drop anything queued behind it.
            self.pending, self._wanted = None, self.inflight[0]
            return
        if self.pending is not None and self.pending[1] == key:
            return
        self._next_token += 1
        self._wanted = self._next_token
        self.pending = (self._next_token, key)
        if not self.busy:
            self._launch()

    def _launch(self):
        if self.pending is None:
            return
        self.inflight, self.pending = self.pending, None
        self._thread = threading.Thread(target=self._work, args=self.inflight, daemon=True)
        self._thread.start()

    def _work(self, token, key):
        # Worker thread: pure math only, no Tkinter access.
        try:
            self._queue.put((token, key, self._fn(key), None))
        except Exception as e:
            self._queue.put((token, key, None, e))

    def poll(self):
        # GUI thread: the only place outcomes may touch widgets.
        try:
            while True:
                token, key, result, error = self._queue.get_nowait()
                self._thread = self.inflight = None
                if token == self._wanted:
                    self._on_result(key, result, error)
                if self.pending is not None:
                    self._launch()
        except queue.Empty:
            pass


class AutoEntry:
    """An entry field whose value the app computes -- "auto": grey text with an
    AUTO tag inside the field -- until the user types a value of their own --
    "custom": normal text, never changed by the app again. Clearing the field
    and leaving it (or pressing Enter) puts it back on auto.

    `auto_var` returns the BooleanVar holding the field's mode (a callable, as
    for per-layup fields it changes with the selected layup). `on_mode_change`
    is called with the new mode when the user leaves the field in a different
    mode than they entered it."""

    def __init__(self, tab, entry, auto_var, on_mode_change):
        self.tab, self.entry = tab, entry
        self._auto_var = auto_var
        self._on_mode_change = on_mode_change
        self._mode_at_focus = None      # mode when the field gained focus
        self._fill_while_focused = False  # one-shot: allow a refill although focused
        # Key validation fires only for the user's own typing/pasting -- never
        # for the app setting the variable -- which is exactly the line between
        # "custom" and "auto". It never rejects anything.
        entry.configure(validate="key", validatecommand=(entry.register(self._typed), "%P"))
        entry.bind("<FocusIn>", self._focus_in, add="+")
        entry.bind("<FocusOut>", self._focus_out, add="+")
        entry.bind("<Return>", self._focus_out, add="+")
        self.tag = tk.Label(entry, text="AUTO", font=("TkDefaultFont", 7, "bold"), bd=0, padx=0, pady=0, cursor="xterm")
        # The tag sits on top of the field, so a click on it has to reach the field.
        self.tag.bind("<Button-1>", lambda e: (entry.focus_set(), "break")[1])

    @property
    def auto(self):
        return bool(self._auto_var().get())

    def has_focus(self):
        try:
            return self.tab.app.root.focus_get() is self.entry
        except KeyError:  # focus is in a Tk-internal widget (e.g. a combobox popdown)
            return False

    def editing(self):
        """True while the user is in the field, so the app mustn't overwrite it
        -- except once, right after they leave it or press Enter."""
        editing = self.has_focus() and not self._fill_while_focused
        self._fill_while_focused = False
        return editing

    def rebind(self):
        # The field now edits another layup's value (switched while in it).
        if self.has_focus():
            self._mode_at_focus = self.auto

    def refresh(self):
        # Grey text and the AUTO tag while auto; the theme's normal text when
        # custom. A disabled field shows neither: its own greyed look says it all.
        if self.auto and not self.entry.instate(["disabled"]):
            color = self.tab.app.canvas_palette["auto_text"]
            self.entry.configure(foreground=color)
            self.tag.configure(fg=color, bg=self.tab.app.root.style.colors.inputbg)
            self.tag.place(relx=1.0, rely=0.5, x=-7, anchor="e")
        else:
            self.entry.configure(foreground="")
            self.tag.place_forget()

    def _typed(self, new_text):
        # An emptied field counts as auto straight away (the preview follows at
        # once); it's refilled with the computed value when the user leaves it.
        auto = new_text.strip() == ""
        auto_var = self._auto_var()
        if bool(auto_var.get()) != auto:
            auto_var.set(auto)
            self.refresh()
        return True

    def _focus_in(self, event=None):
        self._mode_at_focus = self.auto
        if self._mode_at_focus:
            # Select the computed value, so typing replaces it rather than
            # appending to it. Deferred until the click has placed the cursor.
            self.tab.app.root.after_idle(lambda: self.has_focus() and self.entry.select_range(0, tk.END))

    def _focus_out(self, event=None):
        auto = self.auto
        if auto:
            self._fill_while_focused = True  # also on Enter, where focus stays put
            self.tab._redraw_now()
        if self._mode_at_focus is not None and self._mode_at_focus != auto:
            self._on_mode_change(auto)
        self._mode_at_focus = auto if self.has_focus() else None


class PlanTab:
    def __init__(self, settings_parent, viz_parent, app):
        self.settings_parent = settings_parent
        self.viz_parent = viz_parent
        self.app = app
        self.canvas = None
        self.redraw_timer = None
        self.tank_entries = {}
        self.layup_entries = {}

        # --- Background calculation state ---
        # The strand-path simulation (and, while continuing a wind, the tank's
        # progress up to the continue point) can be slow for long winds, so both
        # run on worker threads (see _BackgroundCalc) instead of the GUI thread.
        #
        # Every simulation is keyed by its immutable winding.WindingJob, and the
        # last finished result is cached against it. Anything that doesn't change
        # the job -- switching the displayed layup, toggling "Show All Layups",
        # resizing, rotating the view -- redraws straight from that cache without
        # re-running the simulation, which is what keeps layup switching instant.
        # Progress is keyed and cached the same way, by (job, continue point).
        self._sim = _BackgroundCalc(winding.simulate, self._apply_calc_result)
        self._prog = _BackgroundCalc(lambda key: winding.progress(*key), self._apply_progress_result)
        self._result = None        # (job, winding.SimulationResult) of the last finished simulation
        self._progress = None      # ((job, point), winding.Progress) of the last finished progress
        self._job = None           # job currently shown; None while a field holds invalid input
        self._sim_blocked = False  # True while the current job can't be simulated at all
        self._view_geom = None     # (scale, ox, oy) the tank is currently drawn with
        self._tank_outline = None  # the tank silhouette polygon's screen points
        # While the partial page is open: the ProgramPoint to continue from (the
        # preview then shows the tank as wound up to it), and who to tell about
        # its location / problems (on_progress(location, error)).
        self.partial_point = None
        self.on_progress = None

        # --- 3D-ish strand view rotation state ---
        # The tank's outer silhouette looks identical from any angle around its own
        # axis (it's a solid of revolution), so "rotating" the view doesn't need to
        # touch the tank drawing at all -- only which parts of the wound strand are
        # front-facing (visible) vs. wrapped around the back changes. That split is
        # cheap trig over the cached raw simulation points, so rotating can be
        # redrawn every animation frame without re-running the background simulation.
        self._view_azimuth = 0.0

        # Rotation is driven by a simple velocity model rather than a per-click
        # "ease toward a target" animation: while a button is held, angular velocity
        # ramps up toward a max speed; once released, it ramps back down to zero.
        # This gives a genuinely smooth accelerate/cruise/decelerate feel for both a
        # quick tap (a small nudge that speeds up and back down almost immediately)
        # and a long hold (ramps up to, and cruises at, a steady top speed), with a
        # single continuous animation loop instead of restarting an ease curve on
        # every repeat tick (which caused the earlier "slows down the longer you
        # hold it, then lurches forward on release" bug).
        self._spin_direction = 0  # -1, 0, or +1 -- which way is currently held
        self._spin_velocity = 0.0  # deg/s, signed
        self._spin_job = None
        self._spin_last_time = None
        self.SPIN_MAX_SPEED_DEG_S = 220.0
        self.SPIN_ACCEL_DEG_S2 = 700.0
        self.SPIN_DECEL_DEG_S2 = 700.0

        # Preview-only (not a G-code setting): draw every layup's first cycle on
        # the tank at once, color-coded, with a legend -- instead of only the
        # layup currently selected in the settings panel.
        self.show_all_var = tk.BooleanVar(value=False)
        self._legend_fonts = (tkfont.Font(font=LEGEND_FONT), tkfont.Font(font=LEGEND_FONT_ACTIVE))

        self.setup_ui()
        self.on_layups_changed()
        self.app.root.after(50, self._poll_calc_queue)

    def setup_ui(self):
        left_panel = self.settings_parent

        # A slightly smaller body font than the rest of the app so all settings
        # groups comfortably fit on screen without scrolling; see
        # theme.setup_compact_styles for the actual font sizes.
        LBL, ENT, CMB, FRM = "Settings.TLabel", "Settings.TEntry", "Settings.TCombobox", "Settings.TLabelframe"
        # Section padding (horizontal, vertical) and the gap below each section,
        # kept tight so the whole panel fits a maximized window without scrolling.
        FRAME_PAD, FRAME_GAP = (8, 4), (0, 6)

        def add_fields(frame, fields, first_row=0, store=None):
            for i, (label_text, key) in enumerate(fields):
                ttk.Label(frame, text=label_text, style=LBL).grid(row=first_row + i, column=0, sticky="w", pady=1, padx=(0, 10))
                # Layup entries are bound to a variable later (on_layups_changed),
                # since which layup's variables they edit changes on every switch.
                var = self.app.params.get(key)
                entry = ttk.Entry(frame, width=12, style=ENT, font=FIELD_FONT, **({"textvariable": var} if var is not None else {}))
                entry.grid(row=first_row + i, column=1, sticky="e", pady=1)
                if store is not None: store[key] = entry
            frame.columnconfigure(0, weight=1)

        # 1. Machine Settings
        machine_frame = ttk.LabelFrame(left_panel, text="Machine Settings", padding=FRAME_PAD, style=FRM)
        machine_frame.pack(fill=tk.X, pady=FRAME_GAP)
        add_fields(machine_frame, MACHINE_FIELDS)
        # A plain ttk.Checkbutton, same as "Optimize Trajectory" in Winding
        # Settings -- just the compact Settings font, no color/Toolbutton style.
        ttk.Checkbutton(machine_frame, text="Home Axes (G28) Before Winding",
                        variable=self.app.params["home_before_wind"],
                        style="Settings.TCheckbutton").grid(row=len(MACHINE_FIELDS), column=0, columnspan=2, sticky="w", pady=(6, 2))
        # Depends on the checkbox above (indented under it, disabled without
        # homing): where the wind starts after homing. Auto: where the dome ends.
        row = len(MACHINE_FIELDS) + 1
        self._start_x_label = ttk.Label(machine_frame, text=START_X_FIELD[0], style=LBL)
        self._start_x_label.grid(row=row, column=0, sticky="w", pady=1, padx=(22, 10))
        self._start_x_entry = ttk.Entry(machine_frame, textvariable=self.app.params["start_x"], width=12, style=ENT, font=FIELD_FONT)
        self._start_x_entry.grid(row=row, column=1, sticky="e", pady=1)
        self._start_x_field = AutoEntry(self, self._start_x_entry, lambda: self.app.params["start_x_auto"],
                                        self._start_x_mode_changed)

        # 2. Tank Settings
        tank_frame = ttk.LabelFrame(left_panel, text="Tank Settings", padding=FRAME_PAD, style=FRM)
        tank_frame.pack(fill=tk.X, pady=FRAME_GAP)
        ttk.Label(tank_frame, text="End-Cap Type", style=LBL).grid(row=0, column=0, sticky="w", pady=1, padx=(0, 10))
        self.cap_type_combo = ttk.Combobox(tank_frame, textvariable=self.app.params["end_cap_type"], values=["Round", "Flat"], state="readonly", width=10, style=CMB, font=FIELD_FONT)
        self.cap_type_combo.grid(row=0, column=1, sticky="e", pady=1)
        self.app.params["end_cap_type"].trace_add("write", self.on_cap_type_change)
        add_fields(tank_frame, TANK_FIELDS, first_row=1, store=self.tank_entries)

        # 3. Winding Settings -- shared by every layup of the program.
        winding_frame = ttk.LabelFrame(left_panel, text="Winding Settings", padding=FRAME_PAD, style=FRM)
        winding_frame.pack(fill=tk.X, pady=FRAME_GAP)
        add_fields(winding_frame, WINDING_FIELDS)
        # Experimental: eases a small piece of each dwell's rotation into the
        # traversal steps flanking it instead of one abrupt stop, so Klipper's
        # motion planner doesn't need to slow to a near-halt at the turnaround.
        # A plain ttk.Checkbutton (just a font-size tweak via the Settings style,
        # not a color/Toolbutton style) -- a real checkbox is the most immediately
        # recognizable widget for a plain on/off setting like this. Greyed out
        # while a Turnaround Zone is set, which spreads the whole turnaround
        # rotation out and so supersedes it (see winding.compute_dwell_blend).
        self._optimize_check = ttk.Checkbutton(winding_frame, text="Optimize Trajectory (experimental)",
                                               variable=self.app.params["optimize_trajectory"],
                                               style="Settings.TCheckbutton")
        self._optimize_check.grid(row=len(WINDING_FIELDS), column=0, columnspan=2, sticky="w", pady=(4, 0))
        # PAUSE between layups, to check the fiber after every pattern change.
        ttk.Checkbutton(winding_frame, text="Pause After Each Layup", variable=self.app.params["pause_after_layup"],
                        style="Settings.TCheckbutton").grid(row=len(WINDING_FIELDS) + 1, column=0, columnspan=2,
                                                            sticky="w", pady=(2, 0))

        # 4. Layups -- the switchable per-layup settings. The section's title is
        # itself the switcher: [swatch] Layup 2 of 3 [<] [>] [-] [+], where the
        # swatch is the layup's strand color on the tank (matching the preview's
        # strands and legend). Living in the title keeps it from costing a row.
        nav = ttk.Frame(left_panel)
        NAV = "Nav.TButton"
        self._layup_swatch = tk.Canvas(nav, width=SWATCH_W + 1, height=SWATCH_H + 1, highlightthickness=0, bd=0)
        self._layup_swatch.pack(side=tk.LEFT, padx=(0, 6))
        self.layup_title_var = tk.StringVar()
        # Fixed width so the buttons don't jump around as the text changes.
        ttk.Label(nav, textvariable=self.layup_title_var, style="SettingsHeader.TLabel", width=13).pack(side=tk.LEFT)
        self._prev_btn = ttk.Button(nav, text="‹", width=2, style=NAV, takefocus=False, command=lambda: self._step_layup(-1))
        self._prev_btn.pack(side=tk.LEFT)
        self._next_btn = ttk.Button(nav, text="›", width=2, style=NAV, takefocus=False, command=lambda: self._step_layup(1))
        self._next_btn.pack(side=tk.LEFT, padx=(2, 10))
        self._remove_btn = ttk.Button(nav, text="−", width=2, style=NAV, takefocus=False, command=self._remove_layup)
        self._remove_btn.pack(side=tk.LEFT)
        self._add_btn = ttk.Button(nav, text="+", width=2, style=NAV, takefocus=False, command=self._add_layup)
        self._add_btn.pack(side=tk.LEFT, padx=(2, 0))
        layup_frame = ttk.LabelFrame(left_panel, labelwidget=nav, padding=FRAME_PAD, style=FRM)
        layup_frame.pack(fill=tk.X, pady=FRAME_GAP)
        nav.lift(layup_frame)  # a labelwidget must stack above its frame to be visible
        add_fields(layup_frame, LAYUP_FIELDS, store=self.layup_entries)
        # Angles too close to 0/90 deg aren't physically realizable for the
        # current tank size/tow width (see compute_wind_angle_bounds) and are
        # what caused the "calculates forever" symptom. Only auto-correct once
        # the user is actually done editing this one field (not on every
        # keystroke -- mid-typing "4" for "45" is not itself an out-of-range
        # value worth fighting).
        self.layup_entries["wind_angle"].bind("<FocusOut>", self._validate_wind_angle)
        self.layup_entries["wind_angle"].bind("<Return>", self._validate_wind_angle)
        self._cycles_field = AutoEntry(self, self.layup_entries["passes"],
                                       lambda: self.app.layups[self.app.active_layup]["auto_cycles"],
                                       self._cycles_mode_changed)

        # Side by side, equal widths: the primary action filled, the secondary
        # one outlined in the same color.
        actions = ttk.Frame(left_panel)
        actions.pack(fill=tk.X, pady=(4, 2))
        actions.columnconfigure((0, 1), weight=1, uniform="actions")
        self.generate_btn = ttk.Button(actions, text="Generate G-Code", command=self.app.generate, style="primary.TButton")
        self.generate_btn.grid(row=0, column=0, sticky="ew", padx=(0, 3), ipady=4)
        ttk.Button(actions, text="Open G-Code", command=lambda: self.app.view_tab.open_gcode_view(),
                   style="primary.Outline.TButton").grid(row=0, column=1, sticky="ew", padx=(3, 0), ipady=4)
        # Opens the page for continuing an interrupted wind (see partial_page).
        ttk.Button(actions, text="Export Partial G-Code…", command=self.app.show_partial_page,
                   style="primary.Outline.TButton").grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0), ipady=1)

        # Visualization Section. Bottom strip first (so it claims its height
        # before the row above expands): the "Show All Layups" toggle plus the
        # legend it reveals. Above it, left to right: rotate-view arrows, the
        # main (pseudo-3D) tank canvas, a thin end-on view keeping the eye-reach
        # lines, and the Estimates sidebar. Packed right-to-left/then-left-to-right
        # so the visual order ends up: [arrows][3D canvas][end view][estimates].
        bottom_bar = ttk.Frame(self.viz_parent)
        bottom_bar.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))
        ttk.Checkbutton(bottom_bar, text="Show All Layups", variable=self.show_all_var,
                        style="Settings.TCheckbutton", command=self._on_show_all_toggled).pack(side=tk.LEFT, anchor="n")
        self.legend_canvas = tk.Canvas(bottom_bar, height=1, highlightthickness=0, bd=0)
        self.legend_canvas.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(18, 0))
        self.legend_canvas.bind("<Configure>", lambda e: self._draw_legend())

        viz_row = ttk.Frame(self.viz_parent)
        viz_row.pack(fill=tk.BOTH, expand=True)

        self._build_estimates_panel(viz_row)

        self.end_canvas = tk.Canvas(viz_row, bg=self.app.canvas_palette["canvas_bg"], relief="flat",
                                     borderwidth=1, highlightthickness=1,
                                     highlightbackground=self.app.canvas_palette["shaft_outline"], width=110)
        self.end_canvas.pack(side=tk.RIGHT, fill=tk.Y, padx=(6, 6))
        self.end_canvas.bind("<Configure>", lambda e: self.schedule_redraw())

        rotate_frame = ttk.Frame(viz_row, padding=(0, 0, 4, 0))
        rotate_frame.pack(side=tk.LEFT, fill=tk.Y)
        rotate_frame.rowconfigure(0, weight=1)
        rotate_frame.rowconfigure(3, weight=1)
        ttk.Frame(rotate_frame).grid(row=0, column=0)
        pal = self.app.canvas_palette
        up_btn = tk.Button(rotate_frame, text="▲",
                            relief="flat", bd=0, highlightthickness=0, font=("TkDefaultFont", 13),
                            fg=pal["muted"], activeforeground=pal["tank"], cursor="hand2")
        up_btn.grid(row=1, column=0, pady=(0, 4))
        up_btn.bind("<ButtonPress-1>", lambda e: self._set_spin_direction(-1))
        up_btn.bind("<ButtonRelease-1>", lambda e: self._set_spin_direction(0))
        up_btn.bind("<Leave>", lambda e: self._set_spin_direction(0))
        down_btn = tk.Button(rotate_frame, text="▼",
                              relief="flat", bd=0, highlightthickness=0, font=("TkDefaultFont", 13),
                              fg=pal["muted"], activeforeground=pal["tank"], cursor="hand2")
        down_btn.grid(row=2, column=0, pady=(4, 0))
        down_btn.bind("<ButtonPress-1>", lambda e: self._set_spin_direction(1))
        down_btn.bind("<ButtonRelease-1>", lambda e: self._set_spin_direction(0))
        down_btn.bind("<Leave>", lambda e: self._set_spin_direction(0))
        ttk.Frame(rotate_frame).grid(row=3, column=0)
        self._rotate_buttons = (up_btn, down_btn)

        self.canvas = tk.Canvas(viz_row, bg=self.app.canvas_palette["canvas_bg"], relief="flat",
                                 borderwidth=1, highlightthickness=1,
                                 highlightbackground=self.app.canvas_palette["shaft_outline"])
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", lambda e: self.schedule_redraw())
        self.on_cap_type_change()

    def _build_estimates_panel(self, parent):
        # Two groups: "Program" figures cover the whole winding program (every
        # layup back to back); the second group covers only the layup currently
        # selected in the settings panel, and follows the layup switcher.
        panel = ttk.Frame(parent, padding=(10, 0))
        panel.pack(side=tk.RIGHT, fill=tk.Y)
        panel.columnconfigure(1, weight=1)
        heading_font = ("TkHeadingFont", 10, "bold")
        self.est = {}
        row = 0

        def heading(text=None, textvariable=None, top=0):
            nonlocal row
            ttk.Label(panel, text=text, textvariable=textvariable, font=heading_font).grid(
                row=row, column=0, columnspan=2, sticky="w", pady=(top, 4))
            row += 1

        def estimate(key, name, initial, style=None, on_click=None):
            nonlocal row
            self.est[key] = tk.StringVar(value=initial)
            ttk.Label(panel, text=name).grid(row=row, column=0, sticky="w", pady=1, padx=(0, 14))
            value = ttk.Label(panel, textvariable=self.est[key], anchor="e", **({"style": style} if style else {}))
            value.grid(row=row, column=1, sticky="e", pady=1)
            if on_click:
                # Clickable to cycle units, styled identically to every other
                # readout here (no color/underline) so it doesn't announce itself
                # -- just a cursor change on hover as the only hint.
                value.configure(cursor="hand2")
                value.bind("<Button-1>", on_click)
            row += 1
            return value

        heading("Program")
        estimate("time", "Time", "00:00:00")
        estimate("tow", "Required Tow", "0.00 m")
        estimate("cycles", "Cycles", "-")
        estimate("rotation", "Rotation Speed", "0.0 rpm", on_click=self._cycle_rotation_speed_unit)
        estimate("reach", "Eye Reach", "- to - mm", style="warning.TLabel")
        self.layup_heading_var = tk.StringVar(value="Layup 1")
        heading(textvariable=self.layup_heading_var, top=12)
        estimate("layup_time", "Time", "00:00:00")
        estimate("cycle_time", "Time (1 Cycle)", "00:00:00")
        estimate("layup_tow", "Required Tow", "0.00 m")
        estimate("xspeed", "X-Speed", "0.00 mm/s", on_click=self._cycle_xspeed_unit)
        estimate("coverage", "Cycles for Full Coverage", "-")
        # How much of the surface the layup's bands cover with its Number of
        # Cycles; shown in the warning color while gaps would remain.
        self._coverage_label = estimate("coverage_pct", "Coverage", "-")
        estimate("extra", "Extra Rotation", "-- / --")

        # Raw values cached in their base unit so clicking just re-formats them,
        # with no recalculation needed -- rotation speed is always exactly Max
        # Rotation Speed (see winding.calc_move), and X-speed follows from that
        # and the selected layup's winding angle.
        self._rotation_speed_deg_per_min = 0.0
        self._rotation_speed_unit_idx = 0
        self.ROTATION_SPEED_UNITS = ["rpm", "°/s", "°/min"]
        self._xspeed_mm_s = 0.0
        self._xspeed_unit_idx = 0
        self.XSPEED_UNITS = ["mm/s", "m/s"]

    # --- Clickable-unit Estimates readouts ---

    def _cycle_rotation_speed_unit(self, event=None):
        self._rotation_speed_unit_idx = (self._rotation_speed_unit_idx + 1) % len(self.ROTATION_SPEED_UNITS)
        self._update_rotation_speed_display()

    def _update_rotation_speed_display(self):
        unit = self.ROTATION_SPEED_UNITS[self._rotation_speed_unit_idx]
        deg_per_min = self._rotation_speed_deg_per_min
        if unit == "rpm":
            val = deg_per_min / 360.0
        elif unit == "°/s":
            val = deg_per_min / 60.0
        else:
            val = deg_per_min
        self.est["rotation"].set(f"{val:.1f} {unit}")

    def _cycle_xspeed_unit(self, event=None):
        self._xspeed_unit_idx = (self._xspeed_unit_idx + 1) % len(self.XSPEED_UNITS)
        self._update_xspeed_display()

    def _update_xspeed_display(self):
        unit = self.XSPEED_UNITS[self._xspeed_unit_idx]
        val = self._xspeed_mm_s if unit == "mm/s" else self._xspeed_mm_s / 1000.0
        self.est["xspeed"].set(f"{val:.2f} {unit}")

    # --- Auto/custom fields: Number of Cycles and Start Wind at X ---

    def _cycles_mode_changed(self, auto):
        n = len(self.app.layups)
        prefix = f"Layup {self.app.active_layup + 1}: " if n > 1 else ""
        value = self.layup_entries["passes"].get().strip()
        if auto:
            self.app.set_status(f"{prefix}Number of Cycles is back on auto -- {value} cycles for full coverage.")
        else:
            self.app.set_status(f"{prefix}Number of Cycles set to {value} and fixed there. "
                                "Clear the field to put it back on auto (full coverage).")

    def _start_x_mode_changed(self, auto):
        value = self._start_x_entry.get().strip()
        if auto:
            self.app.set_status(f"Start Wind at X is back on auto -- {value} mm, where the dome ends and the tank turns straight.")
        else:
            self.app.set_status(f"Start Wind at X set to {value} mm and fixed there. "
                                "Clear the field to put it back on auto (where the dome ends).")

    def _sync_auto_values(self, job):
        # Writes each auto setting's computed value into its field -- for the
        # layups not on screen too, so switching shows the right number at once.
        # A field the user is typing in is left alone until they leave it, so a
        # cleared field isn't refilled under their cursor.
        editing = self._cycles_field.editing()
        for i, (layup_vars, layup) in enumerate(zip(self.app.layups, job.layups)):
            if not layup.auto_cycles or (editing and i == self.app.active_layup):
                continue
            if winding.full_coverage_cycles(job.tank_diameter, layup.wind_angle, job.bandwidth, layup.pattern_number) is None:
                continue  # not computable right now; geometry_errors() explains why
            _set_if_changed(layup_vars["passes"], layup.passes)
        if job.start_x_auto and not self._start_x_field.editing():
            _set_if_changed(self.app.params["start_x"], round(job.start_x, 1))
        # Start Wind at X only applies when homing first.
        state = ["!disabled"] if job.home_before_wind else ["disabled"]
        self._start_x_entry.state(state)
        self._start_x_label.state(state)
        self._cycles_field.refresh()
        self._start_x_field.refresh()

    def _redraw_now(self):
        if self.redraw_timer:
            self.app.root.after_cancel(self.redraw_timer)
            self.redraw_timer = None
        self.draw_visualization()

    # --- Layup switching ---

    def on_layups_changed(self):
        # Called by the app after any layup add/remove/select/load: re-points the
        # layup entry fields at the selected layup's own Tk variables (each layup
        # keeps its values -- even half-typed ones -- in its own variables, so
        # switching never has to copy or re-parse anything), then redraws.
        n, i = len(self.app.layups), self.app.active_layup
        for key, entry in self.layup_entries.items():
            entry.configure(textvariable=self.app.layups[i][key])
        self.layup_title_var.set(f"Layup {i + 1} of {n}")
        self.layup_heading_var.set(f"Layup {i + 1}" if n > 1 else "Layup")
        self._prev_btn.configure(state="normal" if i > 0 else "disabled")
        self._next_btn.configure(state="normal" if i < n - 1 else "disabled")
        self._remove_btn.configure(state="normal" if n > 1 else "disabled")
        self._draw_layup_swatch()
        self._cycles_field.refresh()
        self._cycles_field.rebind()
        # Redraw right away rather than debounced: nothing here is slow (a
        # switch that doesn't change any setting redraws from the cached
        # simulation), and an instant response is what makes switching feel snappy.
        if self.redraw_timer:
            self.app.root.after_cancel(self.redraw_timer)
            self.redraw_timer = None
        self.draw_visualization()

    def _draw_layup_swatch(self):
        c = self._layup_swatch
        c.delete("all")
        c.configure(bg=self.app.root.style.colors.bg)
        theme.draw_layup_swatch(c, 0, 0, self.app.canvas_palette, self.app.active_layup, SWATCH_W, SWATCH_H)

    def _step_layup(self, delta):
        self._validate_wind_angle()  # the layup being left
        self.app.select_layup(self.app.active_layup + delta)

    def _add_layup(self):
        self._validate_wind_angle()
        self.app.add_layup()
        n = len(self.app.layups)
        self.app.set_status(f"Layup {n} added with the settings of Layup {n - 1}; its Number of Cycles is on auto.")

    def _remove_layup(self):
        index = self.app.active_layup
        self.app.remove_layup(index)
        self.app.set_status(f"Layup {index + 1} removed.")

    def _on_show_all_toggled(self):
        self._draw_legend()
        self._draw_caption()
        self._draw_start_marker()
        self._redraw_strands()

    def describe_settings_error(self, error):
        label = FIELD_LABELS.get(error.key, error.key)
        where = f" in Layup {error.layup_index + 1}" if error.layup_index is not None and len(self.app.layups) > 1 else ""
        return f"'{label}'{where} must be a number."

    def _validate_wind_angle(self, event=None):
        layup_vars = self.app.layups[self.app.active_layup]
        try:
            lt = float(self.app.params["tank_length"].get())
            dt = float(self.app.params["tank_diameter"].get())
            bandwidth = float(self.app.params["bandwidth"].get())
            angle = float(layup_vars["wind_angle"].get())
        except (tk.TclError, ValueError):
            return  # field is mid-edit / not a valid number yet -- leave it alone
        angle_min, angle_max = winding.compute_wind_angle_bounds(lt, dt, bandwidth)
        which = f" for Layup {self.app.active_layup + 1}" if len(self.app.layups) > 1 else ""
        if angle < angle_min:
            layup_vars["wind_angle"].set(round(angle_min, 2))
            self.app.set_status(f"Winding Angle{which} raised to {angle_min:.2f}° -- the lowest realistic angle for this tank/tow width (below it, the strand barely advances around the tank at all).")
        elif angle > angle_max:
            layup_vars["wind_angle"].set(round(angle_max, 2))
            self.app.set_status(f"Winding Angle{which} lowered to {angle_max:.2f}° -- the highest realistic angle for this tank/tow width (above it, each wrap would overlap the last).")

    def on_cap_type_change(self, *args):
        if self.app.params["end_cap_type"].get() == "Flat":
            self.tank_entries["end_cap_diameter"].config(state="disabled")
        else:
            self.tank_entries["end_cap_diameter"].config(state="normal")
        self.schedule_redraw()

    def on_theme_changed(self):
        self._draw_layup_swatch()
        self.draw_visualization()

    def estimated_total_time(self):
        """Total program time (s) from the latest finished simulation, or None."""
        return self._result[1].total_time if self._result else None

    # --- Drawing ---

    def schedule_redraw(self, *args):
        # A short debounce coalesces bursts of change events (e.g. every keystroke
        # while typing a number) into a single update, without needing to wait for
        # the field to lose focus -- the preview still feels live.
        if self.redraw_timer: self.app.root.after_cancel(self.redraw_timer)
        self.redraw_timer = self.app.root.after(60, self.draw_visualization)

    def draw_visualization(self):
        # Everything here is cheap (geometry only, no strand simulation) so it can
        # run synchronously on every settings change without ever freezing the GUI.
        # The potentially slow strand-path/time/tow simulation is dispatched to a
        # background thread at the end of this method instead of being run inline
        # -- unless the cached result already belongs to these exact settings.
        self.redraw_timer = None
        pal = self.app.canvas_palette
        self.canvas.delete("all")
        self.canvas.configure(bg=pal["canvas_bg"], highlightbackground=pal["shaft_outline"])
        for btn in self._rotate_buttons:
            btn.configure(fg=pal["muted"], activeforeground=pal["tank"])
        self.generate_btn.config(state="disabled")
        self._view_geom = None
        c_w, c_h = self.canvas.winfo_width(), self.canvas.winfo_height()

        try:
            job = self.app.build_job()
        except winding.SettingsError as e:  # a field is empty / mid-edit
            self._job = None
            if c_w >= 100 and c_h >= 100:
                self.canvas.create_text(c_w / 2, c_h / 2, text=self.describe_settings_error(e),
                                        fill=pal["muted"], font=("TkDefaultFont", 10))
            self._draw_legend()
            self._cycles_field.refresh()
            self._start_x_field.refresh()
            self._update_indicator()
            return
        self._job = job
        self._sync_auto_values(job)
        layup = job.layups[self.app.active_layup]
        self._update_instant_estimates(job, layup)
        self._optimize_check.state(["disabled"] if job.turnaround_zone > 0 else ["!disabled"])

        geometry_errors = winding.geometry_errors(job)
        errors = winding.validate(job)
        if not errors: self.generate_btn.config(state="normal")

        if c_w >= 100 and c_h >= 100 and job.tank_length > 0 and job.tank_diameter > 0:
            self._draw_tank(job, c_w, c_h)
            self._draw_end_view(job.tank_diameter / 2, job.eye_arm_length)
            self._draw_start_marker()
        self._draw_warnings(errors, c_w)
        self._draw_caption()
        self._draw_legend()

        if geometry_errors:
            self._sim_blocked = True
            if self.partial_point is not None and self.on_progress is not None:
                self.on_progress(None, None, winding.PointError(geometry_errors[0]))
        else:
            self._sim_blocked = False
            if self._result is not None and self._result[0] == job:
                self._redraw_strands()
            else:
                self._request_calc(job)
            if self.partial_point is not None:
                self._update_progress()
        self._update_sim_estimates()
        self._update_indicator()

    def _draw_tank(self, job, c_w, c_h):
        pal = self.app.canvas_palette
        co, lt, dt, dc, cap_type, ld = (job.chuck_offset, job.tank_length, job.tank_diameter,
                                        job.end_cap_diameter, job.end_cap_type, job.dome_length)
        rt, rc = dt / 2, dc / 2

        # Scale and offset: center the view on the tank's X-center (chuck_offset +
        # tank_length/2), accounting for the shaft's extension further left of the
        # tank so neither side of the drawing gets clipped off-canvas.
        half_extent = max(co + lt / 2.0, lt / 2.0 + 100)
        scale = min((c_w / 2.0 - 40) / half_extent, (c_h - 100) / (winding.Y_REFERENCE + 100))
        ox = c_w / 2.0 + (co + lt / 2.0) * scale
        oy = 150
        self._view_geom = (scale, ox, oy)

        # Draw center line / shaft
        shaft_w = (dc if cap_type == "Round" else dt) * scale
        self.canvas.create_rectangle(ox - (co + lt + 100) * scale, oy - shaft_w/2, ox, oy + shaft_w/2, fill=pal["shaft"], outline=pal["shaft_outline"])

        # Draw Tank (with a subtle vertical shading band to read as a cylinder
        # rather than a flat rectangle -- the outer silhouette itself never
        # changes with the view-rotation buttons, since a solid of revolution
        # looks identical from any angle around its own axis).
        tank_top, tank_bottom = [], []
        for i in range(101):
            x = i * (lt / 100); r = winding.calc_R(x, 0, lt, rt, rc, ld, cap_type)
            tank_top.append((ox - (co + x) * scale, oy - r * scale))
            tank_bottom.append((ox - (co + x) * scale, oy + r * scale))
        self._tank_outline = tank_top + tank_bottom[::-1]
        self.canvas.create_polygon(self._tank_outline, fill=pal["tank"], outline=pal["tank_outline"], width=2)
        self._draw_cylinder_shading(tank_top, tank_bottom)

    def _draw_start_marker(self):
        # Where the wind starts -- Start Wind at X, or the tank's end without
        # homing -- shown with the first layup only, whose first pass begins
        # there. A dashed line across the tank on a halo (so it reads over any
        # strand color), a pointer, and a label; the arrow shows the winding
        # direction: +X runs to the left in this view.
        self.canvas.delete("start_marker")
        job = self._job
        if (job is None or not self._view_geom or self.partial_point is not None
                or not (self.app.active_layup == 0 or self.show_all_var.get())):
            return
        self._draw_marker(job.wind_start_x, "start_marker", f"Wind start  X {job.wind_start_x:.1f}", direction_arrow=True)

    def _draw_marker(self, x, tag, text, direction_arrow=False):
        # A machine position across the tank: a dashed line on a halo (so it
        # reads over any strand color), a pointer and a label above.
        self.canvas.delete(tag)
        job, pal = self._job, self.app.canvas_palette
        scale, ox, oy = self._view_geom
        r = winding.calc_R(x - job.chuck_offset, 0, job.tank_length, job.tank_diameter / 2, job.end_cap_diameter / 2,
                           job.dome_length, job.end_cap_type)
        px, top, bottom = ox - x * scale, oy - r * scale - 8, oy + r * scale + 8
        self.canvas.create_line(px, top, px, bottom, fill=pal["marker_halo"], width=4, tags=tag)
        self.canvas.create_line(px, top, px, bottom, fill=pal["marker"], width=2, dash=(6, 3), tags=tag)
        self.canvas.create_polygon(px - 5, top - 8, px + 5, top - 8, px, top - 1, fill=pal["marker"], outline="", tags=tag)
        label = self.canvas.create_text(px, top - 12, text=text, anchor="s", fill=pal["marker"],
                                        font=("TkDefaultFont", 9, "bold"), tags=tag)
        if direction_arrow:
            x0, y0, x1, y1 = self.canvas.bbox(label)
            self.canvas.create_line(x0 - 6, (y0 + y1) / 2, x0 - 30, (y0 + y1) / 2, fill=pal["marker"], width=2,
                                    arrow=tk.LAST, arrowshape=(7, 8, 3), tags=tag)

    def _draw_warnings(self, errors, c_w):
        # Stacked top-center, each wrapped to the canvas width, measured from the
        # previous one's actual height so long (wrapped) messages never overlap.
        if c_w < 100: return
        pal = self.app.canvas_palette
        y = 12
        for message in errors[:3]:
            item = self.canvas.create_text(c_w / 2, y, text=f"⚠ {message}", fill=pal["warn_text"], anchor="n",
                                           justify="center", width=max(100, c_w - 60), font=("TkDefaultFont", 10, "bold"))
            y = self.canvas.bbox(item)[3] + 4

    def _draw_caption(self):
        # A quiet reminder of what the strand overlay shows, bottom-left. Omitted
        # for single-layup programs, where it would only state the obvious.
        self.canvas.delete("caption")
        n = len(self.app.layups)
        c_h = self.canvas.winfo_height()
        if c_h < 100 or self._job is None: return
        if self.partial_point is not None:
            if self._progress is None or self._progress[0] != self._progress_key() or isinstance(self._progress[1], Exception):
                return
            location = self._progress[1].location
            p = location.point
            text = (f"Wound up to Layup {p.layup + 1} of {n} · Cycle {p.cycle + 1} of "
                    f"{self._job.layups[p.layup].passes} · A {p.angle:.1f}°")
        elif n <= 1:
            return
        elif self.show_all_var.get():
            text = f"All {n} layups · first cycle of each"
        else:
            text = f"Layup {self.app.active_layup + 1} of {n} · first cycle"
        self.canvas.create_text(10, c_h - 8, text=text, anchor="sw", fill=self.app.canvas_palette["muted"],
                                font=("TkDefaultFont", 9), tags="caption")

    def _draw_legend(self):
        # A wrapping row of [swatch] "Layup N · angle · cycles" keys under the
        # preview, shown only with "Show All Layups". The selected layup is set
        # in bold, and clicking any key selects that layup in the settings panel.
        lc = self.legend_canvas
        lc.delete("all")
        colors = self.app.root.style.colors
        lc.configure(bg=colors.bg)
        job = self._job
        if not self.show_all_var.get() or job is None:
            if int(lc.cget("height")) != 1: lc.configure(height=1)
            return
        width = max(lc.winfo_width(), 200)
        row_h, gap, pad = 18, 20, 6
        x = y = 0
        for i, layup in enumerate(job.layups):
            font = self._legend_fonts[1] if i == self.app.active_layup else self._legend_fonts[0]
            cycles = f"{layup.passes} cycle{'s' if layup.passes != 1 else ''}"
            text = f"Layup {i + 1}  ·  {layup.wind_angle:g}°  ·  {cycles}"
            item_w = SWATCH_W + pad + font.measure(text)
            if x > 0 and x + item_w > width:
                x, y = 0, y + row_h
            tag = f"legend_{i}"
            theme.draw_layup_swatch(lc, x, y + (row_h - SWATCH_H) / 2 - 1, self.app.canvas_palette, i,
                                    SWATCH_W, SWATCH_H, tags=(tag,))
            lc.create_text(x + SWATCH_W + pad, y + row_h / 2 - 1, text=text, anchor="w", font=font,
                           fill=colors.fg, tags=(tag,))
            lc.tag_bind(tag, "<Button-1>", lambda e, idx=i: self._select_from_legend(idx))
            lc.tag_bind(tag, "<Enter>", lambda e: lc.configure(cursor="hand2"))
            lc.tag_bind(tag, "<Leave>", lambda e: lc.configure(cursor=""))
            x += item_w + gap
        height = y + row_h
        if int(lc.cget("height")) != height:
            lc.configure(height=height)

    def _select_from_legend(self, index):
        self._validate_wind_angle()
        self.app.select_layup(index)

    def _update_instant_estimates(self, job, layup):
        # Everything here is cheap closed-form math, so it updates instantly on
        # every change (and every layup switch) without the background simulation.
        eye_dist_min, eye_dist_max = winding.eye_reach(job.eye_arm_length)
        self.est["reach"].set(f"{eye_dist_min:.0f} to {eye_dist_max:.0f} mm")
        n = len(job.layups)
        self.est["cycles"].set(f"{job.total_cycles}" + (f" in {n} layups" if n > 1 else ""))

        # Rotation Speed and X-Speed both follow directly from Max Rotation
        # Speed (the machine's rotation is always governed by it exactly, see
        # winding.calc_move) plus the tank diameter / winding angle.
        self._rotation_speed_deg_per_min = job.max_a_speed
        self._update_rotation_speed_display()
        angle_ok = 0 < layup.wind_angle < 90
        self._xspeed_mm_s = job.max_surface_speed / math.tan(math.radians(layup.wind_angle)) if angle_ok else 0.0
        self._update_xspeed_display()

        cycles_needed = winding.full_coverage_cycles(job.tank_diameter, layup.wind_angle, job.bandwidth, layup.pattern_number)
        self.est["coverage"].set(str(cycles_needed) if cycles_needed else "-")

        # Extra Rotation (within the layup's first cycle): the pattern-alignment
        # "extra" rotation needed on top of the base turnaround dwell, split
        # between the far-end (return) turnaround -- which gets the same fixed
        # share every circuit -- and the chuck-end (starting position)
        # turnaround, which absorbs the rest (see
        # compute_turnaround_balance_offset). Shown for the first circuit.
        if angle_ok and layup.passes >= 1 and layup.pattern_number >= 1 and job.tank_diameter > 0 and job.bandwidth > 0:
            plan = winding.plan_layup(job, layup)
            left_offset = plan.extra_after(0, layup.pattern_number) - plan.right_offset
            self.est["extra"].set(f"{plan.right_offset:.1f}° / {left_offset:.1f}°")
            coverage = plan.coverage
            self.est["coverage_pct"].set(f"{coverage * 100:.0f} %")
            # Rounded display, so compare against what's shown: "100 %" never
            # turns orange over a sub-percent shortfall.
            gaps = round(coverage * 100) < 100
            self._coverage_label.configure(foreground=self.app.canvas_palette["warn_text"] if gaps else "")
        else:
            self.est["extra"].set("-- / --")
            self.est["coverage_pct"].set("-")
            self._coverage_label.configure(foreground="")

    def _update_sim_estimates(self):
        # Figures that need the full simulation. While a newer calculation is
        # running they keep showing the previous result (the "Calculating..."
        # indicator flags that); they're blanked only when the current settings
        # can't be simulated at all.
        keys = ("time", "tow", "layup_time", "cycle_time", "layup_tow")
        if self._sim_blocked or self._result is None:
            for key in keys: self.est[key].set("--")
            return
        job, result = self._result
        self.est["time"].set(format_hms(result.total_time))
        self.est["tow"].set(f"{result.total_tow / 1000.0:.2f} m")
        i = self.app.active_layup
        if i < len(result.layups):
            lr = result.layups[i]
            self.est["layup_time"].set(format_hms(lr.time))
            # Average time per cycle -- individual cycles can vary a little (the
            # extra turnaround rotation that keeps the pattern aligned differs
            # slightly cycle to cycle), so this is the layup's time split evenly
            # across its number of cycles.
            self.est["cycle_time"].set(format_hms(lr.time / max(1, job.layups[i].passes)))
            self.est["layup_tow"].set(f"{lr.tow / 1000.0:.2f} m")
        else:
            # A layup that was just added isn't part of the cached result yet.
            for key in ("layup_time", "cycle_time", "layup_tow"): self.est[key].set("…")

    # --- Background calculation plumbing ---

    def _request_calc(self, job):
        self._sim.request(job)

    @property
    def job(self):
        """The WindingJob currently shown, or None while a field holds invalid input."""
        return self._job

    def calculating(self):
        """True while a background calculation is running or queued."""
        return any(c.busy or c.pending is not None for c in (self._sim, self._prog))

    def _poll_calc_queue(self):
        # Runs on the GUI thread via after(): delivers finished calculations.
        self._sim.poll()
        self._prog.poll()
        self._update_indicator()
        self.app.root.after(50, self._poll_calc_queue)

    def _apply_calc_result(self, job, result, error):
        if error is not None:
            print(f"Calc error: {error}")
            return
        self._result = (job, result)
        self._update_sim_estimates()
        self._redraw_strands()
        self.report_progress()  # remaining time needs the total

    # --- Continuing a wind (the partial page) ---

    def set_partial_point(self, point):
        """Show the tank as wound up to `point` (a winding.ProgramPoint) instead of
        the layups' first cycles -- or go back to normal with None."""
        if point == self.partial_point:
            return
        self.partial_point = point
        self._redraw_now()

    def _progress_key(self):
        return (self._job, self.partial_point)

    def _update_progress(self):
        # From draw_visualization: draw the cached progress if it matches what's
        # on screen, otherwise have it calculated.
        if self._progress is not None and self._progress[0] == self._progress_key():
            self._redraw_strands()
            self.report_progress()
        else:
            self._prog.request(self._progress_key())

    def _apply_progress_result(self, key, result, error):
        self._progress = (key, result if error is None else error)
        if key == self._progress_key():
            self._redraw_strands()
            self.report_progress()

    def report_progress(self):
        # Tell the partial page where the point is (or what's wrong with it).
        if self.on_progress is None or self.partial_point is None:
            return
        if self._progress is None or self._progress[0] != self._progress_key():
            self.on_progress(None, None, None)
            return
        outcome = self._progress[1]
        if isinstance(outcome, Exception):
            self.on_progress(None, None, outcome)
        else:
            total = self._result[1] if self._result is not None and self._result[0] == self._job else None
            self.on_progress(outcome.location, total, None)

    # --- Strands ---

    def _redraw_strands(self):
        # Draws the wound strands: normally the selected layup's first cycle --
        # or, with "Show All Layups", every layup's first cycle -- and while the
        # partial page is open, the tank as wound up to the continue point.
        # Only front-facing path segments are drawn; which portions count as
        # "front" depends on self._view_azimuth, so this alone is re-run (cheaply)
        # on every rotate-animation frame, while the tank/shaft/end-view stay
        # untouched. Only results calculated from the exact settings on screen
        # are ever drawn.
        self.canvas.delete("strand_path")
        if self.partial_point is not None:
            self._draw_progress()
        elif self._view_geom and self._result is not None and self._result[0] == self._job:
            job, result = self._result
            indices = range(len(result.layups)) if self.show_all_var.get() else [self.app.active_layup]
            # In winding order, so later layups lie on top of earlier ones like
            # they do on the real tank.
            for li in indices:
                self._draw_runs(li, [run for runs in result.layups[li].strand_runs for run in runs])
        # Strand paths draw on top of everything else drawn synchronously (tank,
        # warnings); keep the markers, caption and calc indicator above that.
        for tag in ("start_marker", "continue_marker", "eye", "caption", "calc_indicator"):
            self.canvas.tag_raise(tag)

    def _draw_runs(self, layup_index, runs):
        scale, ox, oy = self._view_geom
        pal = self.app.canvas_palette
        color, dash = theme.layup_style(pal, layup_index)
        style = dict(fill=color, width=max(1, int(self._job.bandwidth * scale)), capstyle=tk.ROUND, dash=dash or "",
                     tags="strand_path")
        az = math.radians(self._view_azimuth)
        for run in runs:
            poly = []
            for x, r, a in run:
                eff = math.radians(a) - az
                px, py = ox - x * scale, oy - r * math.cos(eff) * scale
                if math.sin(eff) >= 0:
                    poly.append((px, py))
                else:
                    if len(poly) >= 2:
                        self.canvas.create_line(*[c for p in poly for c in p], **style)
                    poly = []
            if len(poly) >= 2:
                self.canvas.create_line(*[c for p in poly for c in p], **style)

    def _draw_progress(self):
        # The tank as it should look at the continue point: every layup wound
        # before it, in winding order -- one that covers the whole tank as a
        # solid layer of its color (every band overlaps the next, so that's what
        # the tank looks like), others and the point's own layup as the bands
        # wound so far -- then the eye where it should be, and a marker.
        self.canvas.delete("continue_marker", "eye")
        if not self._view_geom or self._progress is None or self._progress[0] != self._progress_key():
            return
        progress = self._progress[1]
        if isinstance(progress, Exception):
            return
        pal = self.app.canvas_palette
        for li in range(progress.location.point.layup + 1):
            if li in progress.covered and self._tank_outline:
                color, _ = theme.layup_style(pal, li)
                self.canvas.create_polygon(self._tank_outline, fill=color, outline=pal["tank_outline"], width=2,
                                           tags="strand_path")
            elif li in progress.runs:
                self._draw_runs(li, progress.runs[li])
        self._draw_eye(progress.location)
        self._draw_marker(progress.location.x, "continue_marker",
                          f"Continue here  X {progress.location.x:.1f}  A {progress.location.point.angle:.1f}°")
        self._draw_caption()

    def _draw_eye(self, location):
        # The eye at the continue point, as in the G-Code Preview: its arm (in
        # the eye-reach color) reaching up to the eye, drawn to scale across its
        # width, keeping min_spacing from the tank.
        job, (scale, ox, oy) = self._job, self._view_geom
        pal = self.app.canvas_palette
        px = ox - location.x * scale
        eye_y = oy + (winding.Y_REFERENCE - job.eye_arm_length - location.y) * scale
        base_y = oy + (winding.Y_REFERENCE - job.eye_arm_length) * scale
        half_w = max(3.0, job.eye_width / 2 * scale)
        self.canvas.create_line(px, base_y, px, eye_y, fill=pal["reach"], width=3, tags="eye")
        self.canvas.create_rectangle(px - half_w, eye_y - 4, px + half_w, eye_y + 4, fill=pal["marker"],
                                     outline=pal["marker_halo"], tags="eye")

    # --- View rotation (up/down arrows) ---
    # Velocity-based rather than a per-press "ease toward a target" animation: an
    # earlier version re-triggered a position-ease curve on every hold-repeat tick,
    # but a cubic ease-in-out has zero slope at its start, so restarting it every
    # ~80ms meant the view barely got moving before being reset again -- it visibly
    # slowed the longer the button was held, then lurched through the whole
    # backlog of queued steps in one 350ms burst once released. Tracking actual
    # angular velocity (ramped by acceleration while held, ramped back down by
    # deceleration once released) avoids that entirely: holding longer just means
    # cruising at a steady top speed for longer, and releasing always decelerates
    # smoothly from whatever speed it was actually at.

    def _set_spin_direction(self, direction):
        self._spin_direction = direction
        if self._spin_job is None:
            self._spin_last_time = time.monotonic()
            self._spin_tick()

    def _spin_tick(self):
        now = time.monotonic()
        dt = max(0.0, min(0.05, now - self._spin_last_time))
        self._spin_last_time = now
        target_v = self._spin_direction * self.SPIN_MAX_SPEED_DEG_S
        rate = self.SPIN_ACCEL_DEG_S2 if self._spin_direction != 0 else self.SPIN_DECEL_DEG_S2
        if self._spin_velocity < target_v:
            self._spin_velocity = min(target_v, self._spin_velocity + rate * dt)
        elif self._spin_velocity > target_v:
            self._spin_velocity = max(target_v, self._spin_velocity - rate * dt)
        self._view_azimuth = (self._view_azimuth + self._spin_velocity * dt) % 360.0
        self._redraw_strands()
        if self._spin_direction != 0 or abs(self._spin_velocity) > 0.5:
            self._spin_job = self.app.root.after(16, self._spin_tick)
        else:
            self._spin_velocity = 0.0
            self._spin_job = None

    # --- Cosmetic pseudo-3D tank shading ---

    def _draw_cylinder_shading(self, tank_top, tank_bottom):
        # Adds a lighter "highlight" curve and a darker "shadow" curve between the
        # tank's centerline and its outline, purely cosmetic, to read as a lit
        # cylinder rather than a flat 2D outline.
        pal = self.app.canvas_palette
        highlight = [(x, oy_top + (oy_bot - oy_top) * 0.28) for (x, oy_top), (_, oy_bot) in zip(tank_top, tank_bottom)]
        shadow = [(x, oy_top + (oy_bot - oy_top) * 0.82) for (x, oy_top), (_, oy_bot) in zip(tank_top, tank_bottom)]
        self.canvas.create_line(*[c for p in highlight for c in p], fill=pal["highlight"], width=2, smooth=True)
        self.canvas.create_line(*[c for p in shadow for c in p], fill=pal["shadow"], width=2, smooth=True)

    # --- Thin end-on view: keeps the eye Min/Max reach lines from the old 2D view ---

    def _draw_end_view(self, rt, arm):
        pal = self.app.canvas_palette
        self.end_canvas.configure(bg=pal["canvas_bg"], highlightbackground=pal["shaft_outline"])
        self.end_canvas.delete("all")
        w, h = self.end_canvas.winfo_width(), self.end_canvas.winfo_height()
        if w < 20 or h < 20:
            return
        eye_dist_min, eye_dist_max = winding.eye_reach(arm)
        cx, cy = w / 2.0, h / 2.0
        scale2 = min((w / 2.0 - 10) / max(rt, 1.0), (h / 2.0 - 15) / max(eye_dist_max, 1.0))
        scale2 = max(scale2, 0.001)
        r_px = rt * scale2
        self.end_canvas.create_oval(cx - r_px, cy - r_px, cx + r_px, cy + r_px, fill=pal["tank"], outline=pal["tank_outline"], width=2)
        y_min = cy - eye_dist_min * scale2
        y_max = cy - eye_dist_max * scale2
        self.end_canvas.create_line(4, y_min, w - 4, y_min, fill=pal["reach"], dash=(4, 4))
        self.end_canvas.create_text(w / 2.0, y_min - 9, text="Max", fill=pal["reach"], font=("TkDefaultFont", 8))
        self.end_canvas.create_line(4, y_max, w - 4, y_max, fill=pal["reach"], dash=(4, 4))
        self.end_canvas.create_text(w / 2.0, y_max + 9, text="Min", fill=pal["reach"], font=("TkDefaultFont", 8))

    def _update_indicator(self):
        self.canvas.delete("calc_indicator")
        calcs = (self._sim, self._prog)
        if not any(c.busy for c in calcs): return
        pal = self.app.canvas_palette
        if any(c.busy and c.pending is not None for c in calcs):
            # A newer change has already arrived and will start as soon as the
            # in-flight calculation finishes -- flag that the preview shown once
            # this one lands will immediately be stale.
            text, color = "⏳ Calculating... (update queued)", pal["warn_text"]
        else:
            text, color = "⏳ Calculating...", pal["muted"]
        c_w = self.canvas.winfo_width()
        self.canvas.create_text(c_w - 10, 10, text=text, fill=color, anchor="ne", font=("TkDefaultFont", 9, "italic"), tags="calc_indicator")
