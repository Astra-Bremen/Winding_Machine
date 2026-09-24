import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import math
import theme
import winding
from theme import VIEWPORT_PALETTE as VP

class ViewTab:
    def __init__(self, parent, app):
        self.parent = parent
        self.app = app
        self.view_canvas = None
        self.gcode_commands = []
        self.view_settings = {}
        self._path_static = []
        self._path_substeps = []
        self._cache_key = None
        self.cmd_cycles = []
        self.cycle_starts = {}
        self.total_cycles = 0
        # Which layup (0-based) each command belongs to, from the file's
        # "; LAYUP_START:n" markers; files written before multi-layup support
        # have none, and are treated as one single layup.
        self.cmd_layups = []
        self.layup_count = 1
        self.layup_var = tk.StringVar(value="")
        self.gcode_path_var = tk.StringVar(value="No file loaded")
        self.timeline_var = tk.IntVar(value=0)
        self.line_var = tk.IntVar(value=0)
        self.line_total_var = tk.StringVar(value="/ 0")
        self.cycle_var = tk.IntVar(value=0)
        self.cycle_total_var = tk.StringVar(value="/ 0")
        self.coord_var = tk.StringVar(value="X:   0  Y:  0  A:     0.00")
        self.time_at_pos_var = tk.StringVar(value="00:00:00")
        self.cmd_times = []
        self.play_speed_var = tk.IntVar(value=10)
        # Off by default: plain 2D side view, matching the original representation.
        # Enabling it switches to the pseudo-3D view that co-rotates with the true
        # G-code rotation angle so the tank appears to actually spin while winding.
        self.auto_rotate_var = tk.BooleanVar(value=False)
        self.is_playing = False
        self.play_job = None
        self.play_button = None
        self.setup_ui()

    def setup_ui(self):
        top_bar = ttk.Frame(self.parent)
        top_bar.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(top_bar, textvariable=self.gcode_path_var).pack(side=tk.LEFT, padx=5)
        ttk.Label(top_bar, textvariable=self.layup_var, font=("TkDefaultFont", 9, "bold")).pack(side=tk.RIGHT, padx=5)

        view_content = ttk.Frame(self.parent)
        view_content.pack(fill=tk.BOTH, expand=True)
        # A fixed dark "viewport" regardless of the app's light/dark theme toggle
        # -- the same convention CAD/slicer render views commonly use, and a good
        # backdrop for the bright strand/eye-marker colors either way.
        self.view_canvas = tk.Canvas(view_content, bg=VP["bg"], relief="flat", borderwidth=1,
                                      highlightthickness=1, highlightbackground=VP["tank_outline"])
        self.view_canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.view_canvas.bind("<Configure>", lambda e: self.redraw_view())

        controls_frame = ttk.Frame(self.parent)
        controls_frame.pack(fill=tk.X, pady=5)

        # A plain tk.Button with directly-set colors, not a ttk "Toolbutton"
        # checkbutton -- ttkbootstrap's Toolbutton/Outline styles turned out to
        # render inconsistently in testing (sometimes no visible chrome at all,
        # in either checked or unchecked state), while plain Tk color options are
        # always reliable. This mirrors the same technique already used for the
        # rotate-view arrows in the Settings Preview.
        self.rotate_toggle_btn = tk.Button(controls_frame, text="3D", command=self._toggle_3d,
                                            relief="flat", bd=1, padx=10, pady=3, cursor="hand2")
        self.rotate_toggle_btn.pack(side=tk.LEFT, padx=(0, 10))
        self._update_3d_button()
        self.play_button = ttk.Button(controls_frame, text="▶ Play", width=8, command=self.toggle_play, style="success.TButton")
        self.play_button.pack(side=tk.LEFT, padx=(0, 5))
        ttk.Label(controls_frame, text="Speed:").pack(side=tk.LEFT)
        ttk.Entry(controls_frame, textvariable=self.play_speed_var, width=5).pack(side=tk.LEFT, padx=(0, 15))

        self.timeline_slider = ttk.Scale(controls_frame, from_=0, to=100, length=400, orient=tk.HORIZONTAL, variable=self.timeline_var, command=lambda s: self.redraw_view())
        self.timeline_slider.pack(side=tk.LEFT, padx=(0, 15))

        ttk.Label(controls_frame, text="Line:").pack(side=tk.LEFT)
        self.line_entry = ttk.Entry(controls_frame, textvariable=self.line_var, width=8)
        self.line_entry.pack(side=tk.LEFT)
        self.line_entry.bind("<Return>", self.jump_to_line)
        self.line_entry.bind("<FocusOut>", self.jump_to_line)
        ttk.Label(controls_frame, textvariable=self.line_total_var).pack(side=tk.LEFT, padx=(0, 15))

        ttk.Label(controls_frame, text="|").pack(side=tk.LEFT, padx=5)

        ttk.Label(controls_frame, text="Cycle:").pack(side=tk.LEFT)
        self.cycle_entry = ttk.Entry(controls_frame, textvariable=self.cycle_var, width=6)
        self.cycle_entry.pack(side=tk.LEFT)
        self.cycle_entry.bind("<Return>", self.jump_to_cycle)
        self.cycle_entry.bind("<FocusOut>", self.jump_to_cycle)
        ttk.Label(controls_frame, textvariable=self.cycle_total_var).pack(side=tk.LEFT, padx=(0, 15))

        # A monospace font keeps every digit (and the spaces used to pad shorter
        # numbers) the same width, so the readout doesn't visibly jitter left/right
        # as the coordinates change length while scrubbing/playing. TkFixedFont is
        # Tk's built-in named fixed-width font -- unlike "Consolas" (Windows-only),
        # it resolves to a real monospace font on any platform, Linux included.
        mono_font = ("TkFixedFont", 10)
        ttk.Label(controls_frame, textvariable=self.coord_var, font=mono_font).pack(side=tk.LEFT)
        # Anchored to the right edge of the G-Code Preview pane (not packed after
        # the coordinates) so it stays visible and fixed in place regardless of how
        # wide the rest of the row gets.
        ttk.Label(controls_frame, textvariable=self.time_at_pos_var, font=mono_font).pack(side=tk.RIGHT, padx=(0, 5))

    def reapply_viewport_colors(self):
        # ttkbootstrap retroactively recolors every plain tk.Canvas (not just ttk
        # widgets) whenever the app's light/dark theme changes, which would
        # otherwise overwrite this viewport's intentionally fixed dark palette.
        self.view_canvas.configure(bg=VP["bg"], highlightbackground=VP["tank_outline"])
        self._update_3d_button()

    def _toggle_3d(self):
        self.auto_rotate_var.set(not self.auto_rotate_var.get())
        self._update_3d_button()
        self.redraw_view()

    def _update_3d_button(self):
        colors = self.app.root.style.colors
        on = self.auto_rotate_var.get()
        bg = colors.success if on else colors.light
        fg = "white" if on else colors.fg
        self.rotate_toggle_btn.configure(bg=bg, fg=fg, activebackground=bg, activeforeground=fg)

    def open_gcode_view(self, filepath=None):
        if not filepath: filepath = filedialog.askopenfilename(filetypes=[("G-Code Files", "*.gcode")])
        if not filepath: return
        self.stop_play()
        self.gcode_path_var.set(filepath)
        self.parse_gcode(filepath)
        self._sync_settings_to_app_params()
        self.redraw_view()
        if hasattr(self.app, "set_status"): self.app.set_status(f"Loaded: {filepath}")

    def toggle_play(self):
        self.stop_play() if self.is_playing else self.start_play()

    def start_play(self):
        if not self.gcode_commands or self.timeline_var.get() >= len(self.gcode_commands) - 1:
            self.timeline_var.set(0)
        self.is_playing = True
        self.play_button.config(text="⏸ Pause")
        self._play_step()

    def stop_play(self):
        self.is_playing = False
        if self.play_button: self.play_button.config(text="▶ Play")
        if self.play_job:
            self.app.root.after_cancel(self.play_job)
            self.play_job = None

    def _play_step(self):
        if not self.is_playing: return
        try: speed = max(1, int(self.play_speed_var.get()))
        except (tk.TclError, ValueError): speed = 1
        idx = min(self.timeline_var.get() + speed, len(self.gcode_commands) - 1)
        self.timeline_var.set(idx)
        self.redraw_view()
        if idx >= len(self.gcode_commands) - 1:
            self.stop_play()
            return
        self.play_job = self.app.root.after(100, self._play_step)

    def parse_gcode(self, filepath):
        self.gcode_commands, self.view_settings = [], {}
        self._path_static, self._path_substeps, self._cache_key = [], [], None
        self.cmd_cycles, self.cycle_starts = [], {}
        self.cmd_times = []
        self.cmd_layups, self.layup_count = [], 1
        try:
            with open(filepath, 'r') as f:
                cx, cy, ca = 0.0, 0.0, 0.0
                a_offset = 0.0
                cycle_num = 1
                layup = 0
                feed = 0.0  # G-code F persists across lines that don't repeat it
                elapsed = 0.0
                prev_x, prev_y, prev_a = None, None, None
                for line in f:
                    line = line.strip()
                    if line.startswith(";"):
                        if line.startswith("; CYCLE_COMPLETE:"):
                            try: cycle_num = int(line.split(":", 1)[1].strip()) + 1
                            except ValueError: pass
                        elif line.startswith("; LAYUP_START:"):
                            try: layup = max(0, int(line.split(":", 1)[1].strip()) - 1)
                            except ValueError: pass
                        elif ":" in line:
                            p = line[1:].split(":", 1)
                            self.view_settings[p[0].strip()] = p[1].strip()
                        continue
                    if line.startswith("G92"):
                        # Redefines the current position without moving: subsequent A
                        # values are relative to this new reference. Track the offset
                        # so the true, physically-continuous rotation is reconstructed
                        # for visualization instead of restarting near zero each reset.
                        reset_to = None
                        for p in line.split():
                            if p.startswith("A"):
                                try: reset_to = float(p[1:])
                                except ValueError: pass
                        if reset_to is not None: a_offset = ca - reset_to
                        continue
                    if line.startswith("G1") or line.startswith("G0"):
                        ca_raw = ca - a_offset
                        for p in line.split():
                            if p.startswith("X"): cx = float(p[1:])
                            if p.startswith("Y"): cy = float(p[1:])
                            if p.startswith("A"): ca_raw = float(p[1:])
                            if p.startswith("F"):
                                try: feed = float(p[1:])
                                except ValueError: pass
                        ca = ca_raw + a_offset
                        # Elapsed time up to and including this command, from the
                        # combined (X, Y, true-A) move distance and this move's feed
                        # rate -- the same "distance / feed * 60" relationship the
                        # generator itself uses, so this matches the machine's actual
                        # timing rather than re-simulating the whole wind.
                        if prev_x is not None:
                            dist = math.sqrt((cx - prev_x)**2 + (cy - prev_y)**2 + (ca - prev_a)**2)
                            if feed > 0: elapsed += (dist / feed) * 60.0
                        prev_x, prev_y, prev_a = cx, cy, ca
                        # Store both: `ca` (true, physically-continuous angle) drives
                        # the path/front-back rendering math; `ca_raw` is the value
                        # literally written on this line, shown in the readout so it
                        # matches what the machine actually receives at this line.
                        self.gcode_commands.append((cx, cy, ca, ca_raw))
                        self.cmd_times.append(elapsed)
                        if cycle_num not in self.cycle_starts:
                            self.cycle_starts[cycle_num] = len(self.gcode_commands) - 1
                        self.cmd_cycles.append(cycle_num)
                        self.cmd_layups.append(layup)
        except (OSError, UnicodeDecodeError, ValueError) as e:
            messagebox.showerror("Error", f"Couldn't read the G-code file:\n{e}")
            return
        header_layups = winding.layups_from_header(self.view_settings)
        self.total_cycles = sum(l.passes for l in header_layups) if header_layups else cycle_num
        self.layup_count = max(len(header_layups) if header_layups else 1, max(self.cmd_layups, default=0) + 1)
        self.timeline_slider.config(to=max(0, len(self.gcode_commands)-1))
        self.timeline_var.set(0)
        self.line_total_var.set(f"/ {len(self.gcode_commands)}")
        self.cycle_total_var.set(f"/ {self.total_cycles}")

    def _sync_settings_to_app_params(self):
        # A file written by this app always records the tank geometry; one that
        # does is restored completely: any setting it predates (e.g. the
        # turnaround zone) takes the value the file was actually wound with
        # (winding.LEGACY_VALUES, else the default) -- rather than silently
        # keeping whatever is currently entered.
        is_winder_file = all(k in self.view_settings for k in ("tank_length", "tank_diameter", "chuck_offset"))
        defaults = winding.WindingJob()
        for key, var in self.app.params.items():
            if key not in self.view_settings:
                if is_winder_file: var.set(winding.LEGACY_VALUES.get(key, getattr(defaults, key)))
                continue
            raw = self.view_settings[key]
            try:
                if isinstance(var, tk.BooleanVar): var.set(raw.strip().lower() in ("1", "true", "yes", "on"))
                elif isinstance(var, tk.IntVar): var.set(int(float(raw)))
                elif isinstance(var, tk.DoubleVar): var.set(float(raw))
                else: var.set(raw)
            except ValueError: pass
        # Restores every layup, including from files written before multi-layup
        # support (a single pattern stored as top-level settings).
        layups = winding.layups_from_header(self.view_settings)
        if layups: self.app.load_layups(layups)

    def jump_to_line(self, event=None):
        if not self.gcode_commands: return
        try: idx = int(self.line_var.get())
        except (tk.TclError, ValueError): return
        idx = max(0, min(idx, len(self.gcode_commands) - 1))
        self.timeline_var.set(idx)
        self.redraw_view()

    def jump_to_cycle(self, event=None):
        if not self.cycle_starts: return
        try: cyc = int(self.cycle_var.get())
        except (tk.TclError, ValueError): return
        if cyc not in self.cycle_starts:
            cyc = min(self.cycle_starts.keys(), key=lambda k: abs(k - cyc))
        self.timeline_var.set(self.cycle_starts[cyc])
        self.redraw_view()

    # The eye never moves angularly -- physically, it only translates in X and
    # retracts in Y, always contacting the mandrel at the same fixed spot in the
    # room. The mandrel spins underneath it. EYE_REF_ANGLE_DEG is where that fixed
    # contact point sits in this drawing's (cos, sin) convention: 180 degrees is
    # the bottom of the tank outline, matching the eye marker's own fixed on-screen
    # position, so the strand visibly originates right where the eye is drawn
    # instead of at the tank's centerline.
    EYE_REF_ANGLE_DEG = 180.0

    def _build_path_cache(self, co, lt, rt, rc, ld, cap_type, scale, ox, oy):
        # Caches only what never changes once computed: a point's X-projection and
        # local tank radius don't depend on playback position. Its on-screen angle
        # does (see _draw_wound_path) -- the view co-rotates with the mandrel so the
        # most-recently-laid point always sits at the eye, meaning every already-
        # laid point's screen position must be re-derived relative to the CURRENT
        # rotation on every redraw, not fixed at whatever it was when laid down.
        # Each point also carries its layup index, which picks its strand color.
        pts = []
        for (cx, cy, ca, ca_raw), layup in zip(self.gcode_commands, self.cmd_layups):
            r = winding.calc_R(cx - co, 0, lt, rt, rc, ld, cap_type)
            pts.append((ox - cx * scale, r, ca, layup))
        self._path_static = pts

        # Each stored point is one real G-code command, which -- at a steep wind
        # angle -- can be a single move that legitimately commands close to (or,
        # right at the computed max angle, exactly) a full revolution over just
        # one 5mm step of X, since the machine only needs its two endpoints and
        # linearly interpolates X and A together within the move to reproduce the
        # helix exactly. But that means two adjacent *stored* points can be far
        # apart in angle, and drawing a straight chord directly between them (or
        # testing front/back visibility only at those two points) skips right over
        # what that single real move actually swept through, aliasing the drawn
        # path into a shallow, wrong-looking shape instead of a tight helix. This
        # inserts synthetic in-between points along the same straight-line (X, A)
        # interpolation the machine itself performs for that move, purely so the
        # already-correct G-code gets drawn correctly -- it changes nothing about
        # gcode_commands, the timeline/scrubbing indices, or any other data.
        raw_cx = [cmd[0] for cmd in self.gcode_commands]
        raw_ca = [cmd[2] for cmd in self.gcode_commands]
        subs = []
        cap_deg = winding.RENDER_MAX_DEG_PER_STEP
        for i in range(len(self.gcode_commands) - 1):
            dx, da = raw_cx[i + 1] - raw_cx[i], raw_ca[i + 1] - raw_ca[i]
            n_sub = min(60, math.ceil(abs(da) / cap_deg)) if abs(da) > cap_deg else 1
            layup = self.cmd_layups[i + 1]  # the move from i to i+1 belongs to its target's layup
            seg = []
            for k in range(1, n_sub):
                frac = k / n_sub
                scx = raw_cx[i] + dx * frac
                sca = raw_ca[i] + da * frac
                sr = winding.calc_R(scx - co, 0, lt, rt, rc, ld, cap_type)
                seg.append((ox - scx * scale, sr, sca, layup))
            subs.append(seg)
        self._path_substeps = subs

    def _draw_wound_path(self, oy, scale, ca_now, idx, auto_rotate):
        # Auto-rotate ON: a material point laid down when the mandrel's true
        # rotation was `ca_recorded` is fixed to the mandrel surface, so as the
        # mandrel keeps turning to its current angle `ca_now`, that point is
        # carried along by exactly the same amount: its on-screen (vertical)
        # position uses angle EYE_REF_ANGLE_DEG + (ca_now - ca_recorded). At
        # ca_now == ca_recorded (the point currently being laid) this reduces to
        # EYE_REF_ANGLE_DEG, i.e. it always sits right at the eye; older points
        # recede from there as winding continues.
        #
        # Visibility uses that SAME angle's sin() (the true geometric front/back
        # split for a cylinder viewed edge-on) so material properly cycles through
        # both the near/visible and far/hidden side, sweeping across the full top
        # and bottom of the tank as it ages -- an earlier attempt used a visibility
        # rule tied to "how recently laid" instead, but that rule's sign always
        # matched the vertical-position formula's sign exactly, which meant only
        # ever the bottom half (nearer the eye) could show and the top half stayed
        # permanently empty.
        #
        # The one wrinkle: EYE_REF_ANGLE_DEG sits exactly on that front/back
        # boundary (sin=0 there), since the eye is drawn at the vertical extreme of
        # the circle -- so the instant a point is laid it's precisely on the edge,
        # and the very next degree of rotation would otherwise flip it to hidden.
        # VIS_TOLERANCE softens that one boundary crossing slightly so the freshly
        # laid tip stays visible for a short stretch right at the eye instead of
        # vanishing immediately, without changing the true occlusion for anything
        # already well clear of that edge.
        #
        # Auto-rotate OFF: the original plain 2D side view -- every point rendered
        # at its own recorded angle directly, camera fixed in the world frame, tank
        # never appears to spin.
        #
        # Each layup is drawn in its own identity color (theme.layup_style), the
        # G-code viewport counterpart of the Settings Preview's layup colors.
        VIS_TOLERANCE = math.sin(math.radians(20))
        poly = []
        poly_layup = None  # the layup whose color `poly` is drawn in

        def flush():
            if len(poly) >= 2:
                color, dash = theme.layup_style(VP, poly_layup)
                self.view_canvas.create_line(*[c for p in poly for c in p], fill=color, dash=dash or "", width=1)
            poly.clear()

        def emit(px, r, ca_recorded, layup):
            nonlocal poly_layup
            if layup != poly_layup:
                # Layup boundary: finish the previous layup's line in its own
                # color, then carry on from its last point in the new color so
                # the path stays visually continuous across the change.
                last = poly[-1] if poly else None
                flush()
                if last is not None: poly.append(last)
                poly_layup = layup
            if auto_rotate:
                # Subtracting (not adding) the elapsed rotation matches the tank's
                # actual spin direction: material that has just left the eye stays
                # on the visible near side for the following half-turn, instead of
                # immediately swinging behind the tank.
                eff = math.radians(self.EYE_REF_ANGLE_DEG - (ca_now - ca_recorded))
                visible = math.sin(eff) >= -VIS_TOLERANCE
            else:
                eff = math.radians(ca_recorded)
                visible = math.sin(eff) >= 0
            py = oy - r * math.cos(eff) * scale
            if visible:
                poly.append((px, py))
            else:
                flush()
        for i in range(idx + 1):
            emit(*self._path_static[i])
            # The substeps between real point i and i+1 retrace what that single
            # G-code move actually swept through -- only relevant while both its
            # endpoints are already part of the wound-so-far path (i < idx).
            if i < idx and i < len(self._path_substeps):
                for spt in self._path_substeps[i]:
                    emit(*spt)
        flush()

    def _draw_rotation_grid(self, co, lt, rt, rc, ld, cap_type, scale, ox, oy, ca):
        # A cylinder's outer silhouette never visually changes as it spins around
        # its own axis, so a handful of fixed reference stripes (like lines of
        # longitude painted on the mandrel) are drawn instead -- their front-facing
        # portions sweep across the tank surface as `ca` (the true, continuous
        # rotation angle at the current point in the G-code) advances, giving the
        # simplified top-down view a sense of the tank actually spinning while it
        # winds. A mandrel-fixed marking at angle `stripe_angle` is, in world/screen
        # terms, at `stripe_angle - ca` -- the same relationship (current rotation
        # SUBTRACTS from a mandrel-fixed reference, matching the tank's actual spin
        # direction) used for the wound path itself, so the grid and the strand
        # rotate together, consistently, in the same direction.
        n_stripes, n_samples = 8, 16
        for k in range(n_stripes):
            stripe_angle = math.radians(k * (360.0 / n_stripes))
            poly = []
            for i in range(n_samples + 1):
                x = i * (lt / n_samples)
                r = winding.calc_R(x, 0, lt, rt, rc, ld, cap_type)
                eff = stripe_angle - math.radians(ca)
                px, py = ox - (co + x) * scale, oy - r * math.cos(eff) * scale
                if math.sin(eff) >= 0:
                    poly.append((px, py))
                else:
                    if len(poly) >= 2:
                        self.view_canvas.create_line(*[c for p in poly for c in p], fill=VP["grid"], width=1)
                    poly = []
            if len(poly) >= 2:
                self.view_canvas.create_line(*[c for p in poly for c in p], fill=VP["grid"], width=1)

    def redraw_view(self):
        self.view_canvas.delete("all")
        if not self.gcode_commands: return
        idx = min(self.timeline_var.get(), len(self.gcode_commands)-1)
        cmd = self.gcode_commands[idx]
        self.line_var.set(idx)
        self.cycle_var.set(self.cmd_cycles[idx] if idx < len(self.cmd_cycles) else 0)
        if self.layup_count > 1 and idx < len(self.cmd_layups):
            self.layup_var.set(f"Layup {self.cmd_layups[idx] + 1} / {self.layup_count}")
        else:
            self.layup_var.set("")
        # Fixed-width, integer X/Y (only A keeps decimals) so the readout doesn't
        # change length -- and therefore doesn't visibly shift -- as the numbers
        # change while scrubbing/playing.
        self.coord_var.set(f"X:{cmd[0]:4.0f}  Y:{cmd[1]:3.0f}  A:{cmd[3]:9.2f}")
        elapsed = self.cmd_times[idx] if idx < len(self.cmd_times) else 0.0
        h, m = divmod(int(elapsed), 3600); m, s = divmod(m, 60)
        self.time_at_pos_var.set(f"{h:02d}:{m:02d}:{s:02d}")
        c_w, c_h = self.view_canvas.winfo_width(), self.view_canvas.winfo_height()
        if c_w < 100 or c_h < 100: return
        try:
            lt, dt, co, dc = [float(self.view_settings.get(k, v)) for k, v in zip(["tank_length", "tank_diameter", "chuck_offset", "end_cap_diameter"], [1000, 300, 50, 50])]
            cap_type, ld, arm = self.view_settings.get("end_cap_type", "Round"), float(self.view_settings.get("ld", 0)), float(self.view_settings.get("eye_arm_length", 200))
            eye_width = float(self.view_settings.get("eye_width", 20))
            # Center the view on the tank's X-center (chuck_offset + tank_length/2)
            # instead of right-anchoring, so short tanks aren't left off-center.
            half_extent = max(co + lt / 2.0, lt / 2.0 + 75)
            rt, rc, scale = dt/2, dc/2, min((c_w / 2.0 - 40) / half_extent, (c_h - 100) / (dt + 200))
            ox, oy = c_w / 2.0 + (co + lt / 2.0) * scale, c_h / 2
            tank_top, tank_bottom = [], []
            for i in range(51):
                x = i * (lt / 50); r = winding.calc_R(x, 0, lt, rt, rc, ld, cap_type)
                tank_top.append((ox - (co + x) * scale, oy - r * scale))
                tank_bottom.append((ox - (co + x) * scale, oy + r * scale))
            self.view_canvas.create_polygon(tank_top + tank_bottom[::-1], fill=VP["tank"], outline=VP["tank_outline"])
            auto_rotate = self.auto_rotate_var.get()
            if auto_rotate:
                self._draw_rotation_grid(co, lt, rt, rc, ld, cap_type, scale, ox, oy, cmd[2])

            cache_key = (len(self.gcode_commands), co, lt, rt, rc, ld, cap_type, scale, ox, oy)
            if cache_key != self._cache_key:
                self._build_path_cache(co, lt, rt, rc, ld, cap_type, scale, ox, oy)
                self._cache_key = cache_key
            self._draw_wound_path(oy, scale, cmd[2], idx, auto_rotate)

            tool_x, eye_y = ox - cmd[0] * scale, oy + (winding.Y_REFERENCE - arm - cmd[1]) * scale
            self.view_canvas.create_line(tool_x, oy + (winding.Y_REFERENCE - arm) * scale, tool_x, eye_y, width=3, fill=VP["eye_line"])
            # Eye footprint: drawn to scale so its physical width along X (the
            # dimension the safety clearance now accounts for) is visible, especially
            # near the tank ends where a zero-width marker would be misleading.
            half_w = max(2.0, (eye_width / 2.0) * scale)
            self.view_canvas.create_rectangle(tool_x - half_w, eye_y - 5, tool_x + half_w, eye_y + 5, fill=VP["eye_marker"], outline=VP["bg"])
        except: pass
