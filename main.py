import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import math
import datetime
import ttkbootstrap as tb
import theme
from plan_tab import PlanTab
from view_tab import ViewTab

class CFRPWinderApp:
    # --- HARDCODED G-CODE MACROS ---
    # Homing (G28) is controlled by the "Home Axes Before Winding" checkbox, not by
    # this macro. Use START_GCODE for any additional manual startup G-code (e.g. a
    # pause, or turning on an indicator light) -- it always runs after homing/zeroing.
    START_GCODE = ""
    ONE_WIND_COMPLETE_GCODE = ""  # Execute after every L-R-L cycle
    END_GCODE = ""                # Execute at the very end

    # --- HARDCODED MACHINE LIMITS ---
    MAX_X = 5250.0  # carriage's physical X-axis travel limit (mm)

    # --- VISUALIZATION-ONLY RENDERING RESOLUTION ---
    # Caps how many degrees of rotation may separate two consecutive strand-path
    # points recorded for the 3D preview. Purely a rendering knob (see
    # simulate_winding) -- it has no effect on the G-code itself.
    RENDER_MAX_DEG_PER_STEP = 15.0

    def __init__(self, root):
        self.root = root
        self.root.title("CFRP Winder")
        self.root.geometry("1280x720")
        # Open maximized. "zoomed" is the Windows/most-Tk-builds way to do this;
        # fall back to the X11 equivalent attribute if that's not supported.
        try:
            self.root.state("zoomed")
        except tk.TclError:
            try: self.root.attributes("-zoomed", True)
            except tk.TclError: pass

        self.theme_mode = "light"
        style = theme.apply(self.root, self.theme_mode)
        theme.setup_compact_styles(style)
        self.canvas_palette = theme.PALETTES[self.theme_mode]
        self._icon_image = theme.build_icon(style.colors.primary)
        self.root.iconphoto(True, self._icon_image)

        self.params = {
            "chuck_offset": tk.DoubleVar(value=50.0),
            "home_before_wind": tk.BooleanVar(value=True),
            "eye_arm_length": tk.DoubleVar(value=370.0),
            "eye_width": tk.DoubleVar(value=20.0),
            "min_spacing": tk.DoubleVar(value=10.0),
            "max_surface_speed": tk.DoubleVar(value=220),
            "end_cap_type": tk.StringVar(value="Round"),
            "tank_length": tk.DoubleVar(value=1000.0),
            "tank_diameter": tk.DoubleVar(value=200.0),
            "end_cap_diameter": tk.DoubleVar(value=50.0),
            "passes": tk.IntVar(value=10),
            "pattern_number": tk.IntVar(value=3),
            "wind_angle": tk.DoubleVar(value=45.0),
            "bandwidth": tk.DoubleVar(value=5.0),
            "turnaround_angle": tk.DoubleVar(value=270.0),
            "optimize_trajectory": tk.BooleanVar(value=False)
        }
        
        self._build_menu()

        # Status bar first, packed to the bottom, so it claims a slim strip there
        # before the main content below claims everything else.
        status_bar = ttk.Frame(root, padding=(10, 3))
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Separator(root, orient=tk.HORIZONTAL).pack(side=tk.BOTTOM, fill=tk.X)
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(status_bar, textvariable=self.status_var, style="secondary.TLabel").pack(side=tk.LEFT)

        # --- MAIN LAYOUT: settings on the left, split previews on the right ---
        main_frame = ttk.Frame(root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Left settings panel: wrapped in a canvas + scrollbar so all controls stay
        # reachable even when the window is too short to show them all at once.
        left_frame = ttk.Frame(main_frame)
        left_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))

        left_canvas = tk.Canvas(left_frame, borderwidth=0, highlightthickness=0, bg=style.colors.bg)
        left_scrollbar = ttk.Scrollbar(left_frame, orient=tk.VERTICAL, command=left_canvas.yview)
        left_canvas.configure(yscrollcommand=left_scrollbar.set)
        left_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        left_canvas.pack(side=tk.LEFT, fill=tk.Y, expand=True)
        self._left_canvas = left_canvas

        settings_inner = ttk.Frame(left_canvas)
        left_canvas_window = left_canvas.create_window((0, 0), window=settings_inner, anchor="nw")
        settings_inner.bind("<Configure>", lambda e: left_canvas.configure(scrollregion=left_canvas.bbox("all")))
        left_canvas.bind("<Configure>", lambda e: left_canvas.itemconfig(left_canvas_window, width=e.width))

        def _on_mousewheel(event):
            left_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        left_canvas.bind("<Enter>", lambda e: left_canvas.bind_all("<MouseWheel>", _on_mousewheel))
        left_canvas.bind("<Leave>", lambda e: left_canvas.unbind_all("<MouseWheel>"))

        right_paned = ttk.Panedwindow(main_frame, orient=tk.VERTICAL)
        right_paned.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        settings_preview_frame = ttk.LabelFrame(right_paned, text="Settings Preview", padding="10")
        gcode_preview_frame = ttk.LabelFrame(right_paned, text="G-Code Preview", padding="10")
        right_paned.add(settings_preview_frame, weight=1)
        right_paned.add(gcode_preview_frame, weight=1)

        self.plan_tab = PlanTab(settings_inner, settings_preview_frame, self)
        self.view_tab = ViewTab(gcode_preview_frame, self)

        root.update_idletasks()
        left_canvas.configure(width=settings_inner.winfo_reqwidth())

    def _build_menu(self):
        menubar = tk.Menu(self.root, tearoff=False)
        self.root.config(menu=menubar)

        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="Generate G-Code...", command=self.generate)
        file_menu.add_command(label="Open G-Code...", command=lambda: self.view_tab.open_gcode_view())
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.root.destroy)
        menubar.add_cascade(label="File", menu=file_menu)

        self.theme_mode_var = tk.StringVar(value=self.theme_mode)
        view_menu = tk.Menu(menubar, tearoff=False)
        view_menu.add_radiobutton(label="Light Mode", value="light", variable=self.theme_mode_var, command=lambda: self.set_theme_mode("light"))
        view_menu.add_radiobutton(label="Dark Mode", value="dark", variable=self.theme_mode_var, command=lambda: self.set_theme_mode("dark"))
        menubar.add_cascade(label="View", menu=view_menu)

        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

    def _show_about(self):
        messagebox.showinfo("About CFRP Winder",
                             "CFRP Winder\n\n"
                             "Klipper G-code generator and visualizer for composite\n"
                             "filament winding on a 3-axis (X, Y, A) winder.")

    def set_theme_mode(self, mode):
        if mode not in theme.PALETTES or mode == self.theme_mode:
            return
        self.theme_mode = mode
        style = theme.apply(self.root, mode)
        self.canvas_palette = theme.PALETTES[mode]
        if hasattr(self, "_left_canvas"): self._left_canvas.configure(bg=style.colors.bg)
        # Only the Settings Preview's custom-drawn canvases follow the app theme;
        # the G-Code Preview's viewport is intentionally a fixed dark surface in
        # both modes (see theme.py). ttkbootstrap retroactively recolors every
        # plain tk.Canvas on a theme switch though (not just ttk widgets), so the
        # viewport's fixed color has to be explicitly re-applied afterward or it
        # gets swept into the light/dark toggle too.
        if hasattr(self, "plan_tab"): self.plan_tab.schedule_redraw()
        if hasattr(self, "view_tab"): self.view_tab.reapply_viewport_colors()

    def set_status(self, text):
        if hasattr(self, "status_var"): self.status_var.set(text)

    def pattern_is_valid(self, passes, pattern_number):
        return passes >= 1 and pattern_number >= 1

    def x_bounds_ok(self, chuck_offset, tank_length):
        return (chuck_offset + tank_length) <= self.MAX_X

    @staticmethod
    def surface_speed_to_deg_per_min(mm_per_s, diameter):
        # Converts a surface (tangential) speed in mm/s into the equivalent A-axis
        # rotation rate in deg/min for the given tank diameter, so the user enters a
        # physically meaningful, diameter-independent speed instead of a raw rotation
        # rate. All downstream feed-rate/throttling math still works in deg/min.
        if diameter <= 0: return 0.0
        circumference = math.pi * diameter
        return mm_per_s * (360.0 / circumference) * 60.0

    @staticmethod
    def compute_dwell_extras(traverse_rotation, dwell, within_target, between_target):
        # Dwell is a minimum, not a fixed angle: at the turnaround(s) closing out a
        # circuit, extra rotation (on top of 2x the dwell minimum, since dwell applies
        # at both turnarounds) is added so the NEXT circuit's starting angle lands
        # exactly on the target gap, no matter how large the dwell minimum is. Both
        # values are constants (the starting angle cancels out of the modulo), so
        # they only need to be computed once per generation, not per circuit.
        base = traverse_rotation + 2 * dwell
        extra_within = (within_target - base) % 360.0
        extra_between = (between_target - base) % 360.0
        return extra_within, extra_between

    @staticmethod
    def compute_turnaround_balance_offset(total_circuits, n_strands, dwell, extra_within, extra_between):
        # The outbound (right) turnaround's dwell must stay a TRUE CONSTANT across
        # every circuit -- not just small on average -- or the return traversal's
        # pattern breaks. Here's why: the forward-traversal start angles already
        # form a correct arithmetic progression (stepping by 360/n_strands within a
        # cycle, by the bandwidth shift between cycles); the forward-END angles are
        # that same progression plus one fixed, circuit-independent traversal
        # amount, so they're still in the same correct progression. Adding the SAME
        # constant to every forward-end angle (a fixed right-turnaround dwell)
        # keeps the return-traversal start angles in that identical progression too
        # -- just phase-shifted. But if the right dwell instead varies by circuit
        # (e.g. splitting each circuit's own `extra` between both turnarounds),
        # that varying amount gets added unevenly across circuits, and the return
        # angles fall out of step -- the return-side pattern visibly misaligns even
        # though the forward-side pattern (which only depends on each circuit's
        # TOTAL round-trip rotation, checked once per circuit at its very end)
        # still looks fine. So instead of touching the per-circuit split at all,
        # every circuit's right turnaround gets the exact same extra offset K, and
        # the left turnaround absorbs (extra_i - K) as it always did with
        # (extra_i - 0) -- the round-trip total, and therefore the pattern, is
        # completely unaffected by K's value. Choosing K as half the average
        # extra rotation over the whole generation makes the running total spent
        # on the right and left turnarounds come out exactly equal, instead of
        # (as before this existed) dumping essentially all of it on the left.
        if total_circuits <= 0:
            return 0.0
        n_cycles = max(1, total_circuits // n_strands)
        n_between = max(0, n_cycles - 1)
        n_within = max(0, (total_circuits - 1) - n_between)
        sum_extra = n_between * extra_between + n_within * extra_within
        k_ideal = sum_extra / (2.0 * total_circuits)
        # Clamped to [0, dwell] so neither turnaround's dwell (dwell + K, or
        # dwell + extra_i - K) can ever go negative -- this only bites for an
        # unusually small dwell setting, in which case the balance achieved is
        # merely the best available rather than perfectly even.
        return max(0.0, min(k_ideal, dwell))

    @staticmethod
    def compute_dwell_blend(dwell_amount, optimize, n_steps=3, max_fraction=0.3, max_deg=20.0):
        # "Optimize Trajectory": at a turnaround, the machine currently goes from
        # "X moving, A rotating a little (the helix)" straight into "X frozen, A
        # rotating a lot (the dwell)" in one abrupt move. Klipper's look-ahead
        # planner treats consecutive moves as one combined (X,Y,A) vector, and a
        # move that stops X dead while A keeps going is a very sharp corner in
        # that vector space -- its cross-corner velocity limiter forces the speed
        # at that junction down near zero, which is the "stops for a bit" the
        # dwell is felt as.
        #
        # The fix borrows a standard G-code post-processing trick (corner easing):
        # instead of one sharp corner, spend a small piece of the SAME total dwell
        # rotation gradually, blended into the last few traversal steps before the
        # dwell (ramping up) and the first few traversal steps after it (ramping
        # down), while the dwell move itself only has to cover what's left. Each
        # individual junction's direction change is then smaller, so the planner
        # doesn't need to slow nearly as much at any single one of them.
        #
        # Conservation is exact: sum(tail) + sum(head) + core == dwell_amount, so
        # the cumulative A angle at any shared checkpoint (e.g. the start of the
        # next circuit) comes out bit-for-bit identical to the unoptimized case --
        # only the local motion profile right around each turnaround changes, not
        # the winding pattern itself.
        #
        # Returns (head_blend, tail_blend, core_dwell):
        #   tail_blend: length-n_steps list, ramping UP, to add on top of the
        #               normal helix rotation for the LAST n_steps of the
        #               traversal leading INTO this dwell.
        #   head_blend: length-n_steps list, ramping DOWN, to add on top of the
        #               normal helix rotation for the FIRST n_steps of the
        #               traversal leading OUT of this dwell.
        #   core_dwell: the remaining rotation for the dwell move itself.
        if not optimize or dwell_amount <= 0:
            return [], [], dwell_amount
        blend_each_side = min(max_deg, dwell_amount * max_fraction)
        weights = list(range(1, n_steps + 1))
        wsum = float(sum(weights))
        tail_blend = [blend_each_side * w / wsum for w in weights]
        head_blend = [blend_each_side * w / wsum for w in reversed(weights)]
        core_dwell = dwell_amount - sum(tail_blend) - sum(head_blend)
        return head_blend, tail_blend, core_dwell

    @staticmethod
    def traversal_steps(x_start, x_end, step_size, pitch, head_blend, tail_blend):
        # Yields (x_next, a_delta) for one X traversal from x_start to x_end (the
        # sign of x_end - x_start picks the direction), stepping by step_size (the
        # last step may be shorter, to land exactly on x_end). a_delta is that
        # step's rotation in degrees: the plain helix amount, plus any blended-in
        # dwell rotation from head_blend (applied to the first len(head_blend)
        # steps) or tail_blend (applied to the last len(tail_blend) steps). Pass
        # [] for both to get the original, unblended per-step rotation exactly as
        # before -- this generator is a strict generalization, not a behavior
        # change, when there's nothing to blend.
        direction = 1.0 if x_end >= x_start else -1.0
        total_dist = abs(x_end - x_start)
        if total_dist <= 1e-9:
            return
        n_full = int(total_dist // step_size)
        remainder = total_dist - n_full * step_size
        dists = [step_size] * n_full
        if remainder > 1e-9:
            dists.append(remainder)
        n = len(dists)
        # Clamp the blend windows so they never overlap on a very short traversal.
        n_head = min(len(head_blend), n)
        n_tail = min(len(tail_blend), max(0, n - n_head))
        x = x_start
        for idx, d in enumerate(dists):
            x_next = x + direction * d
            a_delta = (d / pitch) * 360.0
            if idx < n_head:
                a_delta += head_blend[idx]
            elif idx >= n - n_tail:
                a_delta += tail_blend[idx - (n - n_tail)]
            yield x_next, a_delta
            x = x_next

    @staticmethod
    def compute_wind_angle_bounds(tank_length, tank_diameter, bandwidth):
        # Winding angles too close to 0 deg (nearly pure axial motion) or 90 deg
        # (nearly pure hoop motion) aren't physically realizable, and both push
        # `pitch = pi*diameter/tan(angle)` toward a division-by-a-tiny-number
        # blowup that's what actually causes the "calculates forever" symptom.
        #   - Near 90 deg: pitch (the axial advance per revolution) shrinks
        #     toward zero, so once it's smaller than the tow itself, each wrap
        #     winds directly on top of the last one ("lay filament on filament
        #     on filament"). The largest safe angle is where pitch == bandwidth
        #     exactly: pi*diameter/tan(angle) = bandwidth:
        #       angle_max = atan(pi*diameter / bandwidth)
        #   - Near 0 deg: the mandrel barely rotates at all over the length of
        #     one traversal. The circumferential distance covered over the full
        #     tank length is tank_length*tan(angle); the smallest safe angle is
        #     where that equals one tow-width (the strand must advance at least
        #     that far before it reverses, or it never clears space for itself):
        #       angle_min = atan(bandwidth / tank_length)
        # Both bounds are derived from the exact same pitch/shift relationships
        # used everywhere else in this app, not an arbitrary safety margin.
        if tank_length <= 0 or tank_diameter <= 0 or bandwidth <= 0:
            return 0.1, 89.9
        angle_max = min(89.9, math.degrees(math.atan((math.pi * tank_diameter) / bandwidth)))
        angle_min = max(0.1, math.degrees(math.atan(bandwidth / tank_length)))
        if angle_min >= angle_max:
            # A degenerate combination (e.g. an extremely wide tow on a tiny
            # tank) -- fall back to a narrow-but-valid band around the midpoint
            # instead of an inverted or empty range.
            mid = (angle_min + angle_max) / 2.0
            angle_min, angle_max = max(0.1, mid - 0.1), min(89.9, mid + 0.1)
        return angle_min, angle_max

    @staticmethod
    def compute_cycles_for_full_coverage(dt, wind_angle, bandwidth, pattern_number):
        # Each cycle lays `pattern_number` strands evenly spread around the full
        # circumference; between cycles the whole spread advances by exactly one
        # tow-width. So the tank is fully covered once (total bands needed for one
        # wrap) / pattern_number cycles have run, where total bands = circumference
        # / effective tow-width (tow-width widened by 1/cos(wind_angle) to account
        # for the helix angle, same conversion used for shift_degrees elsewhere).
        if wind_angle <= 0 or wind_angle >= 90 or bandwidth <= 0 or pattern_number <= 0:
            return 0.0
        circumference = math.pi * dt
        effective_bandwidth = bandwidth / math.cos(math.radians(wind_angle))
        total_bands = circumference / effective_bandwidth
        return total_bands / pattern_number

    def calc_R(self, x, x_start, x_end, r_tank, r_cap, l_dome, cap_type):
        if cap_type == "Flat": return r_tank
        cyl_start, cyl_end = x_start + l_dome, x_end - l_dome
        if x < cyl_start:
            return math.sqrt(max(r_tank**2 - (cyl_start - x)**2, r_cap**2))
        elif x > cyl_end:
            return math.sqrt(max(r_tank**2 - (x - cyl_end)**2, r_cap**2))
        return r_tank

    def calc_R_eye_footprint(self, x, x_start, x_end, r_tank, r_cap, l_dome, cap_type, eye_width):
        # The eye is physically eye_width wide along X, not a point. Near the tank
        # ends the radius changes quickly with X, so the Y clearance must account
        # for the largest radius anywhere under the eye's footprint (not just its
        # center), or the eye's trailing side can clip the tank. Sampled rather than
        # solved analytically so it stays correct even if the footprint is wider
        # than the tank itself.
        half_w = eye_width / 2.0
        x_lo = max(x_start, x - half_w)
        x_hi = min(x_end, x + half_w)
        n_samples = 6
        r_max = self.calc_R(x, x_start, x_end, r_tank, r_cap, l_dome, cap_type)
        for i in range(n_samples + 1):
            xi = x_lo + (x_hi - x_lo) * i / n_samples
            r_max = max(r_max, self.calc_R(xi, x_start, x_end, r_tank, r_cap, l_dome, cap_type))
        return r_max

    def calc_move(self, x0, y0, a0, x1, y1, a1, max_a_speed, r_next):
        # The combined (X,Y,A) feed rate is always set so the A-axis itself moves
        # at exactly max_a_speed -- there used to be a second, independent
        # "Speed Base (%)" ceiling here too, but at its default (100%) it was
        # always well above what max_a_speed already implied, so it never
        # actually did anything except make the achieved speed harder to reason
        # about if lowered. Removed: the machine's speed is defined by Max
        # Rotation Speed alone now.
        dx, dy, da = abs(x1 - x0), abs(y1 - y0), abs(a1 - a0)
        dist_klip = math.sqrt(dx**2 + dy**2 + da**2)
        act_f = max_a_speed * dist_klip / da if da > 0 and dist_klip > 0 else max_a_speed
        time_sec = (dist_klip / act_f) * 60.0 if act_f > 0 else 0
        tow_len = math.sqrt(dx**2 + (r_next * math.radians(da))**2)
        return act_f, time_sec, tow_len

    def simulate_winding(self, co, lt, dt, dc, ld, arm, space, passes, wind_angle, bandwidth, dwell, max_a_speed, cap_type, pattern_number=1, eye_width=0.0, optimize_trajectory=False):
        # Returns raw (x, r, angle_deg) points per strand/run instead of pre-projected
        # screen coordinates -- the front/back split and (px, py) projection depend on
        # the viewer's chosen azimuth, which can change (e.g. via the rotate-view
        # buttons) without re-running this simulation, so that work is left to the
        # caller. Points are grouped into "runs" (one per single-direction traversal,
        # i.e. broken at every turnaround) so the caller knows which points are
        # meant to be connected by a line and which are not.
        n_strands = max(1, pattern_number)
        if wind_angle <= 0 or wind_angle >= 90 or max_a_speed <= 0:
            return 0.0, 0.0, [[] for _ in range(n_strands)]
        r_tank, r_cap, x_start, x_end = dt/2.0, dc/2.0, co, co + lt
        y_base_dist = 550.0 - arm
        pitch = (math.pi * dt) / math.tan(math.radians(wind_angle))
        shift_degrees = (bandwidth / math.cos(math.radians(wind_angle)) / (math.pi * dt)) * 360.0
        step_size = 5.0
        total_time, total_tow = 0.0, 0.0
        strand_runs = [[] for _ in range(n_strands)]
        current_run = [None] * n_strands
        x_curr, a_curr = x_start, 0.0
        r_curr = self.calc_R(x_curr, x_start, x_end, r_tank, r_cap, ld, cap_type)
        r_eye_curr = self.calc_R_eye_footprint(x_curr, x_start, x_end, r_tank, r_cap, ld, cap_type, eye_width)
        y_curr = max(0.0, min(180.0, y_base_dist - (r_eye_curr + space)))
        n_cycles = max(1, passes)
        total_circuits = n_strands * n_cycles
        traverse_rotation = (2.0 * lt / pitch) * 360.0
        extra_within, extra_between = self.compute_dwell_extras(traverse_rotation, dwell, 360.0 / n_strands, shift_degrees)
        right_offset = self.compute_turnaround_balance_offset(total_circuits, n_strands, dwell, extra_within, extra_between)
        pending_head_blend = []  # blend carried from the previous dwell into this traversal's first steps
        for i in range(total_circuits):
            if i + 1 < total_circuits:
                extra = extra_between if (i + 1) % n_strands == 0 else extra_within
            else:
                extra = 0.0
            # See compute_turnaround_balance_offset: the right (outbound)
            # turnaround always gets the exact same fixed offset, every circuit,
            # so the return traversal's own pattern stays in step; the left
            # (return-side) turnaround absorbs whatever's left of this circuit's
            # `extra`, exactly as it always had to for the forward traversal's
            # pattern to land correctly next circuit.
            for direction in [1, -1]:
                target_x = x_end if direction == 1 else x_start
                dwell_amount = dwell + (right_offset if direction == 1 else (extra - right_offset))
                head_blend, tail_blend, core_dwell = self.compute_dwell_blend(dwell_amount, optimize_trajectory)
                is_last_segment = (i == total_circuits - 1) and (direction == -1)
                if is_last_segment:
                    # Nothing follows the very last dwell to absorb a head blend
                    # into, so fold it back into the dwell move itself instead of
                    # losing that rotation.
                    core_dwell += sum(head_blend)
                    head_blend = []
                for x_next, a_delta in self.traversal_steps(x_curr, target_x, step_size, pitch, pending_head_blend, tail_blend):
                    r_next = self.calc_R(x_next, x_start, x_end, r_tank, r_cap, ld, cap_type)
                    r_eye_next = self.calc_R_eye_footprint(x_next, x_start, x_end, r_tank, r_cap, ld, cap_type, eye_width)
                    y_next = max(0.0, min(180.0, y_base_dist - (r_eye_next + space)))
                    a_next = a_curr + a_delta
                    _, t_sec, tow = self.calc_move(x_curr, y_curr, a_curr, x_next, y_next, a_next, max_a_speed, r_next)
                    total_time, total_tow = total_time + t_sec, total_tow + tow
                    if i < n_strands:
                        if current_run[i] is None:
                            current_run[i] = [(x_curr, r_curr, a_curr)]
                        # step_size is a fixed X distance, not an angular one, so
                        # near the steep end of the wind-angle range (where pitch
                        # shrinks toward the tow width) a single step can sweep
                        # close to -- or, right at the computed max angle, exactly
                        # -- a full revolution. Recording only the step's endpoint
                        # then aliases the helix for rendering: whole visible arcs
                        # can fall entirely between two samples (dropped strand
                        # segments), and at the point where one step's sweep is an
                        # exact multiple of 360 deg, every sample lands at the same
                        # rotational phase, which flattens the drawn path into what
                        # looks like a shallow straight line instead of a tight
                        # helix. Subdividing by rotation (not distance) fixes the
                        # rendering without touching x_curr/a_curr/y_curr or the
                        # time/tow accumulation above, so the actual simulated
                        # state -- and therefore the G-code this mirrors -- is
                        # completely unaffected; only how densely the already-
                        # correct path gets sampled for drawing changes.
                        n_sub = max(1, min(60, math.ceil(abs(a_delta) / self.RENDER_MAX_DEG_PER_STEP)))
                        for k in range(1, n_sub + 1):
                            frac = k / n_sub
                            fx = x_curr + (x_next - x_curr) * frac
                            fa = a_curr + a_delta * frac
                            fr = r_next if k == n_sub else self.calc_R(fx, x_start, x_end, r_tank, r_cap, ld, cap_type)
                            current_run[i].append((fx, fr, fa))
                    x_curr, y_curr, a_curr, r_curr = x_next, y_next, a_next, r_next
                pending_head_blend = head_blend
                a_next = a_curr + core_dwell
                _, t_sec, tow = self.calc_move(x_curr, y_curr, a_curr, x_curr, y_curr, a_next, max_a_speed, r_curr)
                total_time, total_tow, a_curr = total_time + t_sec, total_tow + tow, a_next
                if i < n_strands and current_run[i] is not None:
                    strand_runs[i].append(current_run[i])
                    current_run[i] = None
        return total_time, total_tow, strand_runs

    def generate(self):
        try:
            co, lt, dt, dc, arm, space = [float(self.params[k].get()) for k in ["chuck_offset", "tank_length", "tank_diameter", "end_cap_diameter", "eye_arm_length", "min_spacing"]]
            eye_width = float(self.params["eye_width"].get())
            max_surface_speed, cap_type = float(self.params["max_surface_speed"].get()), self.params["end_cap_type"].get()
            max_a_speed = self.surface_speed_to_deg_per_min(max_surface_speed, dt)
            passes, wind_angle, bandwidth, dwell = int(self.params["passes"].get()), float(self.params["wind_angle"].get()), float(self.params["bandwidth"].get()), float(self.params["turnaround_angle"].get())
            pattern_number = int(self.params["pattern_number"].get())
            if not self.pattern_is_valid(passes, pattern_number):
                messagebox.showerror("Error", "Number of Cycles and Pattern Number must both be at least 1.")
                return
            if not self.x_bounds_ok(co, lt):
                messagebox.showerror("Error", f"X-Axis would reach {co + lt:.0f}mm, exceeding the carriage's travel limit of {self.MAX_X:.0f}mm.\nReduce Tank Length or Chuck X-Offset.")
                return
            rt, rc, ld = dt/2, dc/2, 0.0
            if cap_type == "Round": ld = min(math.sqrt(max(0, rt**2 - rc**2)), lt/2)
            pitch = (math.pi * dt) / math.tan(math.radians(wind_angle))
            shift_deg = (bandwidth / math.cos(math.radians(wind_angle)) / (math.pi * dt)) * 360.0
            n_strands, n_cycles = pattern_number, passes
            total_circuits = n_strands * n_cycles
            traverse_rotation = (2.0 * lt / pitch) * 360.0
            extra_within, extra_between = self.compute_dwell_extras(traverse_rotation, dwell, 360.0 / n_strands, shift_deg)
            right_offset = self.compute_turnaround_balance_offset(total_circuits, n_strands, dwell, extra_within, extra_between)
            # Suggest a name like "17_09_14_32-RND-00_07_26.gcode": date/time
            # generated, end-cap type, and estimated wind duration. Windows
            # filenames can't contain ":", so the HH:MM:SS estimate is written
            # with underscores instead, matching the date's separator style. The
            # duration is read from the Settings Preview's own live estimate
            # (already kept in sync with these same settings) rather than
            # recomputed here, so clicking Generate never blocks on a second full
            # simulation pass over a potentially long wind.
            cap_code = "RND" if cap_type == "Round" else "FLT"
            est_time_str = self.plan_tab.est_time_var.get().replace("Time:", "").strip().replace(":", "_")
            if not est_time_str: est_time_str = "00_00_00"
            suggested_name = f"{datetime.datetime.now().strftime('%d_%m_%H_%M')}-{cap_code}-{est_time_str}.gcode"
            filepath = filedialog.asksaveasfilename(defaultextension=".gcode", filetypes=[("G-Code Files", "*.gcode")], initialfile=suggested_name)
            if not filepath: return
            with open(filepath, 'w') as f:
                f.write("; --- WINDER SETTINGS ---\n")
                for k, v in self.params.items(): f.write(f"; {k}: {v.get()}\n")
                f.write(f"; ld: {ld:.3f}\n; -----------------------\n\n")
                if self.params["home_before_wind"].get():
                    f.write("G28\n")
                else:
                    # Skip homing: just define wherever the carriage/mandrel currently
                    # is as the zero reference for this wind, without moving.
                    f.write("G92 A0\n")
                if self.START_GCODE: f.write(self.START_GCODE + "\n")
                cx, ca, a_offset = co, 0.0, 0.0
                cr = self.calc_R(cx, co, co+lt, rt, rc, ld, cap_type)
                cr_eye = self.calc_R_eye_footprint(cx, co, co+lt, rt, rc, ld, cap_type, eye_width)
                cy = max(0.0, min(180.0, 550 - arm - (cr_eye + space)))
                f.write(f"G1 X{cx:.3f} Y{cy:.3f} A{(ca - a_offset):.3f} F{int(max_a_speed)}\n")
                optimize_trajectory = self.params["optimize_trajectory"].get()
                pending_head_blend = []  # blend carried from the previous dwell into this traversal's first steps
                for i in range(total_circuits):
                    if i + 1 < total_circuits:
                        extra = extra_between if (i + 1) % n_strands == 0 else extra_within
                    else:
                        extra = 0.0
                    # See compute_turnaround_balance_offset / the matching comment
                    # in simulate_winding: the right turnaround always gets the
                    # same fixed offset every circuit (keeping the return
                    # traversal's own pattern in step), and the left turnaround
                    # absorbs whatever's left of this circuit's `extra`.
                    for direction in [1, -1]:
                        target_x = co + lt if direction == 1 else co
                        dwell_amount = dwell + (right_offset if direction == 1 else (extra - right_offset))
                        head_blend, tail_blend, core_dwell = self.compute_dwell_blend(dwell_amount, optimize_trajectory)
                        is_last_segment = (i == total_circuits - 1) and (direction == -1)
                        if is_last_segment:
                            # Nothing follows the very last dwell to absorb a head
                            # blend into, so fold it back into the dwell move
                            # itself instead of losing that rotation.
                            core_dwell += sum(head_blend)
                            head_blend = []
                        for nx, a_delta in self.traversal_steps(cx, target_x, 5.0, pitch, pending_head_blend, tail_blend):
                            nr = self.calc_R(nx, co, co + lt, rt, rc, ld, cap_type)
                            nr_eye = self.calc_R_eye_footprint(nx, co, co + lt, rt, rc, ld, cap_type, eye_width)
                            ny = max(0.0, min(180.0, 550 - arm - (nr_eye + space)))
                            na = ca + a_delta
                            act_f, _, _ = self.calc_move(cx, cy, ca, nx, ny, na, max_a_speed, nr)
                            f.write(f"G1 X{nx:.3f} Y{ny:.3f} A{(na - a_offset):.3f} F{int(act_f)}\n")
                            cx, cy, ca = nx, ny, na
                        pending_head_blend = head_blend
                        na = ca + core_dwell
                        # Compute the feed rate BEFORE advancing `ca` -- calc_move
                        # needs the actual (a0, a1) pair, not (a1, a1), to know this
                        # is a real rotation. It happens to resolve to max_a_speed
                        # either way now that the feed rate is always exactly
                        # max_a_speed for a pure-rotation move, but keeping the
                        # correct (a0, a1) order here is still the honest thing to
                        # pass in.
                        act_f, _, _ = self.calc_move(cx, cy, ca, cx, cy, na, max_a_speed, cr)
                        ca = na
                        f.write(f"G1 X{cx:.3f} Y{cy:.3f} A{(ca - a_offset):.3f} F{int(act_f)}\n")
                    if (i + 1) % n_strands == 0:
                        f.write(f"; CYCLE_COMPLETE:{(i + 1) // n_strands}\n")
                        # Reset the firmware's A-axis position to 0 without moving, so the
                        # accumulated rotation over a long wind never approaches the axis's
                        # +/-9999999 limit. Subsequent A values are written relative to this.
                        f.write("G92 A0\n")
                        a_offset = ca
                if self.END_GCODE: f.write(self.END_GCODE + "\n")
            self.set_status(f"G-code generated: {filepath}")
            messagebox.showinfo("Success", f"G-Code generated!\nSaved to: {filepath}")
            self.view_tab.open_gcode_view(filepath)
        except Exception as e: messagebox.showerror("Error", str(e))

if __name__ == "__main__":
    root = tb.Window(themename=theme.LIGHT_THEME)
    app = CFRPWinderApp(root)
    root.mainloop()
