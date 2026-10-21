import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import bisect
import math
import os
from typing import NamedTuple
import strength
import theme
import winding
from ttkbootstrap.widgets.tooltip import ToolTip
from theme import VIEWPORT_PALETTE as VP
from progress_dialog import ProgressTask

# Which layup a command belongs to before the first "; LAYUP_START" marker:
# homing and the move to the wind start are setup, not fiber.
SETUP = -1
# Files at least this big are read on a worker thread behind a progress window
# (around a second's worth of reading); smaller ones open on the spot.
BACKGROUND_LOAD_BYTES = 1_000_000


class GcodeData(NamedTuple):
    """A G-code file as the viewer shows it (see read_gcode)."""
    commands: list      # (x, y, true continuous A, A as written) per move
    times: list         # elapsed seconds at the end of each move
    cycles: list        # cycle number (across the program) per move
    cycle_starts: dict  # cycle number -> index of its first move
    layups: list        # layup index (or SETUP) per move
    settings: dict      # the settings header, {key: raw string}
    resume: object      # how a partial program continues (winding.Resume), or None
    total_cycles: int
    layup_count: int
    geometry: tuple     # (chuck offset, tank length, tank radius, cap radius, dome length, cap type)
    path: dict          # layup -> thinned strand path, for drawing (see _drawing_path)
    solids: dict        # layup -> how to draw it once finished, if it covers the tank


def _first_cycle_number(settings):
    # The number of the file's first cycle, counted across the whole program:
    # 1, except in a partial program, which continues mid-program.
    layups = winding.layups_from_header(settings)
    resume = winding.resume_from_header(settings)
    if resume is None or not layups:
        return 1
    return sum(l.passes for l in layups[:resume.point.layup]) + resume.point.cycle + 1


def read_gcode(filepath, on_progress=None):
    """Reads a G-code file for the viewer: pure, without Tk, so a large file can
    be read on a worker thread. `on_progress`, if given, gets the share of the
    file read so far (0-1) every few thousand lines. Raises OSError,
    UnicodeDecodeError or ValueError for a file it can't read."""
    commands, times, cycles, cycle_starts, cmd_layups, settings = [], [], [], {}, [], {}
    size, read = max(1, os.path.getsize(filepath)), 0
    with open(filepath, 'r') as f:
        cx, cy, ca = 0.0, 0.0, 0.0
        a_offset = 0.0
        cycle_num = None  # known once the settings header has been read
        layup, saw_layup_marker = SETUP, False
        feed = 0.0  # G-code F persists across lines that don't repeat it
        elapsed = 0.0
        prev_x, prev_y, prev_a = None, None, None
        for n_line, line in enumerate(f, 1):
            read += len(line)
            if on_progress is not None and n_line % 5000 == 0:
                on_progress(0.85 * min(1.0, read / size))
            line = line.strip()
            if line.startswith(";"):
                if line.startswith("; CYCLE_COMPLETE:"):
                    try: cycle_num = int(line.split(":", 1)[1].strip()) + 1
                    except ValueError: pass
                elif line.startswith("; LAYUP_START:"):
                    try: layup, saw_layup_marker = max(0, int(line.split(":", 1)[1].strip()) - 1), True
                    except ValueError: pass
                elif ":" in line:
                    p = line[1:].split(":", 1)
                    settings[p[0].strip()] = p[1].strip()
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
                commands.append((cx, cy, ca, ca_raw))
                times.append(elapsed)
                if cycle_num is None:
                    cycle_num = _first_cycle_number(settings)
                if cycle_num not in cycle_starts:
                    cycle_starts[cycle_num] = len(commands) - 1
                cycles.append(cycle_num)
                cmd_layups.append(layup)
    if not saw_layup_marker:
        # Written before layups existed: every move is part of the wind.
        cmd_layups = [0] * len(cmd_layups)
    header_layups = winding.layups_from_header(settings)
    total_cycles = sum(l.passes for l in header_layups) if header_layups else (cycle_num or 1)
    layup_count = max(len(header_layups) if header_layups else 1, max(cmd_layups, default=0) + 1)
    geometry = _view_geometry(settings)
    path, solids = _drawing_path(commands, cmd_layups, geometry, winding.job_from_header(settings),
                                 None if on_progress is None else (lambda f: on_progress(0.85 + 0.15 * f)))
    if on_progress is not None:
        on_progress(1.0)
    return GcodeData(commands, times, cycles, cycle_starts, cmd_layups, settings,
                     winding.resume_from_header(settings), total_cycles, layup_count, geometry, path, solids)


