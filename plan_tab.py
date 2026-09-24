import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import math
import threading
import queue
import time

class PlanTab:
    def __init__(self, settings_parent, viz_parent, app):
        self.settings_parent = settings_parent
        self.viz_parent = viz_parent
        self.app = app
        self.canvas = None
        self.redraw_timer = None
        self.tank_entries = {}

        # --- Background calculation state ---
        # The strand-path simulation can be slow for long winds, so it always runs
        # on a worker thread instead of the GUI thread. Only one calculation is ever
        # in flight; if settings change again while it's running, the new request
        # just overwrites `_pending_snapshot` (there is only one pending slot), which
        # is exactly how an outdated queued calculation gets "cancelled" -- it's
        # simply replaced before it ever gets to start. Results are matched back to
        # the request that produced them via an incrementing token so a result that
        # arrives after newer settings have already been entered is discarded rather
        # than drawn.
        self._calc_queue = queue.Queue()
        self._calc_thread = None
        self._latest_token = 0
        self._pending_snapshot = None

        # --- 3D-ish strand view rotation state ---
        # The tank's outer silhouette looks identical from any angle around its own
        # axis (it's a solid of revolution), so "rotating" the view doesn't need to
        # touch the tank drawing at all -- only which parts of the wound strand are
        # front-facing (visible) vs. wrapped around the back changes. That split is
        # cheap trig over the cached raw simulation points, so rotating can be
        # redrawn every animation frame without re-running the background simulation.
        self._raw_strand_runs = None
        self._raw_geom = None
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

        self.setup_ui()
        self.app.root.after(50, self._poll_calc_queue)

    def setup_ui(self):
        left_panel = self.settings_parent

        # A slightly smaller body font than the rest of the app so all three
        # settings groups comfortably fit on screen without scrolling; see
        # theme.setup_compact_styles for the actual font sizes.
        LBL, ENT, CMB, FRM = "Settings.TLabel", "Settings.TEntry", "Settings.TCombobox", "Settings.TLabelframe"

        # 1. Machine Settings
        machine_frame = ttk.LabelFrame(left_panel, text="Machine Settings", padding="8", style=FRM)
        machine_frame.pack(fill=tk.X, pady=(0, 8))
        machine_labels = [
            ("Chuck X-Offset (mm)", "chuck_offset"),
            ("Eye Arm Length (mm)", "eye_arm_length"),
            ("Eye Width (mm)", "eye_width"),
            ("Min Spacing (Safety) (mm)", "min_spacing"),
            ("Max Rotation Speed (mm/s)", "max_surface_speed")
        ]
        for i, (label_text, var_name) in enumerate(machine_labels):
            ttk.Label(machine_frame, text=label_text, style=LBL).grid(row=i, column=0, sticky="w", pady=2, padx=(0, 10))
            entry = ttk.Entry(machine_frame, textvariable=self.app.params[var_name], width=12, style=ENT)
            entry.grid(row=i, column=1, sticky="e", pady=2)
            self.app.params[var_name].trace_add("write", self.schedule_redraw)

        # A plain ttk.Checkbutton, same as "Optimize Trajectory" in Winding
        # Settings -- just the compact Settings font, no color/Toolbutton style.
        ttk.Checkbutton(machine_frame, text="Home Axes (G28) Before Winding",
                        variable=self.app.params["home_before_wind"],
                        style="Settings.TCheckbutton").grid(row=len(machine_labels), column=0, columnspan=2, sticky="w", pady=(8, 0))

        # 2. Tank Settings
        tank_frame = ttk.LabelFrame(left_panel, text="Tank Settings", padding="8", style=FRM)
        tank_frame.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(tank_frame, text="End-Cap Type", style=LBL).grid(row=0, column=0, sticky="w", pady=2, padx=(0, 10))
        self.cap_type_combo = ttk.Combobox(tank_frame, textvariable=self.app.params["end_cap_type"], values=["Round", "Flat"], state="readonly", width=10, style=CMB)
        self.cap_type_combo.grid(row=0, column=1, sticky="e", pady=2)
        self.app.params["end_cap_type"].trace_add("write", self.on_cap_type_change)

        tank_labels = [("Tank Length (mm)", "tank_length"), ("Tank Diameter (mm)", "tank_diameter"), ("End Cap Diameter (mm)", "end_cap_diameter")]
        for i, (label_text, var_name) in enumerate(tank_labels):
            ttk.Label(tank_frame, text=label_text, style=LBL).grid(row=i+1, column=0, sticky="w", pady=2, padx=(0, 10))
            entry = ttk.Entry(tank_frame, textvariable=self.app.params[var_name], width=12, style=ENT)
            entry.grid(row=i+1, column=1, sticky="e", pady=2)
            self.tank_entries[var_name] = entry
            self.app.params[var_name].trace_add("write", self.schedule_redraw)

        # 3. Winding Settings
        winding_frame = ttk.LabelFrame(left_panel, text="Winding Settings", padding="8", style=FRM)
        winding_frame.pack(fill=tk.X, pady=(0, 8))
        winding_labels = [
            ("Number of Cycles", "passes"), ("Pattern Number (Strands per Cycle)", "pattern_number"),
            ("Winding Angle (Degrees)", "wind_angle"),
            ("Bandwidth / Tow Width (mm)", "bandwidth"), ("Turnaround / Dwell Angle (Deg)", "turnaround_angle")
        ]
        for i, (label_text, var_name) in enumerate(winding_labels):
            ttk.Label(winding_frame, text=label_text, style=LBL).grid(row=i, column=0, sticky="w", pady=2, padx=(0, 10))
            entry = ttk.Entry(winding_frame, textvariable=self.app.params[var_name], width=12, style=ENT)
            entry.grid(row=i, column=1, sticky="e", pady=2)
            self.app.params[var_name].trace_add("write", self.schedule_redraw)
            if var_name == "wind_angle":
                # Angles too close to 0/90 deg aren't physically realizable for
                # the current tank size/tow width (see compute_wind_angle_bounds)
                # and are what caused the "calculates forever" symptom. Only
                # auto-correct once the user is actually done editing this one
                # field (not on every keystroke -- mid-typing "4" for "45" is
                # not itself an out-of-range value worth fighting).
                entry.bind("<FocusOut>", self._validate_wind_angle)
                entry.bind("<Return>", self._validate_wind_angle)

        # Experimental: eases a small piece of each dwell's rotation into the
        # traversal steps flanking it instead of one abrupt stop, so Klipper's
        # motion planner doesn't need to slow to a near-halt at the turnaround.
        # A plain ttk.Checkbutton (just a font-size tweak via the Settings style,
        # not a color/Toolbutton style) -- a real checkbox is the most immediately
        # recognizable widget for a plain on/off setting like this.
        ttk.Checkbutton(winding_frame, text="Optimize Trajectory (experimental)",
                        variable=self.app.params["optimize_trajectory"],
                        style="Settings.TCheckbutton",
                        command=self.schedule_redraw).grid(row=len(winding_labels), column=0, columnspan=2, sticky="w", pady=(6, 0))

        self.generate_btn = ttk.Button(left_panel, text="Generate G-Code", command=self.app.generate, style="primary.TButton")
        self.generate_btn.pack(pady=(15, 5), fill=tk.X, ipady=4)
        ttk.Button(left_panel, text="Open G-Code", command=lambda: self.app.view_tab.open_gcode_view(),
                   style="secondary.Outline.TButton").pack(pady=(0, 15), fill=tk.X, ipady=2)

        # Visualization Section, left to right: rotate-view arrows, the main
        # (pseudo-3D) tank canvas, a thin end-on view keeping the eye-reach lines,
        # and a slim Estimates sidebar. Packed right-to-left/then-left-to-right so
        # the visual order ends up: [arrows][3D canvas][end view][estimates].
        viz_row = ttk.Frame(self.viz_parent)
        viz_row.pack(fill=tk.BOTH, expand=True)

        estimates_panel = ttk.Frame(viz_row, padding=(10, 0))
        estimates_panel.pack(side=tk.RIGHT, fill=tk.Y)
        self.est_time_var = tk.StringVar(value="Time: 00:00:00")
        self.est_cycle_time_var = tk.StringVar(value="Time (1 Cycle): 00:00:00")
        self.est_tow_var = tk.StringVar(value="Required Tow: 0.00 m")
        self.est_rotation_speed_var = tk.StringVar(value="Rotation Speed: 0.0 rpm")
        self.est_xspeed_var = tk.StringVar(value="X-Speed: 0.00 mm/s")
        self.est_cycles_var = tk.StringVar(value="Cycles for Full Coverage: -")
        self.est_extra_rotation_var = tk.StringVar(value="Extra Rotation: -- / --")
        self.est_reach_var = tk.StringVar(value="Eye Reach: - to - mm")

        # Raw values cached in their base unit so clicking just re-formats them,
        # with no recalculation needed -- rotation speed is always exactly Max
        # Rotation Speed now (see calc_move), and X-speed follows from that and
        # the winding angle.
        self._rotation_speed_deg_per_min = 0.0
        self._rotation_speed_unit_idx = 0
        self.ROTATION_SPEED_UNITS = ["rpm", "°/s", "°/min"]
        self._xspeed_mm_s = 0.0
        self._xspeed_unit_idx = 0
        self.XSPEED_UNITS = ["mm/s", "m/s"]

        # Tk's named fonts (TkDefaultFont/TkHeadingFont/TkFixedFont) resolve to
        # whatever the host OS's actual UI font is -- on Linux Mint that's a real
        # system font, not a Windows-only one like "Arial"/"Consolas" would be.
        ttk.Label(estimates_panel, text="Estimates", font=("TkHeadingFont", 10, "bold")).pack(anchor="w", pady=(0, 6))
        ttk.Label(estimates_panel, textvariable=self.est_time_var).pack(anchor="w", pady=2)
        ttk.Label(estimates_panel, textvariable=self.est_cycle_time_var).pack(anchor="w", pady=2)
        ttk.Label(estimates_panel, textvariable=self.est_tow_var).pack(anchor="w", pady=2)
        # Clickable to cycle units, styled identically to every other readout here
        # (no color/underline) so it doesn't announce itself -- just a cursor
        # change on hover as the only hint.
        rot_lbl = ttk.Label(estimates_panel, textvariable=self.est_rotation_speed_var, cursor="hand2")
        rot_lbl.pack(anchor="w", pady=2)
        rot_lbl.bind("<Button-1>", self._cycle_rotation_speed_unit)
        xspeed_lbl = ttk.Label(estimates_panel, textvariable=self.est_xspeed_var, cursor="hand2")
        xspeed_lbl.pack(anchor="w", pady=2)
        xspeed_lbl.bind("<Button-1>", self._cycle_xspeed_unit)
        ttk.Label(estimates_panel, textvariable=self.est_cycles_var).pack(anchor="w", pady=2)
        ttk.Label(estimates_panel, textvariable=self.est_extra_rotation_var).pack(anchor="w", pady=2)
        ttk.Label(estimates_panel, textvariable=self.est_reach_var, style="warning.TLabel").pack(anchor="w", pady=2)

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
        self.est_rotation_speed_var.set(f"Rotation Speed: {val:.1f} {unit}")

    def _cycle_xspeed_unit(self, event=None):
        self._xspeed_unit_idx = (self._xspeed_unit_idx + 1) % len(self.XSPEED_UNITS)
        self._update_xspeed_display()

    def _update_xspeed_display(self):
        unit = self.XSPEED_UNITS[self._xspeed_unit_idx]
        val = self._xspeed_mm_s if unit == "mm/s" else self._xspeed_mm_s / 1000.0
        self.est_xspeed_var.set(f"X-Speed: {val:.2f} {unit}")

    def _validate_wind_angle(self, event=None):
        try:
            lt = float(self.app.params["tank_length"].get())
            dt = float(self.app.params["tank_diameter"].get())
            bandwidth = float(self.app.params["bandwidth"].get())
            angle = float(self.app.params["wind_angle"].get())
        except (tk.TclError, ValueError):
            return  # field is mid-edit / not a valid number yet -- leave it alone
        angle_min, angle_max = self.app.compute_wind_angle_bounds(lt, dt, bandwidth)
        if angle < angle_min:
            self.app.params["wind_angle"].set(round(angle_min, 2))
            self.app.set_status(f"Winding Angle raised to {angle_min:.2f}° -- the lowest realistic angle for this tank/tow width (below it, the strand barely advances around the tank at all).")
        elif angle > angle_max:
            self.app.params["wind_angle"].set(round(angle_max, 2))
            self.app.set_status(f"Winding Angle lowered to {angle_max:.2f}° -- the highest realistic angle for this tank/tow width (above it, each wrap would overlap the last).")

    def on_cap_type_change(self, *args):
        if self.app.params["end_cap_type"].get() == "Flat":
            self.tank_entries["end_cap_diameter"].config(state="disabled")
        else:
            self.tank_entries["end_cap_diameter"].config(state="normal")
        self.schedule_redraw()

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
        # background thread at the end of this method instead of being run inline.
        pal = self.app.canvas_palette
        self.canvas.delete("all")
        self.canvas.configure(bg=pal["canvas_bg"], highlightbackground=pal["shaft_outline"])
        for btn in self._rotate_buttons:
            btn.configure(fg=pal["muted"], activeforeground=pal["tank"])
        self.generate_btn.config(state="disabled")
        c_w, c_h = self.canvas.winfo_width(), self.canvas.winfo_height()
        if c_w < 100 or c_h < 100: return
        try:
            co, lt, dt, dc, arm, space = [float(self.app.params[k].get()) for k in ["chuck_offset", "tank_length", "tank_diameter", "end_cap_diameter", "eye_arm_length", "min_spacing"]]
            eye_width = float(self.app.params["eye_width"].get())
            max_surface_speed, cap_type = float(self.app.params["max_surface_speed"].get()), self.app.params["end_cap_type"].get()
            max_a_speed = self.app.surface_speed_to_deg_per_min(max_surface_speed, dt)
            passes, wind_angle, bandwidth, dwell = int(self.app.params["passes"].get()), float(self.app.params["wind_angle"].get()), float(self.app.params["bandwidth"].get()), float(self.app.params["turnaround_angle"].get())
            pattern_number = int(self.app.params["pattern_number"].get())
            optimize_trajectory = self.app.params["optimize_trajectory"].get()
            rt, rc, ld = dt/2, dc/2, 0.0
            if cap_type == "Round": ld = min(math.sqrt(max(0, rt**2 - rc**2)), lt/2)

            # Scale and offset: center the view on the tank's X-center (chuck_offset +
            # tank_length/2), accounting for the shaft's extension further left of the
            # tank so neither side of the drawing gets clipped off-canvas.
            half_extent = max(co + lt / 2.0, lt / 2.0 + 100)
            scale = min((c_w / 2.0 - 40) / half_extent, (c_h - 100) / (550 + 100))
            ox = c_w / 2.0 + (co + lt / 2.0) * scale
            oy = 150

            # Draw center line / shaft
            shaft_w = (dc if cap_type == "Round" else dt) * scale
            self.canvas.create_rectangle(ox - (co + lt + 100) * scale, oy - shaft_w/2, ox, oy + shaft_w/2, fill=pal["shaft"], outline=pal["shaft_outline"])

            # Draw Tank (with a subtle vertical shading band to read as a cylinder
            # rather than a flat rectangle -- the outer silhouette itself never
            # changes with the view-rotation buttons, since a solid of revolution
            # looks identical from any angle around its own axis).
            tank_top, tank_bottom = [], []
            for i in range(101):
                x = i * (lt / 100); r = self.app.calc_R(x, 0, lt, rt, rc, ld, cap_type)
                tank_top.append((ox - (co + x) * scale, oy - r * scale))
                tank_bottom.append((ox - (co + x) * scale, oy + r * scale))
            self.canvas.create_polygon(tank_top + tank_bottom[::-1], fill=pal["tank"], outline=pal["tank_outline"], width=2)
            self._draw_cylinder_shading(tank_top, tank_bottom)

            # Eye-Reach: when Y-axis moves, the eye tip moves between
            # (550 - arm - 180) and (550 - arm - 0) mm from the tank centerline.
            # The lines themselves now live in the thin end-view panel; the numbers
            # still show up in the Estimates sidebar.
            eye_dist_min = 550 - arm - 180
            eye_dist_max = 550 - arm - 0
            self.est_reach_var.set(f"Eye Reach: {eye_dist_min:.0f} to {eye_dist_max:.0f} mm")
            x_left = ox - (co + lt + 100) * scale

            # Rotation Speed and X-Speed both follow directly from Max Rotation
            # Speed (the machine's rotation is always governed by it exactly, see
            # calc_move) plus the tank diameter / winding angle -- no simulation
            # needed, so these update instantly like Eye Reach does.
            self._rotation_speed_deg_per_min = max_a_speed
            self._update_rotation_speed_display()
            if 0 < wind_angle < 90:
                self._xspeed_mm_s = max_surface_speed / math.tan(math.radians(wind_angle))
            else:
                self._xspeed_mm_s = 0.0
            self._update_xspeed_display()

            pattern_ok = self.app.pattern_is_valid(passes, pattern_number)

            # Extra Rotation (within the first cycle): the pattern-alignment
            # "extra" rotation needed on top of the base turnaround dwell, split
            # between the far-end (return) turnaround -- which gets the same
            # fixed share every circuit -- and the chuck-end (starting position)
            # turnaround, which absorbs the rest (see
            # compute_turnaround_balance_offset). Shown for the first circuit
            # specifically; cheap pure math, so it updates instantly like Eye
            # Reach/Rotation Speed/X-Speed rather than needing the background
            # strand simulation.
            if pattern_ok and 0 < wind_angle < 90:
                pitch_pattern = (math.pi * dt) / math.tan(math.radians(wind_angle))
                traverse_rotation_pattern = (2.0 * lt / pitch_pattern) * 360.0
                shift_deg_pattern = (bandwidth / math.cos(math.radians(wind_angle)) / (math.pi * dt)) * 360.0
                extra_within, extra_between = self.app.compute_dwell_extras(traverse_rotation_pattern, dwell, 360.0 / pattern_number, shift_deg_pattern)
                total_circuits = pattern_number * passes
                right_offset = self.app.compute_turnaround_balance_offset(total_circuits, pattern_number, dwell, extra_within, extra_between)
                # Circuit 0's own "extra" -- 0 if it's also the very last circuit
                # (nothing follows it to align to), otherwise whichever of
                # extra_within/extra_between applies to it, exactly matching
                # generate()'s own per-circuit logic.
                if total_circuits <= 1:
                    first_extra = 0.0
                else:
                    first_extra = extra_between if pattern_number == 1 else extra_within
                left_offset = first_extra - right_offset
                self.est_extra_rotation_var.set(f"Extra Rotation: {right_offset:.1f}° / {left_offset:.1f}°")
            else:
                self.est_extra_rotation_var.set("Extra Rotation: -- / --")
            if not pattern_ok:
                self.canvas.create_text(x_left + 5, oy + 280, text="⚠ Number of Cycles & Pattern Number must both be at least 1!", fill=pal["warn_text"], anchor="w")

            x_max_reached = co + lt
            x_ok = self.app.x_bounds_ok(co, lt)
            if not x_ok:
                self.canvas.create_text(c_w / 2, 20, text=f"⚠ X-Axis would reach {x_max_reached:.0f}mm, exceeding the carriage's {self.app.MAX_X:.0f}mm limit!", fill=pal["warn_text"], anchor="n", font=("TkDefaultFont", 11, "bold"))

            if pattern_ok and x_ok: self.generate_btn.config(state="normal")

            self._draw_end_view(rt, arm)

            # Hand the (potentially slow) strand-path/time/tow simulation off to a
            # background thread -- everything it needs is captured here as plain
            # values so the worker never touches Tkinter. Projection to screen
            # coordinates happens separately (see _redraw_strands), since it also
            # depends on the current view-rotation azimuth, which can change without
            # needing to re-run the simulation itself.
            snapshot = dict(co=co, lt=lt, dt=dt, dc=dc, ld=ld, arm=arm, space=space,
                             passes=passes, wind_angle=wind_angle, bandwidth=bandwidth,
                             dwell=dwell, max_a_speed=max_a_speed,
                             scale=scale, ox=ox, oy=oy, cap_type=cap_type,
                             pattern_number=pattern_number, eye_width=eye_width,
                             optimize_trajectory=optimize_trajectory)
            self._request_calc(snapshot)
        except Exception as e:
            print(f"Viz error: {e}")
        self._update_indicator()

    # --- Background calculation plumbing ---

    def _request_calc(self, snapshot):
        self._latest_token += 1
        self._pending_snapshot = (self._latest_token, snapshot)
        if self._calc_thread is None or not self._calc_thread.is_alive():
            self._launch_calc_thread()

    def _launch_calc_thread(self):
        if self._pending_snapshot is None:
            return
        token, snapshot = self._pending_snapshot
        self._pending_snapshot = None
        t = threading.Thread(target=self._calc_worker, args=(token, snapshot), daemon=True)
        self._calc_thread = t
        t.start()

    def _calc_worker(self, token, snapshot):
        # Runs on a background thread: pure math only, no Tkinter access.
        try:
            result = self.app.simulate_winding(
                snapshot["co"], snapshot["lt"], snapshot["dt"], snapshot["dc"], snapshot["ld"],
                snapshot["arm"], snapshot["space"], snapshot["passes"], snapshot["wind_angle"],
                snapshot["bandwidth"], snapshot["dwell"], snapshot["max_a_speed"],
                snapshot["cap_type"], snapshot["pattern_number"], snapshot["eye_width"],
                snapshot["optimize_trajectory"])
            self._calc_queue.put((token, snapshot, result, None))
        except Exception as e:
            self._calc_queue.put((token, snapshot, None, e))

    def _poll_calc_queue(self):
        # Runs on the GUI thread via after(); this is the only place background
        # results are allowed to touch the canvas/Tk variables.
        try:
            while True:
                token, snapshot, result, err = self._calc_queue.get_nowait()
                self._calc_thread = None
                if err is not None:
                    if token == self._latest_token: print(f"Calc error: {err}")
                elif token == self._latest_token:
                    # Still the most recent request -- nothing changed while it ran.
                    self._apply_calc_result(snapshot, result)
                # A stale result (superseded by a newer request while this one was
                # running) is simply discarded here.
                if self._pending_snapshot is not None:
                    self._launch_calc_thread()
        except queue.Empty:
            pass
        self._update_indicator()
        self.app.root.after(50, self._poll_calc_queue)

    def _apply_calc_result(self, snapshot, result):
        total_time, total_tow, strand_runs = result
        h, m = divmod(int(total_time), 3600); m, s = divmod(m, 60)
        self.est_time_var.set(f"Time: {h:02d}:{m:02d}:{s:02d}")
        # Average time per cycle -- individual cycles can vary a little (the
        # extra turnaround rotation that keeps the pattern aligned differs
        # slightly cycle to cycle), so this is total time split evenly across
        # the number of cycles, same spirit as the other Estimates figures.
        cycle_time = total_time / max(1, snapshot["passes"])
        ch, cm = divmod(int(cycle_time), 3600); cm, cs = divmod(cm, 60)
        self.est_cycle_time_var.set(f"Time (1 Cycle): {ch:02d}:{cm:02d}:{cs:02d}")
        self.est_tow_var.set(f"Required Tow: {(total_tow/1000.0):.2f} m")
        cycles_needed = self.app.compute_cycles_for_full_coverage(snapshot["dt"], snapshot["wind_angle"], snapshot["bandwidth"], snapshot["pattern_number"])
        cycles_needed_int = math.ceil(cycles_needed) if cycles_needed > 0 else 0
        self.est_cycles_var.set(f"Cycles for Full Coverage: {cycles_needed_int}")

        # Cache the raw (unprojected) strand points and the geometry they were
        # computed against so the strand path can be redrawn from any view azimuth
        # -- including during a rotate-button animation -- without re-running the
        # (potentially slow) simulation.
        self._raw_strand_runs = strand_runs
        self._raw_geom = {"scale": snapshot["scale"], "ox": snapshot["ox"], "oy": snapshot["oy"], "bandwidth": snapshot["bandwidth"]}
        self._redraw_strands()

    def _redraw_strands(self):
        # Draws the finished layup after the first cycle: every strand's currently
        # front-facing path only, all in one color. Which portions count as "front"
        # depends on self._view_azimuth, so this alone is re-run (cheaply) on every
        # rotate-animation frame, while the tank/shaft/end-view stay untouched.
        self.canvas.delete("strand_path")
        if not self._raw_strand_runs or not self._raw_geom:
            return
        scale, ox, oy = self._raw_geom["scale"], self._raw_geom["ox"], self._raw_geom["oy"]
        path_w = max(1, int(self._raw_geom["bandwidth"] * scale))
        strand_color = self.app.canvas_palette["strand"]
        az = math.radians(self._view_azimuth)
        for runs in self._raw_strand_runs:
            for run in runs:
                poly = []
                for x, r, a in run:
                    eff = math.radians(a) - az
                    px, py = ox - x * scale, oy - r * math.cos(eff) * scale
                    if math.sin(eff) >= 0:
                        poly.append((px, py))
                    else:
                        if len(poly) >= 2:
                            self.canvas.create_line(*[c for p in poly for c in p], fill=strand_color, width=path_w, capstyle=tk.ROUND, tags="strand_path")
                        poly = []
                if len(poly) >= 2:
                    self.canvas.create_line(*[c for p in poly for c in p], fill=strand_color, width=path_w, capstyle=tk.ROUND, tags="strand_path")
        # Strand paths draw on top of everything else drawn synchronously (tank,
        # warnings); keep the calc indicator above that in turn.
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
        eye_dist_min = 550 - arm - 180
        eye_dist_max = 550 - arm - 0
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
        if self._pending_snapshot is not None:
            # A newer change has already arrived and will start as soon as the
            # in-flight calculation finishes -- flag that the preview shown once
            # this one lands will immediately be stale.
            text, color = "⏳ Calculating... (update queued)", pal["warn_text"]
        else:
            text, color = "⏳ Calculating...", pal["muted"]
        c_w = self.canvas.winfo_width()
        self.canvas.create_text(c_w - 10, 10, text=text, fill=color, anchor="ne", font=("TkDefaultFont", 9, "italic"), tags="calc_indicator")
