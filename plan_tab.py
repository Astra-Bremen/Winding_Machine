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
FIELD_LABELS = {key: label for label, key in MACHINE_FIELDS + TANK_FIELDS + WINDING_FIELDS + LAYUP_FIELDS}

LEGEND_FONT = ("TkDefaultFont", 8)
LEGEND_FONT_ACTIVE = ("TkDefaultFont", 8, "bold")
SWATCH_W, SWATCH_H = 24, 12


def _format_hms(seconds):
    h, m = divmod(int(seconds), 3600); m, s = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


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
        # The strand-path simulation can be slow for long winds, so it always runs
        # on a worker thread instead of the GUI thread. Only one calculation is ever
        # in flight; if settings change again while it's running, the new request
        # just overwrites `_pending` (there is only one pending slot), which is
        # exactly how an outdated queued calculation gets "cancelled" -- it's
        # simply replaced before it ever gets to start. Each request gets a unique
        # token, and only the result for `_wanted_token` (the newest settings) is
        # ever drawn; anything else that arrives is stale and discarded.
        #
        # Every request is keyed by its immutable winding.WindingJob, and the last
        # finished result is cached against it. Anything that doesn't change the
        # job -- switching the displayed layup, toggling "Show All Layups",
        # resizing, rotating the view -- redraws straight from that cache without
        # re-running the simulation, which is what keeps layup switching instant.
        self._calc_queue = queue.Queue()
        self._calc_thread = None
        self._next_token = 0
        self._wanted_token = None
        self._pending = None       # (token, job) waiting for the worker
        self._inflight = None      # (token, job) the worker is simulating right now
        self._result = None        # (job, winding.SimulationResult) of the last finished simulation
        self._job = None           # job currently shown; None while a field holds invalid input
        self._sim_blocked = False  # True while the current job can't be simulated at all
        self._view_geom = None     # (scale, ox, oy) the tank is currently drawn with

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
        FRAME_PAD, FRAME_GAP = (8, 5), (0, 6)

        def add_fields(frame, fields, first_row=0, store=None):
            for i, (label_text, key) in enumerate(fields):
                ttk.Label(frame, text=label_text, style=LBL).grid(row=first_row + i, column=0, sticky="w", pady=1, padx=(0, 10))
                # Layup entries are bound to a variable later (on_layups_changed),
                # since which layup's variables they edit changes on every switch.
                var = self.app.params.get(key)
                entry = ttk.Entry(frame, textvariable=var, width=12, style=ENT) if var is not None else ttk.Entry(frame, width=12, style=ENT)
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
                        style="Settings.TCheckbutton").grid(row=len(MACHINE_FIELDS), column=0, columnspan=2, sticky="w", pady=(8, 0))

        # 2. Tank Settings
        tank_frame = ttk.LabelFrame(left_panel, text="Tank Settings", padding=FRAME_PAD, style=FRM)
        tank_frame.pack(fill=tk.X, pady=FRAME_GAP)
        ttk.Label(tank_frame, text="End-Cap Type", style=LBL).grid(row=0, column=0, sticky="w", pady=1, padx=(0, 10))
        self.cap_type_combo = ttk.Combobox(tank_frame, textvariable=self.app.params["end_cap_type"], values=["Round", "Flat"], state="readonly", width=10, style=CMB)
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
        self._optimize_check.grid(row=len(WINDING_FIELDS), column=0, columnspan=2, sticky="w", pady=(6, 0))

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
        self._setup_cycles_field(self.layup_entries["passes"])

        # Side by side, equal widths: the primary action filled, the secondary
        # one outlined in the same color.
        actions = ttk.Frame(left_panel)
        actions.pack(fill=tk.X, pady=(4, 2))
        actions.columnconfigure((0, 1), weight=1, uniform="actions")
        self.generate_btn = ttk.Button(actions, text="Generate G-Code", command=self.app.generate, style="primary.TButton")
        self.generate_btn.grid(row=0, column=0, sticky="ew", padx=(0, 3), ipady=4)
        ttk.Button(actions, text="Open G-Code", command=lambda: self.app.view_tab.open_gcode_view(),
                   style="primary.Outline.TButton").grid(row=0, column=1, sticky="ew", padx=(3, 0), ipady=4)

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

    # --- Number of Cycles: auto (full coverage) or custom ---
    # Auto: the app keeps the field at the cycles needed for 100 % coverage (see
    # winding.WindingJob), shown in grey with an "AUTO" tag inside the field.
    # Typing a number makes it custom: normal text, no tag, never touched again.
    # Clearing the field and leaving it (or pressing Enter) puts it back on auto.

    def _setup_cycles_field(self, entry):
        # Key validation fires only for the user's own typing/pasting -- never
        # for the app setting the variable -- which is exactly the line between
        # "custom" and "auto" here. It never rejects anything.
        entry.configure(validate="key", validatecommand=(entry.register(self._on_cycles_typed), "%P"))
        entry.bind("<FocusIn>", self._on_cycles_focus_in, add="+")
        entry.bind("<FocusOut>", self._on_cycles_focus_out, add="+")
        entry.bind("<Return>", self._on_cycles_focus_out, add="+")
        self._cycles_tag = tk.Label(entry, text="AUTO", font=("TkDefaultFont", 7, "bold"), bd=0, padx=0, pady=0,
                                    cursor="xterm")
        # The tag sits on top of the field, so a click on it has to reach the field.
        self._cycles_tag.bind("<Button-1>", lambda e: (entry.focus_set(), "break")[1])
        self._cycles_mode_at_focus = None  # auto flag when the field gained focus
        self._fill_focused_cycles = False  # one-shot: refill the field even though it has focus

    def _active_cycles_auto(self):
        return bool(self.app.layups[self.app.active_layup]["auto_cycles"].get())

    def _cycles_field_has_focus(self):
        try:
            return self.app.root.focus_get() is self.layup_entries["passes"]
        except KeyError:  # focus is in a Tk-internal widget (e.g. a combobox popdown)
            return False

    def _on_cycles_typed(self, new_text):
        # An emptied field counts as auto straight away (the preview follows at
        # once); it's refilled with the computed value when the user leaves it.
        auto = new_text.strip() == ""
        auto_var = self.app.layups[self.app.active_layup]["auto_cycles"]
        if bool(auto_var.get()) != auto:
            auto_var.set(auto)
            self._update_cycles_field()
        return True

    def _on_cycles_focus_in(self, event=None):
        self._cycles_mode_at_focus = self._active_cycles_auto()
        if self._cycles_mode_at_focus:
            # Select the computed value, so typing replaces it rather than
            # appending to it. Deferred until the click has placed the cursor.
            entry = self.layup_entries["passes"]
            self.app.root.after_idle(lambda: self._cycles_field_has_focus() and entry.select_range(0, tk.END))

    def _on_cycles_focus_out(self, event=None):
        auto = self._active_cycles_auto()
        if auto:
            self._fill_focused_cycles = True  # also on Enter, where focus stays put
            if self.redraw_timer:
                self.app.root.after_cancel(self.redraw_timer)
            self.draw_visualization()
        if self._cycles_mode_at_focus is not None and self._cycles_mode_at_focus != auto:
            n = len(self.app.layups)
            prefix = f"Layup {self.app.active_layup + 1}: " if n > 1 else ""
            value = self.layup_entries["passes"].get().strip()
            if auto:
                self.app.set_status(f"{prefix}Number of Cycles is back on auto -- {value} cycles for full coverage.")
            else:
                self.app.set_status(f"{prefix}Number of Cycles set to {value} and fixed there. "
                                    "Clear the field to put it back on auto (full coverage).")
        self._cycles_mode_at_focus = auto if self._cycles_field_has_focus() else None

    def _sync_auto_cycles(self, job):
        # Writes each auto layup's computed cycle count into its field (for the
        # layups not on screen too, so switching shows the right number at once).
        # The field the user is currently editing is left alone until they leave
        # it, so a cleared field isn't refilled under their cursor.
        editing = self._cycles_field_has_focus() and not self._fill_focused_cycles
        self._fill_focused_cycles = False
        for i, (layup_vars, layup) in enumerate(zip(self.app.layups, job.layups)):
            if not layup.auto_cycles or (editing and i == self.app.active_layup):
                continue
            if winding.full_coverage_cycles(job.tank_diameter, layup.wind_angle, job.bandwidth, layup.pattern_number) is None:
                continue  # not computable right now; geometry_errors() explains why
            var = layup_vars["passes"]
            try:
                current = var.get()
            except tk.TclError:
                current = None
            if current != layup.passes:
                var.set(layup.passes)
        self._update_cycles_field()

    def _update_cycles_field(self):
        entry, tag = self.layup_entries["passes"], self._cycles_tag
        if self._active_cycles_auto():
            color = self.app.canvas_palette["auto_text"]
            entry.configure(foreground=color)
            tag.configure(fg=color, bg=self.app.root.style.colors.inputbg)
            tag.place(relx=1.0, rely=0.5, x=-7, anchor="e")
        else:
            entry.configure(foreground="")  # back to the theme's normal text color
            tag.place_forget()

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
        self._update_cycles_field()
        if self._cycles_field_has_focus():
            # Switched layups while in the field: it now edits another layup.
            self._cycles_mode_at_focus = self._active_cycles_auto()
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
            self._update_cycles_field()
            self._update_indicator()
            return
        self._job = job
        self._sync_auto_cycles(job)
        layup = job.layups[self.app.active_layup]
        self._update_instant_estimates(job, layup)
        self._optimize_check.state(["disabled"] if job.turnaround_zone > 0 else ["!disabled"])

        geometry_errors = winding.geometry_errors(job)
        errors = winding.validate(job)
        if not errors: self.generate_btn.config(state="normal")

        if c_w >= 100 and c_h >= 100 and job.tank_length > 0 and job.tank_diameter > 0:
            self._draw_tank(job, c_w, c_h)
            self._draw_end_view(job.tank_diameter / 2, job.eye_arm_length)
        self._draw_warnings(errors, c_w)
        self._draw_caption()
        self._draw_legend()

        if geometry_errors:
            self._sim_blocked = True
        else:
            self._sim_blocked = False
            if self._result is not None and self._result[0] == job:
                self._redraw_strands()
            else:
                self._request_calc(job)
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
        self.canvas.create_polygon(tank_top + tank_bottom[::-1], fill=pal["tank"], outline=pal["tank_outline"], width=2)
        self._draw_cylinder_shading(tank_top, tank_bottom)

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
        if n <= 1 or c_h < 100 or self._job is None: return
        if self.show_all_var.get():
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
        self.est["time"].set(_format_hms(result.total_time))
        self.est["tow"].set(f"{result.total_tow / 1000.0:.2f} m")
        i = self.app.active_layup
        if i < len(result.layups):
            lr = result.layups[i]
            self.est["layup_time"].set(_format_hms(lr.time))
            # Average time per cycle -- individual cycles can vary a little (the
            # extra turnaround rotation that keeps the pattern aligned differs
            # slightly cycle to cycle), so this is the layup's time split evenly
            # across its number of cycles.
            self.est["cycle_time"].set(_format_hms(lr.time / max(1, job.layups[i].passes)))
            self.est["layup_tow"].set(f"{lr.tow / 1000.0:.2f} m")
        else:
            # A layup that was just added isn't part of the cached result yet.
            for key in ("layup_time", "cycle_time", "layup_tow"): self.est[key].set("…")

    # --- Background calculation plumbing ---

    def _request_calc(self, job):
        if self._inflight is not None and self._inflight[1] == job:
            # These exact settings are already being simulated (e.g. a value was
            # changed and changed straight back) -- drop anything queued behind.
            self._pending = None
            self._wanted_token = self._inflight[0]
            return
        if self._pending is not None and self._pending[1] == job:
            return
        self._next_token += 1
        self._wanted_token = self._next_token
        self._pending = (self._next_token, job)
        if self._calc_thread is None or not self._calc_thread.is_alive():
            self._launch_calc_thread()

    def _launch_calc_thread(self):
        if self._pending is None:
            return
        self._inflight, self._pending = self._pending, None
        t = threading.Thread(target=self._calc_worker, args=self._inflight, daemon=True)
        self._calc_thread = t
        t.start()

    def _calc_worker(self, token, job):
        # Runs on a background thread: pure math only, no Tkinter access.
        try:
            self._calc_queue.put((token, job, winding.simulate(job), None))
        except Exception as e:
            self._calc_queue.put((token, job, None, e))

    def _poll_calc_queue(self):
        # Runs on the GUI thread via after(); this is the only place background
        # results are allowed to touch the canvas/Tk variables.
        try:
            while True:
                token, job, result, err = self._calc_queue.get_nowait()
                self._calc_thread = None
                self._inflight = None
                if token == self._wanted_token:
                    # Still the most recent request -- nothing changed while it ran.
                    if err is not None: print(f"Calc error: {err}")
                    else: self._apply_calc_result(job, result)
                # A stale result (superseded by a newer request while this one was
                # running) is simply discarded here.
                if self._pending is not None:
                    self._launch_calc_thread()
        except queue.Empty:
            pass
        self._update_indicator()
        self.app.root.after(50, self._poll_calc_queue)

    def _apply_calc_result(self, job, result):
        self._result = (job, result)
        self._update_sim_estimates()
        self._redraw_strands()

    def _redraw_strands(self):
        # Draws the selected layup's first cycle -- or, with "Show All Layups",
        # every layup's first cycle in its own identity color, in winding order so
        # later layups lie on top of earlier ones like they do on the real tank.
        # Only front-facing path segments are drawn; which portions count as
        # "front" depends on self._view_azimuth, so this alone is re-run (cheaply)
        # on every rotate-animation frame, while the tank/shaft/end-view stay
        # untouched. Only a result simulated from the exact settings on screen
        # is ever drawn.
        self.canvas.delete("strand_path")
        if not self._view_geom or self._result is None or self._result[0] != self._job:
            return
        job, result = self._result
        scale, ox, oy = self._view_geom
        path_w = max(1, int(job.bandwidth * scale))
        pal = self.app.canvas_palette
        az = math.radians(self._view_azimuth)
        indices = range(len(result.layups)) if self.show_all_var.get() else [self.app.active_layup]
        for li in indices:
            color, dash = theme.layup_style(pal, li)
            style = dict(fill=color, width=path_w, capstyle=tk.ROUND, dash=dash or "", tags="strand_path")
            for runs in result.layups[li].strand_runs:
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
        # Strand paths draw on top of everything else drawn synchronously (tank,
        # warnings); keep the caption and calc indicator above that in turn.
        self.canvas.tag_raise("caption")
        self.canvas.tag_raise("calc_indicator")

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
        calculating = self._calc_thread is not None and self._calc_thread.is_alive()
        if not calculating: return
        pal = self.app.canvas_palette
        if self._pending is not None:
            # A newer change has already arrived and will start as soon as the
            # in-flight calculation finishes -- flag that the preview shown once
            # this one lands will immediately be stale.
            text, color = "⏳ Calculating... (update queued)", pal["warn_text"]
        else:
            text, color = "⏳ Calculating...", pal["muted"]
        c_w = self.canvas.winfo_width()
        self.canvas.create_text(c_w - 10, 10, text=text, fill=color, anchor="ne", font=("TkDefaultFont", 9, "italic"), tags="calc_indicator")