def _view_geometry(settings):
    # (chuck offset, tank length, tank radius, end cap radius, dome length,
    # end cap type) from a settings header, for drawing.
    get = lambda key, default: float(settings.get(key, default))
    return (get("chuck_offset", 50), get("tank_length", 1000), get("tank_diameter", 300) / 2,
            get("end_cap_diameter", 50) / 2, get("ld", 0), settings.get("end_cap_type", "Round"))


def _drawing_path(commands, cmd_layups, geometry, job, on_progress=None):
    """What the G-Code Preview draws, worked out once per file: per layup, the
    thinned strand path ((keys, [(x, r, A in radians)])) -- a key being the
    command a point belongs to, fractional for points retracing what one long
    move swept through -- and, for the layups that cover the tank once
    finished, how to draw them solid: ("tank",) or, for a 90° wind,
    ("band", x_lo, x_hi, x where its pass begins). `job` is the file's own
    (job_from_header), None if it can't be told."""
    co, lt, rt, rc, ld, cap_type = geometry
    lo, hi = co, co + lt
    known = {}

    def on_tank(x):
        # (x, r) where the fiber lies with the eye at x: beyond the tank's ends
        # (where a layup's Additional X-Axis Travel takes the eye) it stays at
        # the end, around the polar opening.
        p = known.get(x)
        if p is None:
            tx = lo if x < lo else hi if x > hi else x
            p = known[x] = (tx, winding.calc_R(tx - co, 0, lt, rt, rc, ld, cap_type))
        return p

    solids, hoop_inside = {}, {}
    if job is not None and len(job.layups) >= max(cmd_layups, default=-1) + 1:
        sides = winding.start_sides(job)
        first, last = job.hoop_span
        half = job.bandwidth / 2
        for li, layup in enumerate(job.layups):
            if layup.hoop:
                solids[li] = ("band", first - half, last + half, first - half if sides[li] == 1 else last + half)
                hoop_inside[li] = (first + 1e-6, last - 1e-6)  # drawn as the band, not as strands
            elif winding.plan_layup(job, layup).coverage >= 0.995:
                solids[li] = ("tank",)
    # Dense path: every command, plus in-between points along any move that
    # turns the mandrel by more than RENDER_MAX_DEG_PER_STEP -- drawn as a
    # straight chord, a near-full turn in one 5 mm step would be aliased into a
    # shallow wrong-looking line. The move from i to i+1 belongs to i+1's layup.
    dense = {}
    cap_deg = winding.RENDER_MAX_DEG_PER_STEP
    n = len(commands)
    for i in range(n):
        layup = cmd_layups[i]
        if layup == SETUP:
            continue
        pts = dense.setdefault(layup, [])
        x, a = commands[i][0], commands[i][2]
        if i > 0:
            px, pa = commands[i - 1][0], commands[i - 1][2]
            if not pts:
                pts.append((*on_tank(px), pa, i - 1))  # where the previous layup / the setup left off
            da = a - pa
            if abs(da) > cap_deg:
                n_sub = min(60, math.ceil(abs(da) / cap_deg))
                for k in range(1, n_sub):
                    f = k / n_sub
                    pts.append((*on_tank(px + (x - px) * f), pa + da * f, i - 1 + f))
        pts.append((*on_tank(x), a, i))
        if on_progress is not None and i % 50000 == 0:
            on_progress(0.8 * i / max(1, n))
    path = {}
    for li, pts in dense.items():
        inside = hoop_inside.get(li)
        if inside is not None:
            pts = [p for p in pts if not inside[0] < p[0] < inside[1]]
        thin = winding.thin_path(pts)
        path[li] = ([p[3] for p in thin], [(p[0], p[1], math.radians(p[2])) for p in thin])
    return path, solids


class ViewTab:
    def __init__(self, parent, app):
        self.parent = parent
        self.app = app
        self.view_canvas = None
        self.gcode_commands = []
        self.view_settings = {}
        self.data = None  # the file shown, as read_gcode() read it
        self.cmd_cycles = []
        self.cycle_starts = {}
        self.total_cycles = 0
        # Which layup (0-based) each command belongs to, from the file's
        # "; LAYUP_START:n" markers; files written before multi-layup support
        # have none, and are treated as one single layup.
        self.cmd_layups = []
        self.layup_count = 1
        self.layup_var = tk.StringVar(value="")
        # How a partial program continues (winding.Resume), from its header.
        self.resume = None
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
        speed_label = ttk.Label(controls_frame, text="Speed:")
        speed_label.pack(side=tk.LEFT)
        speed_entry = ttk.Entry(controls_frame, textvariable=self.play_speed_var, width=5)
        speed_entry.pack(side=tk.LEFT, padx=(0, 15))

        self.timeline_slider = ttk.Scale(controls_frame, from_=0, to=100, length=400, orient=tk.HORIZONTAL, variable=self.timeline_var, command=lambda s: self.redraw_view())
        self.timeline_slider.pack(side=tk.LEFT, padx=(0, 15))

        line_label = ttk.Label(controls_frame, text="Line:")
        line_label.pack(side=tk.LEFT)
        self.line_entry = ttk.Entry(controls_frame, textvariable=self.line_var, width=8)
        self.line_entry.pack(side=tk.LEFT)
        self.line_entry.bind("<Return>", self.jump_to_line)
        self.line_entry.bind("<FocusOut>", self.jump_to_line)
        ttk.Label(controls_frame, textvariable=self.line_total_var).pack(side=tk.LEFT, padx=(0, 15))

        ttk.Label(controls_frame, text="|").pack(side=tk.LEFT, padx=5)

        cycle_label = ttk.Label(controls_frame, text="Cycle:")
        cycle_label.pack(side=tk.LEFT)
        self.cycle_entry = ttk.Entry(controls_frame, textvariable=self.cycle_var, width=6)
        self.cycle_entry.pack(side=tk.LEFT)
        self.cycle_entry.bind("<Return>", self.jump_to_cycle)
        self.cycle_entry.bind("<FocusOut>", self.jump_to_cycle)
        ttk.Label(controls_frame, textvariable=self.cycle_total_var).pack(side=tk.LEFT, padx=(0, 15))
        # Hover explanations, as on the settings page.
        for widgets, tip in (
                ([self.rotate_toggle_btn], "Switches between the flat side view and a 3D view that spins with the "
                                           "mandrel."),
                ([self.play_button], "Plays the program from the current position (from the start once it's at "
                                     "the end)."),
                ([speed_label, speed_entry], "Playback speed: how many G-code moves each frame advances (10 frames "
                                             "a second)."),
                ([self.timeline_slider], "Drag to scrub through the program."),
                ([line_label, self.line_entry], "Jumps to a G-code move: its number, counted from 0 over the moves only (not "
                                    "the file's line number). Press Enter."),
                ([cycle_label, self.cycle_entry], "Jumps to the start of a cycle, numbered across the whole program as in the "
                                     "file's CYCLE_COMPLETE markers. Press Enter.")):
            for widget in widgets:
                ToolTip(widget, text=tip, wraplength=300, delay=400)

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
        self.view_canvas.configure(bg=VP["bg"], highlightbackground=VP["tank_outline"], highlightthickness=1)
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
        fail = lambda e: messagebox.showerror("Error", f"Couldn't read the G-code file:\n{e}")
        try:
            big = os.path.getsize(filepath) >= BACKGROUND_LOAD_BYTES
        except OSError as e:
            fail(e)
            return
        if big:
            # Reading a long program takes a while: on a worker thread, with
            # the progress shown (see progress_dialog).
            ProgressTask(self.app, "Opening G-Code",
                         lambda progress: read_gcode(filepath, lambda f: progress(f, "Reading the file…")),
                         on_done=lambda data: self.show_file(filepath, data), on_error=fail,
                         finishing="Drawing the preview…")
            return
        try:
            data = read_gcode(filepath)
        except (OSError, UnicodeDecodeError, ValueError) as e:
            fail(e)
            return
        self.show_file(filepath, data)

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
        """Reads a G-code file into the viewer, on the spot. Returns whether it
        could be read."""
        try:
            data = read_gcode(filepath)
        except (OSError, UnicodeDecodeError, ValueError) as e:
            messagebox.showerror("Error", f"Couldn't read the G-code file:\n{e}")
            return False
        self._load(data)
        return True

    def show_file(self, filepath, data):
        """Shows a file read by read_gcode() and restores the settings it was
        made with."""
        self.stop_play()
        self.gcode_path_var.set(filepath)
        self._load(data)
        self._sync_settings_to_app_params()
        self.redraw_view()
        if hasattr(self.app, "set_status"): self.app.set_status(f"Loaded: {filepath}")

    def _load(self, data):
        self.gcode_commands, self.view_settings = data.commands, data.settings
        self.data = data
        self.cmd_cycles, self.cycle_starts, self.cmd_times = data.cycles, data.cycle_starts, data.times
        self.cmd_layups, self.layup_count = data.layups, data.layup_count
        self.resume, self.total_cycles = data.resume, data.total_cycles
        self.timeline_slider.config(to=max(0, len(self.gcode_commands)-1))
        self.timeline_var.set(0)
        self.line_total_var.set(f"/ {len(self.gcode_commands)}")
        self.cycle_total_var.set(f"/ {self.total_cycles}")

    def current_point(self):
        """The program point at the timeline position, as a winding.ProgramPoint
        -- for continuing an interrupted wind from where the machine stopped --
        or None without a file."""
        if not self.gcode_commands:
            return None
        idx = min(self.timeline_var.get(), len(self.gcode_commands) - 1)
        layup = self.cmd_layups[idx]
        if layup == self.SETUP:
            # Not winding yet: the file's own starting point.
            return self.resume.point if self.resume else winding.ProgramPoint(0, 0, 0.0)
        layups = winding.layups_from_header(self.view_settings) or []
        cycle = self.cmd_cycles[idx] - 1 - sum(l.passes for l in layups[:layup])
        return winding.ProgramPoint(layup, max(0, cycle), max(0.0, self.gcode_commands[idx][3]))

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
                if is_winder_file:
                    value = winding.LEGACY_VALUES.get(key, getattr(defaults, key))
                    var.set(value(self.view_settings) if callable(value) else value)
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
        # The material estimate the file was made with (files without one keep
        # the current inputs: they're not winding settings).
        material = strength.material_from_header(self.view_settings)
        if material is not None:
            self.app.load_material(material)
        # A partial program also records where it continues from: restore that
        # on the partial-export page, recreating the conditions it was made in.
        if self.resume is not None:
            self.app.load_partial(self.resume)

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

    # cmd_layups value of setup moves (homing, travel to the wind start).
    SETUP = SETUP

    def _draw_layers(self, data, idx, oy, scale, ox, tank_outline, rt, auto_rotate):
        # The fiber wound up to command `idx`, layup by layup in winding order
        # (later ones lie on top): a finished layup that covers the tank is
        # drawn as a solid layer of its color -- every band overlaps the next,
        # so that's what the tank looks like, and how the Settings Preview shows
        # a partial wind -- a 90° wind as the band it is, growing while it's
        # wound; anything else as its strands. Only the layup being wound (or
        # one with gaps) is drawn strand by strand, from the thinned path
        # read_gcode() prepared, so a frame costs the same however much has
        # been wound before.
        #
        # Auto-rotate ON: a material point laid down when the mandrel's true
        # rotation was `a` is fixed to the mandrel surface, so as the mandrel
        # keeps turning to its current angle `a_now`, that point is carried
        # along by exactly the same amount: its on-screen (vertical) position
        # uses angle EYE_REF_ANGLE_DEG - (a_now - a). At a_now == a (the point
        # being laid) that's EYE_REF_ANGLE_DEG, right at the eye; older points
        # recede from there as winding continues. Subtracting (not adding) the
        # elapsed rotation matches the tank's actual spin direction: material
        # that has just left the eye stays on the visible near side for the
        # following half-turn. Visibility uses that same angle's sin() (the
        # front/back split of a cylinder seen edge-on); EYE_REF_ANGLE_DEG sits
        # exactly on that boundary, so VIS_TOLERANCE keeps the freshly laid tip
        # visible for a short stretch at the eye instead of vanishing at once.
        #
        # Auto-rotate OFF: the plain 2D side view -- every point at its own
        # recorded angle, the tank never appears to spin.
        current = self.cmd_layups[idx]
        if current == self.SETUP:
            return
        canvas = self.view_canvas
        cmd = self.gcode_commands[idx]
        a_now = math.radians(cmd[2])
        ref = math.radians(self.EYE_REF_ANGLE_DEG)
        vis_tol = -math.sin(math.radians(20)) if auto_rotate else 0.0
        sin, cos = math.sin, math.cos
        r_now = self._radius_at(data, cmd[0])
        for li in range(current + 1):
            color, dash = theme.layup_style(VP, li)
            solid = data.solids.get(li)
            done = li < current
            if solid is not None and solid[0] == "tank" and done:
                canvas.create_polygon(tank_outline, fill=color, outline=VP["tank_outline"])
                continue
            if solid is not None and solid[0] == "band":
                # A 90° wind: the band from where its pass began, up to the eye.
                _, x_lo, x_hi, x_from = solid
                x_to = (x_hi if x_from == x_lo else x_lo) if done else min(x_hi, max(x_lo, cmd[0]))
                if x_to != x_from:
                    canvas.create_rectangle(ox - x_from * scale, oy - rt * scale, ox - x_to * scale, oy + rt * scale,
                                            fill=color, outline="")
            keys, points = data.path.get(li, ((), ()))
            end = bisect.bisect_right(keys, idx) if not done else len(points)
            line = []

            def flush():
                if len(line) >= 4:
                    canvas.create_line(*line, fill=color, dash=dash or "", width=1)
                line.clear()
            for k in range(end):
                x, r, a = points[k]
                eff = ref - (a_now - a) if auto_rotate else a
                if sin(eff) >= vis_tol:
                    line.append(ox - x * scale)
                    line.append(oy - r * cos(eff) * scale)
                else:
                    flush()
            if not done and line:
                # Right up to the eye, wherever the thinned path left off.
                eff = ref if auto_rotate else a_now
                line.append(ox - cmd[0] * scale)
                line.append(oy - r_now * cos(eff) * scale)
            flush()

    @staticmethod
    def _radius_at(data, x):
        co, lt, rt, rc, ld, cap_type = data.geometry
        return winding.calc_R(x - co, 0, lt, rt, rc, ld, cap_type)

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
        layup = self.cmd_layups[idx] if idx < len(self.cmd_layups) else 0
        if layup == self.SETUP:
            self.layup_var.set("Moving to wind start")
        elif self.layup_count > 1:
            self.layup_var.set(f"Layup {layup + 1} / {self.layup_count}")
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
        if c_w < 100 or c_h < 100 or self.data is None: return
        try:
            co, lt, rt, rc, ld, cap_type = self.data.geometry
            arm = float(self.view_settings.get("eye_arm_length", 200))
            eye_width = float(self.view_settings.get("eye_width", 20))
            # Center the view on the tank's X-center (chuck_offset + tank_length/2)
            # instead of right-anchoring, so short tanks aren't left off-center.
            half_extent = max(co + lt / 2.0, lt / 2.0 + 75)
            scale = min((c_w / 2.0 - 40) / half_extent, (c_h - 100) / (2 * rt + 200))
            ox, oy = c_w / 2.0 + (co + lt / 2.0) * scale, c_h / 2
            tank_top, tank_bottom = [], []
            for i in range(51):
                x = i * (lt / 50); r = winding.calc_R(x, 0, lt, rt, rc, ld, cap_type)
                tank_top.append((ox - (co + x) * scale, oy - r * scale))
                tank_bottom.append((ox - (co + x) * scale, oy + r * scale))
            outline = tank_top + tank_bottom[::-1]
            self.view_canvas.create_polygon(outline, fill=VP["tank"], outline=VP["tank_outline"])
            auto_rotate = self.auto_rotate_var.get()
            self._draw_layers(self.data, idx, oy, scale, ox, outline, rt, auto_rotate)
            if auto_rotate:
                # Over the (solid) layers, so the tank is still seen to spin.
                self._draw_rotation_grid(co, lt, rt, rc, ld, cap_type, scale, ox, oy, cmd[2])

            tool_x, eye_y = ox - cmd[0] * scale, oy + (winding.Y_REFERENCE - arm - cmd[1]) * scale
            self.view_canvas.create_line(tool_x, oy + (winding.Y_REFERENCE - arm) * scale, tool_x, eye_y, width=3, fill=VP["eye_line"])
            # Eye footprint: drawn to scale so its physical width along X (the
            # dimension the safety clearance now accounts for) is visible, especially
            # near the tank ends where a zero-width marker would be misleading.
            half_w = max(2.0, (eye_width / 2.0) * scale)
            self.view_canvas.create_rectangle(tool_x - half_w, eye_y - 5, tool_x + half_w, eye_y + 5, fill=VP["eye_marker"], outline=VP["bg"])
        except: pass
