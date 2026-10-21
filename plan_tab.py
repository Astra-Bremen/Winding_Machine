import tkinter as tk
from tkinter import ttk
import tkinter.font as tkfont
import math
import sys
import threading
import queue
import time
import strength
import theme
import winding
from ttkbootstrap.widgets.tooltip import ToolTip

# (label, settings key) for every entry field, grouped as they appear in the
# settings panel. Machine/Tank/Winding Settings are global to the whole program;
# LAYUP_FIELDS are edited per layup through the layup switcher.
MACHINE_FIELDS = [
    ("Chuck X-Offset (mm)", "chuck_offset"),
    ("Eye Arm Length (mm)", "eye_arm_length"),
    ("Eye Width (mm)", "eye_width"),
    ("Min Spacing (Safety) (mm)", "min_spacing"),
    ("Max Rotation Speed (mm/s)", "max_surface_speed"),
    ("Max Filament Speed (mm/s)", "max_filament_speed"),
    ("Max Move Time (s)", "max_move_time"),
]
TANK_FIELDS = [("Straight Length (mm)", "straight_length"), ("Tank Diameter (mm)", "tank_diameter"), ("End Cap Diameter (mm)", "end_cap_diameter")]
WINDING_FIELDS = [("Bandwidth / Tow Width (mm)", "bandwidth"), ("Turnaround Zone (mm)", "turnaround_zone")]
LAYUP_FIELDS = [
    ("Number of Cycles", "passes"),
    ("Pattern Number (Strands per Cycle)", "pattern_number"),
    ("Winding Angle (Degrees)", "wind_angle"),
    ("Bulkhead Angle (Deg)", "bulkhead_angle"),
    ("Turnaround / Dwell Angle (Deg)", "turnaround_angle"),
    ("Additional X-Axis Travel (mm)", "x_extension"),
]
START_X_FIELD = ("Start Wind at X (mm)", "start_x")
# The two speed limits. Every move runs as fast as both allow -- whichever it
# reaches first sets its pace (see winding.calc_move) -- and a LIMIT tag marks
# the one that sets the selected layup's pace in the middle of the tank.
SPEED_LIMIT_KEYS = ("max_surface_speed", "max_filament_speed")
FIELD_LABELS = {key: label for label, key in MACHINE_FIELDS + [START_X_FIELD] + TANK_FIELDS + WINDING_FIELDS + LAYUP_FIELDS}
# What every input in the settings panel does, shown when hovering over it
# (its label or the field itself). Where the meaning depends on the current
# settings, a live note is added below (see PlanTab._update_field_notes).
FIELD_TIPS = {
    # Machine
    "chuck_offset": "Where the tank begins on the machine: the distance (mm) from X0, where homing leaves the "
                    "carriage, to the tank's chuck-side end. Every X in the program is measured from X0.",
    "eye_arm_length": "Length of the delivery eye's arm (mm). With the Y axis' 180 mm of travel, it sets how close "
                      "to and how far from the tank axis the eye can get: Eye Reach in the estimates, and the dashed "
                      "lines in the end view.",
    "eye_width": "Width of the eye along the tank (mm). The safety gap is kept across its full width, so the eye's "
                 "edges stay clear of the domes too, not just its center.",
    "min_spacing": "Gap the eye keeps from the tank surface (mm). The Y axis follows the tank's profile, domes "
                   "included, at this distance.",
    "max_surface_speed": "The fastest the mandrel may turn, given as the speed of the tank's surface. It sets the "
                         "pace of steep layups.",
    "max_filament_speed": "The fastest the fiber may leave the eye: the tow laid per second. It sets the pace of "
                          "shallow layups, where the carriage covers far more ground than the mandrel turns. Every "
                          f"move also stays below a fixed F{winding.MAX_FEED:.0f} failsafe.",
    "max_move_time": "Longest any single G-code move may take (s). After a PAUSE, Klipper still finishes every move "
                     "it has queued, so longer moves are split into equal pieces along the same path: the machine "
                     f"then stops within about 2 s plus this. At least {winding.MIN_MOVE_TIME:g} s.",
    "home_before_wind": "Starts the program by homing (G28): the eye pulls back, travels to Start Wind at X and "
                        "moves in, and the machine pauses so the fiber can be attached. Off: no homing; wherever the "
                        "machine stands is the start, and the wind begins at the tank's chuck-side end.",
    "start_x": "Machine X (mm) where the wind begins after homing; it winds toward +X from there. Auto (grey): "
               "where the chuck-side dome ends and the tank turns straight, or where a 90° first layup begins. "
               "Type a value to fix it; clear the field to go back to auto.",
    # Tank
    "end_cap_type": "Round: spherical domes that close down to the End Cap Diameter; the helical layers wind over "
                    "them. Flat: flat ends, no domes.",
    "straight_length": "Length of the tank's straight (cylindrical) section (mm), between the two domes. With round "
                       "end caps the domes come on top of it: the whole tank is this plus a dome at each end.",
    "tank_diameter": "Outer diameter of the straight section (mm). It also turns Max Rotation Speed, a surface "
                     "speed, into the mandrel's turning speed.",
    "end_cap_diameter": "Diameter (mm) of the polar opening at each dome's tip, where the dome ends and the boss "
                        "begins. No layup can turn around closer to the axis than this: winding down to it covers "
                        "the whole dome.",
    # Winding
    "bandwidth": "Width (mm) of the tow band as it lies on the tank. It decides how many cycles cover the tank "
                 "completely, and how far a 90° wind advances per turn.",
    "turnaround_zone": "Length (mm) at each end over which the carriage turns around: it runs in on the helix, "
                       "slows smoothly to a stop at the very end while the mandrel keeps turning, and runs back "
                       "out -- one smooth curve, so the machine never has to brake for a corner. The turnaround's "
                       "rotation is spread over the zone, so no fiber piles up in a ring at the end. Auto (grey): "
                       "the dome -- each layup turns over the part of it that it winds (its Bulkhead Angle), so "
                       "the fiber turns around beyond the straight section "
                       f"({winding.FLAT_TURNAROUND_ZONE:g} mm with flat end caps). Type a value to fix it; clear the "
                       "field to go back to auto. 0 = turn on the spot (the machine stops there).",
    "pause_after_layup": "Pauses (PAUSE) after every layup but the last, so the fiber can be checked, e.g. for "
                         "slipping, before the pattern changes. Resume on the machine to continue. Around a 90° "
                         "wind it pauses where the wind starts and ends, on the straight section.",
    # Layup
    "hoop": "One pass along the straight section only, never over the domes, each turn laid one band width on "
            "from the last so the bands lie edge to edge (just short of 90°). No pattern and no turnarounds, so the "
            "fields below don't apply.\n\nThe layup before it finishes its last turnaround and runs on at its own "
            "angle to the straight section, where the layup change happens. The 90° wind ends at the other end of "
            "the straight section: a helical layup after it goes on over that dome at its own angle, turns around "
            "there with its own dwell, and winds back from that end. A Winding Angle above "
            f"{winding.HOOP_ANGLE_THRESHOLD:g}° switches this on.",
    "double": "Only for a 90° wind: winds it a second time, back to where it began -- exactly like a second 90° "
              "layup right after it, with the pause between the two (Pause After Each Layup). The layups after it "
              "then start from the same end this one did.",
    "passes": "How many cycles this layup winds. Each cycle lays one band per strand (Pattern Number), evenly "
              "around the tank, then moves the pattern on. Auto (grey): the fewest cycles that cover the tank "
              "completely, following every change. Type a number to fix it; clear the field to go back to auto.",
    "pattern_number": "Strands per cycle: how many evenly spaced bands each cycle lays around the tank before the "
                      "pattern moves on. More strands give a finer diamond pattern.",
    "wind_angle": "Angle between the fiber and the tank's axis (degrees). Low angles run along the tank and cover "
                  f"the domes; high ones run around it. Above {winding.HOOP_ANGLE_THRESHOLD:g}° the layup becomes "
                  "a 90° wind.",
    "bulkhead_angle": "How far onto each dome the layup winds before it turns around: the angle at the dome's "
                      "center (on the axis, where the straight section ends) from the axis to the turning point. "
                      "90° winds the straight section only; the lower the angle, the closer to the polar opening "
                      "it turns, and at the opening's own angle (or below) it winds the whole dome. Auto (grey): "
                      "where the fiber turns by itself at this Winding Angle -- on a round dome at the same angle, "
                      "but never inside the polar opening. Type a value to fix it; clear the field to go back to "
                      "auto. The Bulkhead view beside the preview shows it.",
    "turnaround_angle": "Least rotation (degrees) at each end of the tank while the fiber turns around. A little "
                        "more is added where needed, so every circuit lands exactly on the pattern (Extra Rotation "
                        "in the estimates).",
    "x_extension": "How much further than where the layup turns (its Bulkhead Angle) the eye goes before turning "
                   "back (mm). The fiber trails behind the eye, so this lets it reach all the way out to that "
                   "point. The turnaround stretches out by this much, still one smooth curve, and its extra "
                   "rotation is counted in the pattern, so every circuit still lands exactly on it. Beyond the "
                   "tank's ends the eye keeps closing in smoothly until it's Min Spacing from the End Cap Diameter: "
                   "check that the chuck has room there. 0 = turn around right there; a negative value is set to 0.",
    # Preview
    "show_all": "Draws the first cycle of every layup on the tank at once, each in its own color, with a legend "
                "below; click a legend entry to select that layup. Off: only the selected layup.",
    "bulk_single": "Shows a single circuit in the Bulkhead view: one strand coming onto the dome, going around "
                   "it (dashed where it runs behind) and back off. Off: every circuit of the first cycle, one per "
                   "strand of the pattern, with the Bulkhead Angle drawn over them -- its line and arc from the "
                   "dome's center, the turning circle (dashed), the polar opening's angle (dotted) and, with "
                   "Additional X-Axis Travel, where the eye turns back (▼). While continuing a wind, the view shows "
                   "what's wound so far.",
}

# Entry fields use the same compact size as their labels (a ttk style can't
# set an entry's font; it has to be given to the widget itself).
FIELD_FONT = ("TkDefaultFont", 8)
LEGEND_FONT = ("TkDefaultFont", 8)
# The Estimates sidebar's rows: compact, a notch below the app's body text.
ESTIMATE_FONT = ("TkDefaultFont", 9)
LEGEND_FONT_ACTIVE = ("TkDefaultFont", 8, "bold")
# The side views' small print: a view's title, its labels, and a value.
VIEW_TITLE_FONT = ("TkDefaultFont", 8, "bold")
VIEW_LABEL_FONT = ("TkDefaultFont", 8)
VIEW_VALUE_FONT = ("TkDefaultFont", 9, "bold")
SWATCH_W, SWATCH_H = 24, 12
# The column beside the tank preview: the end-on Eye Reach view on top, the
# Bulkhead view below it.
SIDE_COLUMN_W = 196
BULK_ARROW_X = 12  # where the Bulkhead view's turn arrows sit, from its left edge
TANK_ARROW_X = 18  # where the tank preview's do (it keeps 40 px clear on each side)
# The tank preview's margins (px): a band on top for the markers' labels (and
# warnings), one at the foot for the caption, and room at each side.
TANK_TOP, TANK_BOTTOM, TANK_SIDE = 64, 40, 40
END_VIEW_H = 124
# How much of the straight section the Bulkhead view shows next to the dome
# (at least), and of the boss beyond it, as a share of the tank's radius --
# and how far onto the straight section it has the strands to draw, as much
# as it ever shows of it.
BULKHEAD_STUB, BULKHEAD_BOSS, BULKHEAD_REACH = 0.45, 0.22, 3.0


def layup_caption(layup, sep=" · "):
    """What a layup winds, in short: "45° · 30 cycles", or "90° wind"."""
    if layup.hoop:
        return "90° wind ×2" if layup.double else "90° wind"
    return f"{layup.wind_angle:g}°{sep}{layup.passes} cycle{'s' if layup.passes != 1 else ''}"


def format_hms(seconds):
    h, m = divmod(int(seconds), 3600); m, s = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def short_time(seconds):
    # "2:37" for short durations, "1:02:37" from an hour on.
    h, m = divmod(int(seconds), 3600); m, s = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _set_if_changed(var, value):
    # Setting a watched variable triggers a redraw; skip it when nothing changes.
    try:
        current = var.get()
    except tk.TclError:
        current = None
    if current != value:
        var.set(value)


def _drawn_parts(job, layup, runs):
    # The parts of these runs drawn as strands: all of them -- except a 90°
    # wind's pass, which is drawn as the band it is (only its run onto the
    # straight section stays a strand).
    if not layup.hoop:
        return runs
    first, last = job.hoop_span
    parts = []
    for run in runs:
        part = []
        for p in run:
            if first + 1e-6 < p[0] < last - 1e-6:
                if len(part) >= 2:
                    parts.append(part)
                part = []
            else:
                part.append(p)
        if len(part) >= 2:
            parts.append(part)
    return parts


def _bulkhead_parts(job, parts):
    # What of these runs lies over the far end's dome and the straight section
    # next to it -- as much of it as the Bulkhead view can show, whatever its
    # size (BULKHEAD_REACH) -- in finer detail, A in radians.
    cyl_end = job.chuck_offset + job.tank_length - job.dome_length
    x_near = max(job.chuck_offset + job.dome_length, cyl_end - BULKHEAD_REACH * job.tank_diameter / 2)
    out = []
    for part in parts:
        near = []
        for p in list(part) + [None]:  # (a sentinel ends the last stretch)
            if p is not None and p[0] >= x_near:
                near.append(p)
                continue
            if len(near) >= 2:
                out.append([(x, r, math.radians(a)) for x, r, a in winding.thin_path(near, 4.0, 6.0, 0.4)])
            near = []
    return out


def _preview_runs(job, runs_by_layup, first_circuit=None):
    """What the preview draws of some layups' strand runs ({layup: [run of
    (x, r, A deg)]}): {layup: (fine, coarse, bulkhead, bulkhead_single)}, each
    a list of runs thinned (winding.thin_path) with A in radians -- `coarse`
    for while the view spins, when sub-pixel detail can't be seen anyway;
    `bulkhead` just what lies over the far end's dome, in finer detail, for
    the Bulkhead view, and `bulkhead_single` the same of the layup's first
    circuit alone (from `first_circuit`, {layup: its runs}; else all of them)
    -- and a 90° wind's pass left out, since it's drawn as the band it is.
    Pure: runs on the worker thread, together with the calculation it belongs
    to."""
    drawn = {}
    for li, runs in runs_by_layup.items():
        layup = job.layups[li]
        parts = _drawn_parts(job, layup, runs)
        fine, coarse = [], []
        for part in parts:
            thin = winding.thin_path(part)
            fine.append([(x, r, math.radians(a)) for x, r, a in thin])
            coarse.append([(x, r, math.radians(a)) for x, r, a in winding.thin_path(thin, 20.0, 30.0, 3.0)])
        bulkhead = _bulkhead_parts(job, parts)
        if first_circuit is not None and li in first_circuit:
            single = _bulkhead_parts(job, _drawn_parts(job, layup, first_circuit[li]))
        else:
            single = bulkhead
        drawn[li] = fine, coarse, bulkhead, single
    return drawn


def _simulate_for_preview(job, previous=None, report=None):
    # The simulation -- picking up from `previous` (the last one) where the
    # settings allow -- plus each layup's first cycle as the preview draws it.
    # Each layup's is handed to `report((layup, drawn))` as soon as it's
    # complete, so the preview can draw it while the rest is still running.
    drawn = {}

    def on_layup(li, strand_runs):
        # strand_runs: per circuit of the first cycle, its runs.
        drawn.update(_preview_runs(job, {li: [run for runs in strand_runs for run in runs]},
                                   {li: strand_runs[0] if strand_runs else []}))
        if report is not None:
            report((li, drawn[li]))
    result = winding.simulate(job, previous, on_layup)
    return result, drawn


def _progress_for_preview(job, point):
    # The tank as wound up to `point`, its runs as the preview draws them.
    progress = winding.progress(job, point)
    return progress._replace(runs=_preview_runs(job, progress.runs))


class _Spinner:
    """Turns a view around the tank's axis while one of its turn arrows is
    held (see PlanTab._bind_turn_arrows), calling `on_frame()` to redraw it --
    every frame, and once more at rest (for full detail again).

    Velocity-based rather than a per-press "ease toward a target" animation:
    an earlier version re-triggered a position-ease curve on every hold-repeat
    tick, but a cubic ease-in-out has zero slope at its start, so restarting it
    every ~80ms meant the view barely got moving before being reset again --
    it visibly slowed the longer the button was held, then lurched through the
    whole backlog of queued steps in one 350ms burst once released. Tracking
    actual angular velocity (ramped by acceleration while held, ramped back
    down by deceleration once released) avoids that entirely: holding longer
    just means cruising at a steady top speed for longer, and releasing always
    decelerates smoothly from whatever speed it was actually at."""
    MAX_SPEED = 220.0  # deg/s
    ACCEL = 700.0      # deg/s², while held
    DECEL = 700.0      # deg/s², once released

    def __init__(self, root, on_frame):
        self._root, self._on_frame = root, on_frame
        self.azimuth = 0.0   # deg: which side of the tank faces the viewer
        self.direction = 0   # -1, 0, or +1 -- which way is currently held
        self.velocity = 0.0  # deg/s, signed
        self._job = None
        self._last = None

    @property
    def moving(self):
        return self.direction != 0 or abs(self.velocity) > 0.5

    def hold(self, direction):
        self.direction = direction
        if self._job is None and self.moving:
            self._last = time.monotonic()
            self._tick()

    def _tick(self):
        now = time.monotonic()
        dt = max(0.0, min(0.05, now - self._last))
        self._last = now
        target = self.direction * self.MAX_SPEED
        rate = self.ACCEL if self.direction != 0 else self.DECEL
        if self.velocity < target:
            self.velocity = min(target, self.velocity + rate * dt)
        elif self.velocity > target:
            self.velocity = max(target, self.velocity - rate * dt)
        self.azimuth = (self.azimuth + self.velocity * dt) % 360.0
        self._on_frame()
        if self.moving:
            self._job = self._root.after(16, self._tick)
        else:
            self.velocity, self._job = 0.0, None
            self._on_frame()  # at rest: full detail again


_PARTIAL = object()  # marks a worker's interim report in _BackgroundCalc's queue


class _BackgroundCalc:
    """Runs fn(key) on a worker thread, for the newest requested key only, and
    hands the outcome to on_result(key, result, error) on the GUI thread (from
    poll()). Only one calculation is ever in flight; a request made while one
    runs just overwrites the single pending slot -- which is exactly how an
    outdated queued calculation gets "cancelled": it's replaced before it ever
    starts. Each request gets a unique token, and only the newest request's
    outcome is delivered; anything else that arrives is stale and discarded.

    With `on_partial`, fn is called as fn(key, report): each report(update)
    on the worker reaches on_partial(key, update) on the GUI thread -- also
    for the newest request only -- before the outcome does."""

    def __init__(self, fn, on_result, defer, on_partial=None):
        self._fn, self._on_result, self._on_partial = fn, on_result, on_partial
        # The worker is started through `defer` (Tk's after_idle): once the GUI
        # has finished the redraw that asked for it. Started mid-redraw, it
        # would contend for the GIL with every Tk call still to come -- each one
        # hands the GIL over, and getting it back can take a whole switch
        # interval -- which made a settings change freeze the GUI for up to
        # half a second.
        self._defer = defer
        self._launch_scheduled = False
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
        if not self.busy and not self._launch_scheduled:
            self._launch_scheduled = True
            self._defer(self._launch)

    def _launch(self):
        self._launch_scheduled = False
        if self.pending is None or self.busy:
            return
        self.inflight, self.pending = self.pending, None
        self._thread = threading.Thread(target=self._work, args=self.inflight, daemon=True)
        self._thread.start()

    def _work(self, token, key):
        # Worker thread: pure math only, no Tkinter access.
        try:
            if self._on_partial is None:
                result = self._fn(key)
            else:
                result = self._fn(key, lambda update: self._queue.put((token, key, update, _PARTIAL)))
            self._queue.put((token, key, result, None))
        except Exception as e:
            self._queue.put((token, key, None, e))

    def poll(self):
        # GUI thread: the only place outcomes may touch widgets.
        try:
            while True:
                token, key, result, error = self._queue.get_nowait()
                if error is _PARTIAL:
                    if token == self._wanted:
                        self._on_partial(key, result)
                    continue
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
        self.machine_entries = {}

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
        defer = self.app.root.after_idle
        # Each simulation starts from the last finished one: after an edit to
        # one layup, the unchanged ones before it aren't wound again.
        self._sim = _BackgroundCalc(
            lambda job, report: _simulate_for_preview(job, self._result[1] if self._result else None, report),
            self._apply_calc_result, defer, on_partial=self._apply_partial_runs)
        self._prog = _BackgroundCalc(lambda key: _progress_for_preview(*key), self._apply_progress_result, defer)
        self._runs = {}  # layup -> its first cycle as the preview draws it (see _preview_runs)
        # The layups' first cycles of the simulation still running, as they
        # come in: (job, {layup: drawn runs}). Drawn until the result is in.
        self._partial = None
        self._partial_changed = False  # a visible layup came in: redraw after this poll
        # While a calculation runs, the GUI thread gets the GIL back after at
        # most this long (Python's default is 5 ms) -- and it gives it up on
        # every Tk call, so a redraw with a hundred of them stays quick.
        sys.setswitchinterval(0.0005)
        self._result = None        # (job, winding.SimulationResult) of the last finished simulation
        self._progress = None      # ((job, point), winding.Progress) of the last finished progress
        self.estimate = None       # (job, strength.Material, strength.Estimate) shown in the estimates
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
        # The tank preview and the Bulkhead view each turn on their own arrows
        # (see _Spinner).
        self._tank_spin = _Spinner(self.app.root, self._redraw_tank_strands)
        self._bulk_spin = _Spinner(self.app.root, self._draw_bulkhead_strands)

        # Preview-only (not a G-code setting): draw every layup's first cycle on
        # the tank at once, color-coded, with a legend -- instead of only the
        # layup currently selected in the settings panel.
        self.show_all_var = tk.BooleanVar(value=False)
        # Preview-only too: the Bulkhead view shows one circuit going around the
        # dome (on), or every circuit of the first cycle (off).
        self.bulk_single_var = tk.BooleanVar(value=False)
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
                label = ttk.Label(frame, text=label_text, style=LBL)
                label.grid(row=first_row + i, column=0, sticky="w", pady=1, padx=(0, 10))
                # Layup entries are bound to a variable later (on_layups_changed),
                # since which layup's variables they edit changes on every switch.
                var = self.app.params.get(key)
                entry = ttk.Entry(frame, width=12, style=ENT, font=FIELD_FONT, **({"textvariable": var} if var is not None else {}))
                entry.grid(row=first_row + i, column=1, sticky="e", pady=1)
                if store is not None: store[key] = entry
                self._add_tip(key, label, entry)
            frame.columnconfigure(0, weight=1)

        # 1. Machine Settings
        machine_frame = ttk.LabelFrame(left_panel, text="Machine Settings", padding=FRAME_PAD, style=FRM)
        machine_frame.pack(fill=tk.X, pady=FRAME_GAP)
        self._tips = {}
        add_fields(machine_frame, MACHINE_FIELDS, store=self.machine_entries)
        # Which speed limit sets the selected layup's pace: a small LIMIT tag
        # inside that field, made like the AUTO tags (see AutoEntry) and shown
        # by _update_speed_limit. A click on it reaches the field.
        self._limit_tags = {}
        for key in SPEED_LIMIT_KEYS:
            entry = self.machine_entries[key]
            tag = tk.Label(entry, text="LIMIT", font=("TkDefaultFont", 7, "bold"), bd=0, padx=0, pady=0, cursor="xterm")
            tag.bind("<Button-1>", lambda e, field=entry: (field.focus_set(), "break")[1])
            self._limit_tags[key] = tag
        # A plain ttk.Checkbutton, same as "Pause After Each Layup" in Winding
        # Settings -- just the compact Settings font, no color/Toolbutton style.
        home_check = ttk.Checkbutton(machine_frame, text="Home Axes (G28) Before Winding",
                                     variable=self.app.params["home_before_wind"], style="Settings.TCheckbutton")
        home_check.grid(row=len(MACHINE_FIELDS), column=0, columnspan=2, sticky="w", pady=(6, 2))
        self._add_tip("home_before_wind", home_check)
        # Depends on the checkbox above (indented under it, disabled without
        # homing): where the wind starts after homing. Auto: where the dome ends.
        row = len(MACHINE_FIELDS) + 1
        self._start_x_label = ttk.Label(machine_frame, text=START_X_FIELD[0], style=LBL)
        self._start_x_label.grid(row=row, column=0, sticky="w", pady=1, padx=(22, 10))
        self._start_x_entry = ttk.Entry(machine_frame, textvariable=self.app.params["start_x"], width=12, style=ENT, font=FIELD_FONT)
        self._start_x_entry.grid(row=row, column=1, sticky="e", pady=1)
        self._start_x_field = AutoEntry(self, self._start_x_entry, lambda: self.app.params["start_x_auto"],
                                        self._start_x_mode_changed)
        self._add_tip("start_x", self._start_x_label, self._start_x_entry)

        # 2. Tank Settings
        tank_frame = ttk.LabelFrame(left_panel, text="Tank Settings", padding=FRAME_PAD, style=FRM)
        tank_frame.pack(fill=tk.X, pady=FRAME_GAP)
        cap_label = ttk.Label(tank_frame, text="End-Cap Type", style=LBL)
        cap_label.grid(row=0, column=0, sticky="w", pady=1, padx=(0, 10))
        self.cap_type_combo = ttk.Combobox(tank_frame, textvariable=self.app.params["end_cap_type"], values=["Round", "Flat"], state="readonly", width=10, style=CMB, font=FIELD_FONT)
        self.cap_type_combo.grid(row=0, column=1, sticky="e", pady=1)
        self._add_tip("end_cap_type", cap_label, self.cap_type_combo)
        self.app.params["end_cap_type"].trace_add("write", self.on_cap_type_change)
        add_fields(tank_frame, TANK_FIELDS, first_row=1, store=self.tank_entries)

        # 3. Winding Settings -- shared by every layup of the program.
        winding_frame = ttk.LabelFrame(left_panel, text="Winding Settings", padding=FRAME_PAD, style=FRM)
        winding_frame.pack(fill=tk.X, pady=FRAME_GAP)
        winding_entries = {}
        add_fields(winding_frame, WINDING_FIELDS, store=winding_entries)
        # Auto: the dome (see winding.WindingJob.turnaround_zone).
        self._zone_field = AutoEntry(self, winding_entries["turnaround_zone"],
                                     lambda: self.app.params["turnaround_zone_auto"], self._zone_mode_changed)
        # PAUSE between layups, to check the fiber after every pattern change.
        pause_check = ttk.Checkbutton(winding_frame, text="Pause After Each Layup",
                                      variable=self.app.params["pause_after_layup"], style="Settings.TCheckbutton")
        pause_check.grid(row=len(WINDING_FIELDS), column=0, columnspan=2, sticky="w", pady=(4, 0))
        self._add_tip("pause_after_layup", pause_check)

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
        for button, tip in ((self._prev_btn, "Previous layup"), (self._next_btn, "Next layup"),
                            (self._remove_btn, "Remove this layup"),
                            (self._add_btn, "Add a layup after the last one, with its settings (its Number of "
                                            "Cycles and Bulkhead Angle start on auto)")):
            ToolTip(button, text=tip, wraplength=300, delay=400)
        layup_frame = ttk.LabelFrame(left_panel, labelwidget=nav, padding=FRAME_PAD, style=FRM)
        layup_frame.pack(fill=tk.X, pady=FRAME_GAP)
        nav.lift(layup_frame)  # a labelwidget must stack above its frame to be visible
        # The 90° switch heads the section: it decides whether the fields below
        # apply. Beside it, "Double" (a 90° wind's only setting) winds it there
        # and back. Their variables are the selected layup's (see
        # _apply_layup_mode).
        hoop_row = ttk.Frame(layup_frame)
        hoop_row.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 3))
        self._hoop_check = ttk.Checkbutton(hoop_row, text="90° Wind", style="Settings.TCheckbutton",
                                           command=self._on_hoop_toggled)
        self._hoop_check.pack(side=tk.LEFT)
        self._add_tip("hoop", self._hoop_check)
        self._double_check = ttk.Checkbutton(hoop_row, text="Double", style="Settings.TCheckbutton",
                                             command=self._on_double_toggled)
        self._double_check.pack(side=tk.LEFT, padx=(18, 0))
        self._add_tip("double", self._double_check)
        add_fields(layup_frame, LAYUP_FIELDS, first_row=1, store=self.layup_entries)
        # What a 90° wind's greyed-out fields show instead of its (unused)
        # helical settings: its pass(es), its actual angle, the straight
        # section only (90°), no pattern, dwell or travel beyond. A field that
        # doesn't apply to the tank (the Bulkhead Angle with flat end caps)
        # shows "—" the same way.
        self._hoop_display = {key: tk.StringVar(value="—") for key in self.layup_entries}
        self._hoop_display["passes"].set("1")
        self._hoop_display["bulkhead_angle"].set("90")
        self._no_value = tk.StringVar(value="—")
        for key, check in (("x_extension", self._validate_x_extension), ("bulkhead_angle", self._validate_bulkhead)):
            self.layup_entries[key].bind("<FocusOut>", check, add="+")
            self.layup_entries[key].bind("<Return>", check, add="+")
        self._angle_before_edit = None
        self.layup_entries["wind_angle"].bind("<FocusIn>", self._remember_angle, add="+")
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
        # Auto: where the fiber turns by itself at the layup's winding angle.
        self._bulkhead_field = AutoEntry(self, self.layup_entries["bulkhead_angle"],
                                         lambda: self.app.layups[self.app.active_layup]["bulkhead_auto"],
                                         self._bulkhead_mode_changed)

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

        # Visualization Section, a grid: on top, left to right, the main
        # (pseudo-3D) tank canvas, a column with the end-on view keeping the
        # eye-reach lines over the Bulkhead view, and the Estimates sidebar
        # (the full height). Below, the views' checkboxes in one row, on one
        # line: "Show All Layups" (with the legend it reveals) under the tank
        # canvas, "View Single" under the Bulkhead view. Both views' turn
        # arrows are drawn inside them (see _draw_turn_arrows).
        viz_row = ttk.Frame(self.viz_parent)
        viz_row.pack(fill=tk.BOTH, expand=True)
        viz_row.columnconfigure(0, weight=1)
        viz_row.rowconfigure(0, weight=1)
        CHECK_PAD = (6, 0)  # above the checkbox row

        bottom_bar = ttk.Frame(viz_row)
        bottom_bar.grid(row=1, column=0, sticky="new", pady=CHECK_PAD)
        show_all = ttk.Checkbutton(bottom_bar, text="Show All Layups", variable=self.show_all_var,
                                   style="Settings.TCheckbutton", command=self._on_show_all_toggled)
        show_all.pack(side=tk.LEFT, anchor="n")
        self._add_tip("show_all", show_all)
        self.legend_canvas = tk.Canvas(bottom_bar, height=1, highlightthickness=0, bd=0)
        self.legend_canvas.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(18, 0))
        self.legend_canvas.bind("<Configure>", lambda e: self._draw_legend())

        self._build_estimates_panel(viz_row).grid(row=0, column=2, rowspan=2, sticky="ns")

        # The column: the end view on top, the Bulkhead view under it -- the
        # same width and frame. The Bulkhead view's own turn arrows are drawn
        # inside it, at its left edge (see _draw_turn_arrows), so nothing
        # sticks out of the column.
        side_column = ttk.Frame(viz_row, width=SIDE_COLUMN_W)
        side_column.grid(row=0, column=1, sticky="ns", padx=(6, 6))
        side_column.grid_propagate(False)
        side_column.columnconfigure(0, weight=1)
        side_column.rowconfigure(1, weight=1)
        canvas_style = dict(bg=self.app.canvas_palette["canvas_bg"], relief="flat", borderwidth=1, highlightthickness=1,
                            highlightbackground=self.app.canvas_palette["shaft_outline"])
        self.end_canvas = tk.Canvas(side_column, height=END_VIEW_H, **canvas_style)
        self.end_canvas.grid(row=0, column=0, sticky="ew")
        self.end_canvas.bind("<Configure>", lambda e: self.schedule_redraw())
        # The selected layup's turnaround up close: one dome in profile, its
        # Bulkhead Angle, and its first cycle's strands over it -- one circuit
        # or all of them ("View Single"), turned on arrows of its own.
        self.bulkhead_canvas = tk.Canvas(side_column, height=1, **canvas_style)
        self.bulkhead_canvas.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        self.bulkhead_canvas.bind("<Configure>", lambda e: self.schedule_redraw())
        self._bind_turn_arrows(self.bulkhead_canvas, self._bulk_spin)
        # In the checkbox row, under the Bulkhead view (lined up with it).
        self._bulk_single_check = ttk.Checkbutton(viz_row, text="View Single", variable=self.bulk_single_var,
                                                  style="Settings.TCheckbutton", command=self._draw_bulkhead_strands)
        self._bulk_single_check.grid(row=1, column=1, sticky="nw", padx=(6, 6), pady=CHECK_PAD)
        self._add_tip("bulk_single", self._bulk_single_check)
        self._bulk_geom = None     # (scale, junction x, axis y) the Bulkhead view is drawn with
        self._bulk_outline = None  # the dome's silhouette there (screen points)

        self.canvas = tk.Canvas(viz_row, bg=self.app.canvas_palette["canvas_bg"], relief="flat",
                                 borderwidth=1, highlightthickness=1,
                                 highlightbackground=self.app.canvas_palette["shaft_outline"])
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", lambda e: self.schedule_redraw())
        self._bind_turn_arrows(self.canvas, self._tank_spin)
        self.on_cap_type_change()

    def _build_estimates_panel(self, parent):
        # A slim sidebar beside the pattern preview with two stacked groups:
        # "Program" (the whole wind, every layup back to back) and the layup
        # selected in the settings panel, which follows the layup switcher.
        # Compact rows -- a short label and one value, or two related ones
        # ("1169 m · 1.87 kg": the tow's length and its dry fiber mass).
        # Hovering a row says what it shows; clicking the rotation or X-speed
        # changes their units. The masses and the safety factor come from
        # strength.estimate(), set up in the Strength & Materials window.
        # Returned to be placed by the caller.
        panel = ttk.Frame(parent, padding=(12, 0, 2, 0))
        panel.columnconfigure(1, weight=1)
        self.est, self._est_values = {}, {}

        def heading(text=None, textvariable=None, top=0):
            ttk.Label(panel, text=text, textvariable=textvariable, font=("TkHeadingFont", 10, "bold")).grid(
                row=panel.grid_size()[1], column=0, columnspan=2, sticky="w", pady=(top, 3))

        def estimate(key, name, tip, style=None, on_click=None):
            row = panel.grid_size()[1]
            self.est[key] = tk.StringVar(value="--")
            name_label = ttk.Label(panel, text=name, font=ESTIMATE_FONT)
            name_label.grid(row=row, column=0, sticky="w", padx=(0, 12), pady=1)
            value = ttk.Label(panel, textvariable=self.est[key], anchor="e", font=ESTIMATE_FONT,
                              **({"style": style} if style else {}))
            value.grid(row=row, column=1, sticky="e", pady=1)
            for widget in (name_label, value):
                ToolTip(widget, text=tip, wraplength=260, delay=500)
            if on_click:
                # Styled identically to every other readout (no color/underline)
                # so it doesn't announce itself -- the hand cursor and the
                # tooltip are the hints.
                value.configure(cursor="hand2")
                value.bind("<Button-1>", on_click)
            self._est_values[key] = value

        heading("Program")
        estimate("time", "Time", "Total winding time · cycles in all layups together.")
        estimate("tow", "Tow", "Tow length · its dry fiber mass (set up in Strength & Materials).")
        estimate("resin", "Resin", "Resin mass for that fiber (set up in Strength & Materials).")
        estimate("rotation", "Rotation", "Top mandrel speed (at the turnarounds): Max Rotation Speed, within "
                                         f"the F{winding.MAX_FEED:.0f} failsafe. Click to change units.",
                 on_click=self._cycle_rotation_speed_unit)
        estimate("reach", "Eye Reach", "How close to and how far from the tank axis the eye can get across its "
                                       "Y travel.", style="warning.TLabel")
        estimate("safety_factor", "Safety Factor", "Rough strength estimate at the operating pressure: allowable "
                                                   "÷ actual hoop stress (Strength & Materials). Orange below 1.")
        ttk.Button(panel, text="Strength & Materials…", style="primary.Outline.TButton",
                   command=self.app.show_strength_dialog).grid(row=panel.grid_size()[1], column=0, columnspan=2,
                                                               sticky="ew", pady=(6, 0))

        self.layup_heading_var = tk.StringVar(value="Layup")
        heading(textvariable=self.layup_heading_var, top=14)
        estimate("layup_time", "Time", "This layup's winding time · its average time per cycle.")
        estimate("layup_tow", "Tow", "This layup's tow length · its dry fiber mass.")
        estimate("layup_resin", "Resin", "Resin mass for this layup's fiber.")
        estimate("xspeed", "X-Speed", "Carriage speed in the middle of the tank, set by the speed limit tagged "
                                       "LIMIT in Machine Settings. Click to change units.",
                 on_click=self._cycle_xspeed_unit)
        estimate("coverage", "Coverage", "How much of the surface the layup's bands cover · the fewest cycles "
                                         "that cover it completely. Orange while gaps remain.")
        estimate("extra", "Extra Rotation", "Alignment rotation added at the far-end / chuck-side turnaround "
                                            "(first circuit), on top of the dwell angle.")

        # Raw values cached in their base unit so clicking just re-formats them,
        # with no recalculation needed (both are closed-form: see
        # _update_instant_estimates).
        self._rotation_speed_deg_per_min = 0.0
        self._rotation_speed_unit_idx = 0
        self.ROTATION_SPEED_UNITS = ["rpm", "°/s", "°/min"]
        self._xspeed_mm_s = 0.0
        self._xspeed_unit_idx = 0
        self.XSPEED_UNITS = ["mm/s", "m/s"]
        return panel

    # --- Hover explanations ---

    def _add_tip(self, key, *widgets):
        # The explanation of setting `key` (FIELD_TIPS) on its label and field.
        self._tips.setdefault(key, []).extend(ToolTip(w, text=FIELD_TIPS[key], wraplength=300, delay=400)
                                              for w in widgets)

    def _set_note(self, key, note=None):
        # A live note below a setting's explanation (or none).
        text = FIELD_TIPS[key] + (f"\n\n{note}" if note else "")
        for tip in self._tips.get(key, ()):
            tip.text = text

    def _update_field_notes(self, job, layup):
        # What the explanations can't know in advance: the current tank's
        # limits, the auto values, and which fields a 90° wind ignores. Without
        # a valid job (a field mid-edit) they show the plain explanations.
        notes = dict.fromkeys(FIELD_TIPS)
        if job is not None:
            lo, hi = winding.eye_reach(job.eye_arm_length)
            notes["eye_arm_length"] = (f"With this arm the eye reaches {lo:.0f}–{hi:.0f} mm from the tank axis; "
                                       f"the tank's radius is {job.tank_diameter / 2:.0f} mm.")
            if job.home_before_wind:
                lo, hi = job.start_x_range
                auto = f" Auto now: X {job.start_x:.1f}." if job.start_x_auto else ""
                notes["start_x"] = f"Allowed on this tank: X {lo:.1f} to {hi:.1f} mm.{auto}"
            else:
                notes["start_x"] = "Not used while homing is off: the wind then starts at the tank's end."
            round_caps = job.end_cap_type == "Round" and job.dome_length > 0
            x_start, x_end = job.chuck_offset, job.chuck_offset + job.tank_length
            notes["straight_length"] = (f"The whole tank: {job.tank_length:.1f} mm end to end, a {job.dome_length:.1f} "
                                        f"mm dome at each end (X {x_start:g}–{x_end:.1f})." if round_caps else
                                        "With flat end caps the whole tank is the straight section.")
            if job.end_cap_type == "Flat":
                notes["end_cap_diameter"] = "Not used with flat end caps."
            elif round_caps:
                notes["end_cap_diameter"] = (f"Its Bulkhead Angle: {winding.opening_angle(job):.1f}° -- a layup "
                                             "at this or lower winds the whole dome.")
            if job.turnaround_zone_auto:
                where = "the dome; each layup turns over the part it winds" if round_caps else "flat end caps"
                notes["turnaround_zone"] = f"Auto now: up to {job.turnaround_zone:.1f} mm ({where})."
            notes["turnaround_zone"] = ((notes["turnaround_zone"] + " " if notes["turnaround_zone"] else "")
                                        + f"At most {job.tank_length / 2:g} mm on this tank (half its whole length).")
            if len(job.layups) == 1:
                notes["pause_after_layup"] = "With a single layup there's nothing to pause between."
            if layup.hoop:
                notes["passes"] = ("A double 90° wind is two passes: there and back." if layup.double
                                   else "A 90° wind is always exactly one pass.")
                notes["pattern_number"] = notes["turnaround_angle"] = "Not used by a 90° wind."
                notes["wind_angle"] = f"Not used by a 90° wind: it winds at {job.hoop_angle:.1f}°."
                notes["x_extension"] = "Not used by a 90° wind: it stays on the straight section."
                notes["bulkhead_angle"] = "A 90° wind stays on the straight section: 90°."
            else:
                notes["double"] = "Only for a 90° wind."
                turn_lo, turn_hi = winding.turn_points(job, layup)
                notes["x_extension"] = (f"This layup's eye turns at X {turn_lo - layup.x_extension:.1f} and "
                                        f"X {turn_hi + layup.x_extension:.1f} (the tank spans {x_start:g}–{x_end:.1f}).")
                if round_caps:
                    cover = winding.bulkhead_cover(job, layup)
                    auto = winding.auto_bulkhead_angle(job, layup.wind_angle)
                    whole = cover >= job.dome_length
                    notes["bulkhead_angle"] = (
                        (f"Auto now: {auto:.1f}°. " if layup.bulkhead_auto else f"Auto would be {auto:.1f}°. ")
                        + ("This layup winds the whole dome" if whole else
                           f"This layup winds {cover:.1f} of each dome's {job.dome_length:.1f} mm")
                        + f", turning at X {turn_lo:.1f} and X {turn_hi:.1f}. The whole dome from "
                          f"{winding.opening_angle(job):.1f}° (the polar opening) down.")
                else:
                    notes["bulkhead_angle"] = "Not used with flat end caps: there's no dome."
                needed = winding.full_coverage_cycles(job.tank_diameter, layup.wind_angle, job.bandwidth,
                                                      layup.pattern_number)
                if needed is not None:
                    notes["passes"] = f"Full coverage at these settings: {needed} cycle{'s' if needed != 1 else ''}."
                if job.tank_length > 0 and job.tank_diameter > 0 and job.bandwidth > 0:
                    lo, hi = winding.compute_wind_angle_bounds(job.tank_length, job.tank_diameter, job.bandwidth)
                    hi = min(hi, winding.HOOP_ANGLE_THRESHOLD)
                    notes["wind_angle"] = f"Possible on this tank with this tow: {lo:.1f}° to {hi:.1f}°."
        for key, note in notes.items():
            if key not in SPEED_LIMIT_KEYS:  # those are _update_speed_limit's
                self._set_note(key, note)

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

    def _bulkhead_mode_changed(self, auto):
        n = len(self.app.layups)
        prefix = f"Layup {self.app.active_layup + 1}: " if n > 1 else ""
        value = self.layup_entries["bulkhead_angle"].get().strip()
        if auto:
            self.app.set_status(f"{prefix}Bulkhead Angle is back on auto -- {value}°, where the fiber turns by "
                                "itself at this winding angle.")
        else:
            self.app.set_status(f"{prefix}Bulkhead Angle set to {value}° and fixed there. "
                                "Clear the field to put it back on auto.")

    def _zone_mode_changed(self, auto):
        value = self._zone_field.entry.get().strip()
        if auto:
            self.app.set_status(f"Turnaround Zone is back on auto -- {value} mm, the dome "
                                f"({winding.FLAT_TURNAROUND_ZONE:g} mm with flat end caps).")
        else:
            self.app.set_status(f"Turnaround Zone set to {value} mm and fixed there. "
                                "Clear the field to put it back on auto (the dome).")

    def _start_x_mode_changed(self, auto):
        value = self._start_x_entry.get().strip()
        if auto:
            where = ("where Layup 1's 90° wind begins" if self._job is not None and self._job.layups[0].hoop
                     else "where the dome ends and the tank turns straight")
            self.app.set_status(f"Start Wind at X is back on auto -- {value} mm, {where}.")
        else:
            self.app.set_status(f"Start Wind at X set to {value} mm and fixed there. "
                                "Clear the field to put it back on auto (where the dome ends).")

    def _sync_auto_values(self, job):
        # Writes each auto setting's computed value into its field -- for the
        # layups not on screen too, so switching shows the right number at once.
        # A field the user is typing in is left alone until they leave it, so a
        # cleared field isn't refilled under their cursor.
        editing = self._cycles_field.editing()
        editing_bulkhead = self._bulkhead_field.editing()
        for i, (layup_vars, layup) in enumerate(zip(self.app.layups, job.layups)):
            if layup.hoop:
                continue
            if layup.bulkhead_auto and not (editing_bulkhead and i == self.app.active_layup):
                _set_if_changed(layup_vars["bulkhead_angle"], round(layup.bulkhead_angle, 1))
            if not layup.auto_cycles or (editing and i == self.app.active_layup):
                continue
            if winding.full_coverage_cycles(job.tank_diameter, layup.wind_angle, job.bandwidth, layup.pattern_number) is None:
                continue  # not computable right now; geometry_errors() explains why
            _set_if_changed(layup_vars["passes"], layup.passes)
        if job.start_x_auto and not self._start_x_field.editing():
            _set_if_changed(self.app.params["start_x"], round(job.start_x, 1))
        if job.turnaround_zone_auto and not self._zone_field.editing():
            _set_if_changed(self.app.params["turnaround_zone"], round(job.turnaround_zone, 1))
        # Start Wind at X only applies when homing first.
        state = ["!disabled"] if job.home_before_wind else ["disabled"]
        self._start_x_entry.state(state)
        self._start_x_label.state(state)
        self._cycles_field.refresh()
        self._bulkhead_field.refresh()
        self._start_x_field.refresh()
        self._zone_field.refresh()

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
        self._apply_layup_mode()
        self.layup_title_var.set(f"Layup {i + 1} of {n}")
        self.layup_heading_var.set(f"Layup {i + 1}" if n > 1 else "Layup")
        self._prev_btn.configure(state="normal" if i > 0 else "disabled")
        self._next_btn.configure(state="normal" if i < n - 1 else "disabled")
        self._remove_btn.configure(state="normal" if n > 1 else "disabled")
        self._draw_layup_swatch()
        self._cycles_field.rebind()
        self._bulkhead_field.rebind()
        # Redraw right away rather than debounced: nothing here is slow (a
        # switch that doesn't change any setting redraws from the cached
        # simulation), and an instant response is what makes switching feel snappy.
        if self.redraw_timer:
            self.app.root.after_cancel(self.redraw_timer)
            self.redraw_timer = None
        self.draw_visualization()

    def _apply_layup_mode(self):
        # Points the layup section at the selected layup: its 90° checkbox, and
        # its entry fields -- or, for a 90° wind, greyed-out fields showing what
        # it winds instead. Its own helical values stay untouched underneath, so
        # unchecking the box brings them straight back.
        layup_vars = self.app.layups[self.app.active_layup]
        self._hoop_check.configure(variable=layup_vars["hoop"])
        self._double_check.configure(variable=layup_vars["double"])
        hoop = self._is_hoop(layup_vars)
        # "Double" is a 90° wind's own setting.
        self._double_check.state(["!disabled"] if hoop else ["disabled"])
        self._hoop_display["passes"].set("2" if hoop and self._is_set(layup_vars["double"]) else "1")
        # Without a dome there's no Bulkhead Angle to set.
        flat = self.app.params["end_cap_type"].get() == "Flat"
        for key, entry in self.layup_entries.items():
            if hoop:
                variable, off = self._hoop_display[key], True
            elif key == "bulkhead_angle" and flat:
                variable, off = self._no_value, True
            else:
                variable, off = layup_vars[key], False
            entry.configure(textvariable=variable)
            entry.state(["disabled"] if off else ["!disabled"])
        self._cycles_field.refresh()
        self._bulkhead_field.refresh()

    @staticmethod
    def _is_set(var):
        try:
            return bool(var.get())
        except tk.TclError:
            return False

    @staticmethod
    def _is_hoop(layup_vars):
        return PlanTab._is_set(layup_vars["hoop"])

    def _on_double_toggled(self):
        layup_vars = self.app.layups[self.app.active_layup]
        i, n = self.app.active_layup, len(self.app.layups)
        prefix = f"Layup {i + 1}: " if n > 1 else ""
        if self._is_set(layup_vars["double"]):
            text = ("the 90° wind goes there and back, pausing at the far end like between two layups." if prefix
                    else "The 90° wind goes there and back, pausing at the far end like between two layups.")
            if i < n - 1:
                text += f" The layup{'s' if i < n - 2 else ''} after it now start{'' if i < n - 2 else 's'} from the same end it does."
        else:
            text = "the 90° wind is a single pass again." if prefix else "The 90° wind is a single pass again."
            if i < n - 1:
                text += f" The layup{'s' if i < n - 2 else ''} after it now start{'' if i < n - 2 else 's'} from the other end."
        self.app.set_status(prefix + text)
        self._apply_layup_mode()
        self._redraw_now()

    def _on_hoop_toggled(self):
        # The checkbox itself was clicked.
        layup_vars = self.app.layups[self.app.active_layup]
        if self._is_hoop(layup_vars):
            self._settle_helical_angle(layup_vars)
            self._announce_hoop(True, "90° wind: one pass over the straight section, each turn one band width on "
                                      "from the last.")
        else:
            angle = self._settle_helical_angle(layup_vars)
            back = f" at {angle:g}°" if angle is not None else ""
            self._announce_hoop(False, f"wound helically again{back}.")
        self._apply_layup_mode()
        self._redraw_now()

    def _announce_hoop(self, hoop, text):
        i, n = self.app.active_layup, len(self.app.layups)
        prefix = f"Layup {i + 1}: " if n > 1 else ""
        text = prefix + (text if prefix else text[:1].upper() + text[1:])
        # (A double one winds back to where it began: nothing after it moves.)
        if i < n - 1 and not self._is_set(self.app.layups[i]["double"]):
            text += f" The layup{'s' if i < n - 2 else ''} after it now start{'' if i < n - 2 else 's'} from the other end."
        self.app.set_status(text)

    def _remember_angle(self, event=None):
        # The angle before this edit: what a layup keeps as its helical angle if
        # the edit turns it into a 90° wind.
        try:
            self._angle_before_edit = (self.app.active_layup,
                                       float(self.app.layups[self.app.active_layup]["wind_angle"].get()))
        except (tk.TclError, ValueError):
            self._angle_before_edit = None

    def _settle_helical_angle(self, layup_vars):
        # A 90° wind keeps its layup's helical angle for when it's unchecked;
        # an angle above the 90° threshold can't be that, so it goes back to the
        # angle from before the edit (or the threshold). Returns the angle, or
        # None while the field doesn't hold a number.
        limit = winding.HOOP_ANGLE_THRESHOLD
        try:
            angle = float(layup_vars["wind_angle"].get())
        except (tk.TclError, ValueError):
            return None
        if angle <= limit:
            return angle
        before = self._angle_before_edit
        keep = before[1] if before and before[0] == self.app.active_layup and 0 < before[1] <= limit else limit
        layup_vars["wind_angle"].set(keep)
        return keep

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
        self.app.set_status(f"Layup {n} added with the settings of Layup {n - 1}; its Number of Cycles and "
                            "Bulkhead Angle are on auto.")

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
        if self._is_hoop(layup_vars):
            return  # its angle field isn't in use
        try:
            job = self.app.build_job()  # for the whole tank's length
            angle = float(layup_vars["wind_angle"].get())
        except (winding.SettingsError, tk.TclError, ValueError):
            return  # field is mid-edit / not a valid number yet -- leave it alone
        lt, dt, bandwidth = job.tank_length, job.tank_diameter, job.bandwidth
        if angle > winding.HOOP_ANGLE_THRESHOLD:
            # Steeper than that is a 90° wind: switch the layup over.
            keep = self._settle_helical_angle(layup_vars)
            layup_vars["hoop"].set(True)
            self._announce_hoop(True, f"a Winding Angle above {winding.HOOP_ANGLE_THRESHOLD:g}° is wound as a 90° "
                                      f"wind over the straight section. Uncheck it to wind helically again (at {keep:g}°).")
            self._apply_layup_mode()
            self._redraw_now()
            return
        angle_min, angle_max = winding.compute_wind_angle_bounds(lt, dt, bandwidth)
        which = f" for Layup {self.app.active_layup + 1}" if len(self.app.layups) > 1 else ""
        if angle < angle_min:
            layup_vars["wind_angle"].set(round(angle_min, 2))
            self.app.set_status(f"Winding Angle{which} raised to {angle_min:.2f}° -- the lowest realistic angle for this tank/tow width (below it, the strand barely advances around the tank at all).")
        elif angle > angle_max:
            layup_vars["wind_angle"].set(round(angle_max, 2))
            self.app.set_status(f"Winding Angle{which} lowered to {angle_max:.2f}° -- the highest realistic angle for this tank/tow width (above it, each wrap would overlap the last).")

    def _validate_x_extension(self, event=None):
        # The eye can't turn around before it gets there: a negative Additional
        # X-Axis Travel is set to 0 once the user leaves the field, the way an
        # angle above the 90° threshold turns the layup into a 90° wind.
        layup_vars = self.app.layups[self.app.active_layup]
        if self._is_hoop(layup_vars):
            return
        try:
            value = float(layup_vars["x_extension"].get())
        except (tk.TclError, ValueError):
            return
        if value < 0:
            layup_vars["x_extension"].set(0.0)
            which = f" for Layup {self.app.active_layup + 1}" if len(self.app.layups) > 1 else ""
            self.app.set_status(f"Additional X-Axis Travel{which} set to 0 mm -- it can't be negative: the eye "
                                "then turns around right where the layup does.")

    def _validate_bulkhead(self, event=None):
        # Within what the dome allows: down to the polar opening's angle (the
        # whole dome; anything lower winds just the same) and up to 90° (the
        # straight section only). An emptied field is going back to auto.
        layup_vars = self.app.layups[self.app.active_layup]
        if self._is_hoop(layup_vars) or self._is_set(layup_vars["bulkhead_auto"]):
            return
        try:
            angle = float(layup_vars["bulkhead_angle"].get())
            job = self.app.build_job()
        except (winding.SettingsError, tk.TclError, ValueError):
            return
        if job.end_cap_type != "Round":
            return
        # Shown to one decimal, rounded down: still at or below the opening.
        lowest = math.floor(winding.opening_angle(job) * 10) / 10
        which = f" for Layup {self.app.active_layup + 1}" if len(self.app.layups) > 1 else ""
        if angle < lowest:
            layup_vars["bulkhead_angle"].set(lowest)
            self.app.set_status(f"Bulkhead Angle{which} raised to {lowest:.1f}° -- the polar opening's: the "
                                "layup winds the whole dome, and can't turn any closer to the axis.")
        elif angle > 90:
            layup_vars["bulkhead_angle"].set(90.0)
            self.app.set_status(f"Bulkhead Angle{which} lowered to 90° -- the straight section only.")

    def on_cap_type_change(self, *args):
        if self.app.params["end_cap_type"].get() == "Flat":
            self.tank_entries["end_cap_diameter"].config(state="disabled")
        else:
            self.tank_entries["end_cap_diameter"].config(state="normal")
        if self.layup_entries:  # (not yet while the panel is being built)
            self._apply_layup_mode()  # the Bulkhead Angle needs a dome
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
        # Colors (and the border) are set on every redraw: ttkbootstrap's theme
        # switch recolors plain Tk widgets and drops canvas borders.
        self.canvas.configure(bg=pal["canvas_bg"], highlightbackground=pal["shaft_outline"], highlightthickness=1)
        self.generate_btn.config(state="disabled")
        self._view_geom = None
        c_w, c_h = self.canvas.winfo_width(), self.canvas.winfo_height()
        # Centered on the tank's axis (see _draw_tank).
        self._draw_turn_arrows(self.canvas, TANK_ARROW_X, TANK_TOP + (c_h - TANK_TOP - TANK_BOTTOM) / 2.0, 13, 14)

        try:
            job = self.app.build_job()
        except winding.SettingsError as e:  # a field is empty / mid-edit
            self._job = None
            if c_w >= 100 and c_h >= 100:
                self.canvas.create_text(c_w / 2, c_h / 2, text=self.describe_settings_error(e),
                                        fill=pal["muted"], font=("TkDefaultFont", 10))
            self._draw_legend()
            self._cycles_field.refresh()
            self._bulkhead_field.refresh()
            self._start_x_field.refresh()
            self._zone_field.refresh()
            self._update_speed_limit(None, None)
            self._update_field_notes(None, None)
            self._update_indicator()
            return
        self._job = job
        self._sync_auto_values(job)
        layup = job.layups[self.app.active_layup]
        self._update_instant_estimates(job, layup)

        geometry_errors = winding.geometry_errors(job)
        errors = winding.validate(job)
        if not errors: self.generate_btn.config(state="normal")

        if c_w >= 100 and c_h >= 100 and job.tank_length > 0 and job.tank_diameter > 0:
            self._draw_tank(job, c_w, c_h)
            self._draw_end_view(job.tank_diameter / 2, job.eye_arm_length)
            self._draw_start_marker()
        self._draw_bulkhead_view(job)
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

        # Scale and offset: the tank as large as the canvas allows, centered.
        # Across: the tank's X-center in the middle, with a mounting rod of the
        # same length at each end (as long as the chuck side's, from X=0, within
        # limits), so the whole assembly sits centered. Down: the tank's
        # diameter plus a band above for the markers' labels (and warnings) and
        # one below for the caption. (The end view beside it shows the eye's
        # reach, so no vertical room needs keeping for that here.)
        top, bottom, side = TANK_TOP, TANK_BOTTOM, TANK_SIDE
        rod = min(max(co, 30.0), 150.0)
        half_extent = lt / 2.0 + rod
        scale = max(0.01, min((c_w / 2.0 - side) / half_extent, (c_h - top - bottom) / max(dt, 1.0)))
        ox = c_w / 2.0 + (co + lt / 2.0) * scale
        oy = top + (c_h - top - bottom) / 2.0
        self._view_geom = (scale, ox, oy)

        # The shaft through the tank, the rod sticking out at both ends.
        shaft_w = (dc if cap_type == "Round" else dt) * scale
        self.canvas.create_rectangle(ox - (co + lt + rod) * scale, oy - shaft_w / 2, ox - (co - rod) * scale,
                                     oy + shaft_w / 2, fill=pal["shaft"], outline=pal["shaft_outline"])

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
            i = self.app.active_layup
            layup = self._job.layups[i] if i < len(self._job.layups) else None
            if layup is not None and layup.hoop:
                what = "90° wind there and back" if layup.double else "90° wind over the straight section"
            else:
                what = "first cycle"
            text = f"Layup {i + 1} of {n} · {what}"
            if i < len(self._job.layups) and winding.start_sides(self._job)[i] == -1:
                text += " · from the far end"
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
            text = f"Layup {i + 1}  ·  {layup_caption(layup, '  ·  ')}"
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
        self.est["reach"].set(f"{eye_dist_min:.0f}–{eye_dist_max:.0f} mm")

        # Rotation: the turnarounds' pure rotation runs at Max Rotation Speed
        # within the failsafe. X-Speed: the selected layup's mid-tank helix, at
        # whichever limit it reaches first (winding.wind_speed).
        self._rotation_speed_deg_per_min = min(job.max_a_speed, winding.MAX_FEED)
        self._update_rotation_speed_display()
        angle_ok = 0 < layup.wind_angle < 90
        if layup.hoop:
            self._hoop_display["wind_angle"].set(f"{job.hoop_angle:.1f}" if job.bandwidth > 0 else "—")
        speed = winding.wind_speed(job, layup)
        self._xspeed_mm_s = speed.x_speed if speed else 0.0
        self._update_xspeed_display()
        self._update_speed_limit(job, speed)
        self._update_field_notes(job, layup)

        # Coverage · the fewest cycles for full coverage. Extra Rotation: the
        # pattern-alignment rotation on top of the dwell angle, split between
        # the far-end (return) turnaround -- the same fixed share every circuit
        # -- and the chuck-end turnaround, which absorbs the rest (see
        # compute_turnaround_balance_offset) -- the other way round for a
        # layup that starts from the far end. Shown for the first circuit. A
        # 90° wind lays its bands edge to edge, with no turnarounds at all.
        cycles_needed = winding.full_coverage_cycles(job.tank_diameter, layup.wind_angle, job.bandwidth, layup.pattern_number)
        gaps = False
        if layup.hoop:
            self.est["extra"].set("none")
            self.est["coverage"].set("100 % · straight section")
        elif angle_ok and layup.passes >= 1 and layup.pattern_number >= 1 and job.tank_diameter > 0 and job.bandwidth > 0:
            plan = winding.plan_layup(job, layup)
            outbound = plan.right_offset
            inbound = plan.extra_after(0, layup.pattern_number) - outbound
            far, chuck = (outbound, inbound) if winding.start_sides(job)[self.app.active_layup] == 1 else (inbound, outbound)
            self.est["extra"].set(f"{far:.1f}° / {chuck:.1f}°")
            percent = round(plan.coverage * 100)
            self.est["coverage"].set(f"{percent} %" + (f" · min {cycles_needed} cyc" if cycles_needed else ""))
            # Compared as shown (rounded): "100 %" never turns orange over a
            # sub-percent shortfall.
            gaps = percent < 100
        else:
            self.est["extra"].set("-- / --")
            self.est["coverage"].set("--")
        self._est_values["coverage"].configure(foreground=self.app.canvas_palette["warn_text"] if gaps else "")

    def _update_speed_limit(self, job, speed):
        # Tags the speed limit that sets the selected layup's pace mid-tank
        # with LIMIT, and says what that pace is in both speed fields'
        # tooltips. No tag while the settings don't define a pace, nor in the
        # rare case the MAX_FEED failsafe holds it below both limits. Colored
        # on every call, so a theme switch (which runs a redraw) is covered.
        limiting = {"rotation": "max_surface_speed", "filament": "max_filament_speed"}.get(speed.limit) if speed else None
        for key, tag in self._limit_tags.items():
            if key == limiting:
                tag.configure(fg=self.app.canvas_palette["limit_tag"], bg=self.app.root.style.colors.inputbg)
                tag.place(relx=1.0, rely=0.5, x=-7, anchor="e")
            else:
                tag.place_forget()
        if speed is None:
            for key in SPEED_LIMIT_KEYS:
                self._set_note(key)
            return
        i, n = self.app.active_layup, len(self.app.layups)
        layup = job.layups[i]
        which = f"{f'Layup {i + 1}' if n > 1 else 'the wind'} ({'90° wind' if layup.hoop else f'{layup.wind_angle:g}°'})"
        Which = which[:1].upper() + which[1:]
        surface = math.radians(speed.rotation / 60.0) * job.tank_diameter / 2
        pace = (f"Mid-tank the carriage runs at {speed.x_speed:.0f} mm/s, the fiber at {speed.fiber:.0f} mm/s "
                f"and the tank surface at {surface:.0f} mm/s.")
        for key in SPEED_LIMIT_KEYS:
            if key == limiting:
                note = f"LIMIT: this sets the pace of {which}. {pace}"
            elif limiting is None:
                note = f"{Which} is held below both speed limits by the F{winding.MAX_FEED:.0f} failsafe. {pace}"
            else:
                note = f"{Which} stays below this: {FIELD_LABELS[limiting].split(' (')[0]} limits it first."
            self._set_note(key, note)

    def _update_sim_estimates(self):
        # Figures that need the full simulation (time, tow, masses, strength).
        # While a newer calculation is running they keep showing the previous
        # result (the "Calculating..." indicator flags that); they're blanked
        # only when the current settings can't be simulated at all.
        self.refresh_estimate()

    def simulation_for(self, job):
        """The finished simulation of exactly `job`, if there is one."""
        return self._result[1] if self._result is not None and self._result[0] == job else None

    def refresh_estimate(self):
        """Refreshes every simulation-based figure -- time, tow, masses and the
        safety factor, which also depend on the Strength & Materials inputs --
        and the Strength & Materials window, if it's open."""
        self.estimate = None
        sf_label = self._est_values["safety_factor"]
        sf_label.configure(foreground="")
        if self._sim_blocked or self._result is None:
            for key in ("time", "tow", "resin", "safety_factor", "layup_time", "layup_tow", "layup_resin"):
                self.est[key].set("--")
            self._refresh_strength_dialog()
            return
        job, result = self._result
        try:
            material = self.app.build_material()
        except winding.SettingsError:
            material = None
        if material is not None and strength.errors(material):
            material = None
        cycles = (self._job or job).total_cycles

        def tow_text(tow_mm):
            meters = tow_mm / 1000.0
            length = f"{meters:.0f} m" if meters >= 100 else f"{meters:.1f} m"
            if material is None:
                return f"{length} · -- kg"
            return f"{length} · {strength.masses(tow_mm, material)[0]:.2f} kg"

        def resin_text(tow_mm):
            return "-- kg" if material is None else f"{strength.masses(tow_mm, material)[1]:.2f} kg"

        self.est["time"].set(f"{format_hms(result.total_time)} · {cycles} cyc")
        self.est["tow"].set(tow_text(result.total_tow))
        self.est["resin"].set(resin_text(result.total_tow))
        i = self.app.active_layup
        if i < len(result.layups):
            lr = result.layups[i]
            # Average time per cycle -- individual cycles can vary a little (the
            # extra turnaround rotation that keeps the pattern aligned differs
            # slightly cycle to cycle).
            per_cycle = lr.time / max(1, job.layups[i].passes)
            self.est["layup_time"].set(f"{format_hms(lr.time)} · {short_time(per_cycle)}/cyc")
            self.est["layup_tow"].set(tow_text(lr.tow))
            self.est["layup_resin"].set(resin_text(lr.tow))
        else:
            # A layup that was just added isn't part of the cached result yet.
            for key in ("layup_time", "layup_tow", "layup_resin"): self.est[key].set("…")
        if material is None:
            self.est["safety_factor"].set("--")
        else:
            self.estimate = (job, material, strength.estimate(job, result, material))
            e = self.estimate[2]
            if e.safety_factor is None:
                self.est["safety_factor"].set("--")
            else:
                self.est["safety_factor"].set(f"{e.safety_factor:.2f} @ {material.pressure:g} bar")
                if e.safety_factor < 1:
                    sf_label.configure(foreground=self.app.canvas_palette["warn_text"])
        self._refresh_strength_dialog()

    def _refresh_strength_dialog(self):
        dialog = self.app.strength_dialog
        if dialog is not None and dialog.exists():
            dialog.refresh()

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
        if self._partial_changed:
            # One redraw for all the layups that came in since the last poll.
            self._partial_changed = False
            self._redraw_strands()
        self._update_indicator()
        self.app.root.after(50, self._poll_calc_queue)

    def _apply_partial_runs(self, job, update):
        # A layup's first cycle from the simulation still running.
        li, drawn = update
        if self._partial is None or self._partial[0] != job:
            self._partial = (job, {})
        self._partial[1][li] = drawn
        if job == self._job and self.partial_point is None and (self.show_all_var.get()
                                                                or li == self.app.active_layup):
            self._partial_changed = True

    def _apply_calc_result(self, job, result, error):
        if error is not None:
            print(f"Calc error: {error}")
            return
        result, self._runs = result
        self._partial, self._partial_changed = None, False
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

    def location_for(self, job, point):
        """Where `point` is in `job` (a winding.Location), from the last finished
        progress calculation -- or None if it isn't of exactly these."""
        if self._progress is None or self._progress[0] != (job, point) or isinstance(self._progress[1], Exception):
            return None
        return self._progress[1].location

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
        # The strands in both views: the tank preview and the Bulkhead view.
        self._redraw_tank_strands()
        self._draw_bulkhead_strands()

    def _redraw_tank_strands(self):
        # Draws the wound strands: normally the selected layup's first cycle --
        # or, with "Show All Layups", every layup's first cycle -- and while the
        # partial page is open, the tank as wound up to the continue point.
        # Only front-facing path segments are drawn; which portions count as
        # "front" depends on the view's azimuth (self._tank_spin), so this alone
        # is re-run (cheaply) on every rotate-animation frame, while the
        # tank/shaft/end-view stay untouched. Only results calculated from the
        # exact settings on screen are ever drawn -- while the simulation still
        # runs, the layups it has got through so far.
        self.canvas.delete("strand_path")
        if self.partial_point is not None:
            self._draw_progress()
        elif self._view_geom:
            runs = None
            if self._result is not None and self._result[0] == self._job:
                runs = self._runs
            elif self._partial is not None and self._partial[0] == self._job:
                runs = self._partial[1]
            if runs is not None:
                job = self._job
                indices = range(len(job.layups)) if self.show_all_var.get() else [self.app.active_layup]
                # In winding order, so later layups lie on top of earlier ones
                # like they do on the real tank.
                for li in indices:
                    if li not in runs:
                        continue
                    if job.layups[li].hoop:
                        first, last = job.hoop_span
                        self._draw_band(li, first - job.bandwidth / 2, last + job.bandwidth / 2)
                    self._draw_runs(li, runs[li][self._detail()])
        # Strand paths draw on top of everything else drawn synchronously (tank,
        # warnings); keep the markers, caption and calc indicator above that.
        for tag in ("start_marker", "continue_marker", "eye", "caption", "calc_indicator", "turn_arrows"):
            self.canvas.tag_raise(tag)

    def _draw_runs(self, layup_index, runs):
        # `runs` as _preview_runs() prepared them: (x, r, A in radians) points.
        scale, ox, oy = self._view_geom
        pal = self.app.canvas_palette
        color, dash = theme.layup_style(pal, layup_index)
        # The band's width on screen, rounded up: bands laid edge to edge (full
        # coverage) then close up instead of showing slivers of tank.
        style = dict(fill=color, width=max(1, math.ceil(self._job.bandwidth * scale)), capstyle=tk.ROUND,
                     dash=dash or "", tags="strand_path")
        az = math.radians(self._tank_spin.azimuth)
        sin, cos, create_line = math.sin, math.cos, self.canvas.create_line
        for run in runs:
            line = []
            for x, r, a in run:
                eff = a - az
                if sin(eff) >= 0:
                    line.append(ox - x * scale)
                    line.append(oy - r * cos(eff) * scale)
                else:
                    if len(line) >= 4:
                        create_line(*line, **style)
                    line = []
            if len(line) >= 4:
                create_line(*line, **style)

    def _detail(self):
        # Which of _preview_runs' levels of detail to draw: the coarse one while
        # the view spins (redrawn every frame), the fine one once it rests.
        return 1 if self._tank_spin.moving else 0

    def _draw_band(self, layup_index, x_from, x_to):
        # A 90° wind: its bands lie edge to edge, so it's drawn as the solid band
        # it makes over the straight section (from x_from to x_to).
        if x_from == x_to:
            return
        scale, ox, oy = self._view_geom
        color, _ = theme.layup_style(self.app.canvas_palette, layup_index)
        rt = self._job.tank_diameter / 2
        self.canvas.create_rectangle(ox - x_from * scale, oy - rt * scale, ox - x_to * scale, oy + rt * scale,
                                     fill=color, outline="", tags="strand_path")

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
        job = self._job
        sides = winding.start_sides(job)
        for li in range(progress.location.point.layup + 1):
            if li in progress.covered and self._tank_outline:
                color, _ = theme.layup_style(pal, li)
                self.canvas.create_polygon(self._tank_outline, fill=color, outline=pal["tank_outline"], width=2,
                                           tags="strand_path")
                continue
            if job.layups[li].hoop:
                # The band from where the pass began -- to the eye while it's
                # wound (a double one's way back runs over the finished band).
                first, last = job.hoop_span
                lo, hi = first - job.bandwidth / 2, last + job.bandwidth / 2
                start, end = (lo, hi) if sides[li] == 1 else (hi, lo)
                point = progress.location.point
                if li == point.layup and point.cycle == 0:
                    end = min(hi, max(lo, progress.location.x))
                self._draw_band(li, start, end)
            if li in progress.runs:
                self._draw_runs(li, progress.runs[li][self._detail()])
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
        # The arm runs down to its base, or to the canvas edge when that's lower.
        base_y = min(oy + (winding.Y_REFERENCE - job.eye_arm_length) * scale, self.canvas.winfo_height() - 2)
        half_w = max(3.0, job.eye_width / 2 * scale)
        self.canvas.create_line(px, base_y, px, eye_y, fill=pal["reach"], width=3, tags="eye")
        self.canvas.create_rectangle(px - half_w, eye_y - 4, px + half_w, eye_y + 4, fill=pal["marker"],
                                     outline=pal["marker_halo"], tags="eye")

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
        # The tank end-on, with how close to and how far from its axis the eye
        # can get (Y at its travel's ends). The axis sits low, as the eye only
        # ever works above it.
        pal = self.app.canvas_palette
        c = self.end_canvas
        c.configure(bg=pal["canvas_bg"], highlightbackground=pal["shaft_outline"], highlightthickness=1)
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 40 or h < 40:
            return
        c.create_text(8, 6, text="Eye Reach", anchor="nw", fill=pal["muted"], font=VIEW_TITLE_FONT)
        eye_dist_min, eye_dist_max = winding.eye_reach(arm)
        top, bottom = 24, 8
        reach = max(eye_dist_max, rt, 1.0)
        scale2 = max(0.001, min((w / 2.0 - 12) / max(rt, 1.0), (h - top - bottom) / (reach + rt)))
        cx, cy = w / 2.0, top + reach * scale2
        r_px = rt * scale2
        c.create_oval(cx - r_px, cy - r_px, cx + r_px, cy + r_px, fill=pal["tank"], outline=pal["tank_outline"], width=2)
        for dist, label in ((eye_dist_min, "Max"), (eye_dist_max, "Min")):
            y = cy - dist * scale2
            c.create_line(6, y, w - 6, y, fill=pal["reach"], dash=(4, 4))
            c.create_text(w - 8, y - 2, text=label, anchor="se", fill=pal["reach"], font=VIEW_LABEL_FONT)

    # --- Bulkhead view: the selected layup's turnaround at one dome, up close ---

    def _draw_bulkhead_view(self, job):
        # The far end's dome in profile -- the semicircle the Bulkhead Angle is
        # measured on -- with a little of the straight section and the boss,
        # the selected layup's angle as a line from the semicircle's center,
        # where it turns around, and (drawn by _draw_bulkhead_strands) its
        # first cycle's strands over the dome. +X runs to the left, as in the
        # tank preview beside it.
        pal = self.app.canvas_palette
        c = self.bulkhead_canvas
        c.configure(bg=pal["canvas_bg"], highlightbackground=pal["shaft_outline"], highlightthickness=1)
        c.delete("all")
        self._bulk_geom = self._bulk_outline = None
        w, h = c.winfo_width(), c.winfo_height()
        if w < 60 or h < 80:
            return
        n, i = len(job.layups), min(self.app.active_layup, len(job.layups) - 1)
        title = "Bulkhead" + (f" · Layup {i + 1}" if n > 1 else "")
        title_item = c.create_text(8, 6, text=title, anchor="nw", fill=pal["muted"], font=VIEW_TITLE_FONT)
        self._draw_turn_arrows(c, BULK_ARROW_X, h / 2, 11, 11)
        rt, rc, dome = job.tank_diameter / 2, job.end_cap_diameter / 2, job.dome_length
        if rt <= 0 or job.straight_length < 0:
            return
        layup = job.layups[i]
        round_caps = job.end_cap_type == "Round" and dome > 0
        # Room at the foot for the caption: a second line for a 90° wind or
        # flat end caps.
        caption_lines = 2 if layup.hoop or not round_caps else 1
        top, bottom, left = 26, 10 + 15 * caption_lines, 2 * BULK_ARROW_X + 2  # (clear of the turn arrows)
        # Room beyond the dome for a stub of the boss -- and for the eye, where
        # its Additional X-Axis Travel takes it past the tank's end.
        beyond = 0.0
        if round_caps and not layup.hoop:
            beyond = winding.bulkhead_cover(job, layup) + max(0.0, layup.x_extension) - dome
        boss = max(BULKHEAD_BOSS * rt, 1.15 * beyond) if round_caps else 0.0
        span = boss + dome + BULKHEAD_STUB * rt
        scale = max(0.001, min((w - left) / span, (h - top - bottom) / (2 * rt)))
        jx = left + (boss + dome) * scale        # where the straight section ends (the semicircle's center)
        cy = top + (h - top - bottom) / 2.0      # the tank axis
        self._bulk_geom = (scale, jx, cy)
        # The axis, the boss, and the tank: the straight section running off to
        # the right, the dome's profile around the center.
        c.create_line(0, cy, w, cy, fill=pal["shaft_outline"], dash=(6, 3, 2, 3), tags="bulk_base")
        if round_caps:
            bx = jx - dome * scale
            c.create_rectangle(bx - boss * scale, cy - rc * scale, bx, cy + rc * scale, fill=pal["shaft"],
                               outline=pal["shaft_outline"], tags="bulk_base")
        steps = 48
        # The straight section runs off the view's right edge -- unless the
        # tank is so short that its other end shows there too.
        cyl_start = job.chuck_offset + dome
        cyl_end = job.chuck_offset + job.tank_length - dome
        x_edge = cyl_end - (w + 2 - jx) / scale  # machine X at the right edge
        upper = []
        if x_edge >= cyl_start:
            upper.append((w + 2, cy - rt * scale))
        else:
            radius, x_from = winding._tank_geometry(job).radius, max(job.chuck_offset, x_edge)
            for k in range(steps + 1):
                x = x_from + (cyl_start - x_from) * k / steps
                upper.append((jx - (x - cyl_end) * scale, cy - radius(x) * scale))
        for k in range(steps + 1):
            x = dome * k / steps
            r = math.sqrt(max(rt * rt - x * x, rc * rc)) if round_caps else rt
            upper.append((jx - x * scale, cy - r * scale))
        lower = [(px, 2 * cy - py) for px, py in upper]
        self._bulk_outline = upper + lower[::-1]
        c.create_polygon(self._bulk_outline, fill=pal["tank"], outline=pal["tank_outline"], width=2, tags="bulk_base")
        for share, color in ((0.28, pal["highlight"]), (0.82, pal["shadow"])):
            c.create_line(*[v for (px, py) in upper for v in (px, py + (2 * (cy - py)) * share)],
                          fill=color, width=2, smooth=True, tags="bulk_base")
        if not round_caps:
            self._bulk_caption(c, w, h, "Flat end caps: no dome,", "every layup turns at the tank's end.")
            return
        # The selected layup's Bulkhead Angle: from the semicircle's center to
        # where the layup turns around on the dome, the turning circle seen
        # edge-on through that point, and where its eye turns back beyond it.
        opening = winding.opening_angle(job)
        if layup.hoop:
            angle, cover = 90.0, 0.0
        else:
            angle, cover = max(layup.bulkhead_angle, opening), winding.bulkhead_cover(job, layup)
        ink, halo = pal["marker"], pal["marker_halo"]
        # The polar opening's angle, faintly: the whole dome is wound from it down.
        c.create_line(jx, cy, jx - rt * math.cos(math.radians(opening)) * scale,
                      cy - rt * math.sin(math.radians(opening)) * scale, fill=pal["muted"], dash=(2, 3),
                      tags="bulk_overlay")
        px = jx - rt * math.cos(math.radians(angle)) * scale
        py = cy - rt * math.sin(math.radians(angle)) * scale
        r_turn = cy - py
        tx = jx - cover * scale
        for color, width in ((halo, 4), (ink, 2)):
            c.create_line(tx, cy - r_turn, tx, cy + r_turn, fill=color, width=width,
                          dash=(5, 3) if color == ink else "", tags="bulk_overlay")
        for color, width in ((halo, 4), (ink, 2)):
            c.create_line(jx, cy, px, py, fill=color, width=width, capstyle=tk.ROUND, tags="bulk_overlay")
        arc = max(14.0, min(0.34 * rt * scale, 40.0))
        c.create_arc(jx - arc, cy - arc, jx + arc, cy + arc, start=180.0 - angle, extent=angle, style=tk.ARC,
                     outline=ink, width=1.5, tags="bulk_overlay")
        for x, y, size in ((jx, cy, 3), (px, py, 4)):
            c.create_oval(x - size, y - size, x + size, y + size, fill=ink, outline=halo, tags="bulk_overlay")
        # Its value in the title row (never over the drawing), tagged AUTO
        # like the field it comes from. Where the title would run into it, the
        # title drops the layup's number (the caption under the tank has it).
        auto = layup.bulkhead_auto and not layup.hoop
        value = c.create_text(w - 8, 5, text=f"{angle:.1f}°", anchor="ne", fill=ink, font=VIEW_VALUE_FONT,
                              tags="bulk_caption")
        left_edge = c.bbox(value)[0]
        if auto:
            tag = c.create_text(left_edge - 5, 7, text="AUTO", anchor="ne", fill=pal["auto_text"],
                                font=("TkDefaultFont", 7, "bold"), tags="bulk_caption")
            left_edge = c.bbox(tag)[0]
        if c.bbox(title_item)[2] > left_edge - 6:
            c.itemconfigure(title_item, text="Bulkhead")
        if not layup.hoop and layup.x_extension > 0:
            # Where the eye turns back (beyond the tank's end too, if it goes there).
            ex = jx - (cover + layup.x_extension) * scale
            ey = cy - max(r_turn, rc * scale) - 6
            c.create_polygon(ex - 5, ey - 7, ex + 5, ey - 7, ex, ey, fill=pal["reach"], outline="", tags="bulk_overlay")
        # What it means, below.
        if layup.hoop:
            lines = ("90° wind: straight section only", "-- never on the dome")
        else:
            lines = ("Winds the whole dome" if cover >= dome else f"Dome: {cover:.0f} of {dome:.0f} mm wound",)
        self._bulk_caption(c, w, h, *lines)
        c.itemconfigure("bulk_overlay", state="hidden" if self._bulk_single() else "normal")

    # --- Turn arrows: ▲ ▼ drawn into a view, at its left edge ---

    def _bind_turn_arrows(self, c, spinner):
        # A view's turn arrows are canvas items (see _draw_turn_arrows),
        # redrawn with the view; their tag bindings, made once here, carry over
        # to every redraw. Holding one turns the view (`spinner`); letting go
        # -- anywhere -- stops it.
        pal = lambda key: self.app.canvas_palette[key]  # the current theme's, at the time
        for tag, direction in (("turn_up", -1), ("turn_down", 1)):
            c.tag_bind(tag, "<ButtonPress-1>", lambda e, d=direction: spinner.hold(d))
            c.tag_bind(tag, "<Enter>", lambda e, t=tag: (c.itemconfigure(t, fill=pal("tank")),
                                                         c.configure(cursor="hand2")))
            c.tag_bind(tag, "<Leave>", lambda e, t=tag: (c.itemconfigure(t, fill=pal("muted")),
                                                         c.configure(cursor=""), spinner.hold(0)))
        c.bind("<ButtonRelease-1>", lambda e: spinner.hold(0), add="+")

    def _draw_turn_arrows(self, c, x, y, size, gap):
        # ▲ and ▼ around (x, y) -- a view's left edge, half way down -- in the
        # muted color, highlighted while hovered (see _bind_turn_arrows).
        muted = self.app.canvas_palette["muted"]
        for tag, glyph, dy in (("turn_up", "▲", -gap), ("turn_down", "▼", gap)):
            c.create_text(x, y + dy, text=glyph, fill=muted, font=("TkDefaultFont", size), tags=("turn_arrows", tag))

    def _bulk_single(self):
        # Whether the Bulkhead view shows a single circuit: "View Single" --
        # except while continuing a wind, when it shows what's wound so far.
        return self.bulk_single_var.get() and self.partial_point is None

    def _bulk_caption(self, c, w, h, *lines):
        # One or two lines of small print at the view's foot.
        pal = self.app.canvas_palette
        for k, text in enumerate(reversed(lines)):
            c.create_text(8, h - 7 - 15 * k, text=text, anchor="sw", fill=pal["muted"], font=VIEW_LABEL_FONT,
                          tags="bulk_caption")

    def _draw_bulkhead_strands(self):
        # The strands over the dome, the way the tank preview draws them (the
        # same layups), under the Bulkhead Angle's construction, seen from the
        # view's own angle (its arrows turn it, the tank preview's don't).
        # With "View Single", only the first circuit: one strand coming onto
        # the dome, going around it -- thin and dashed where it runs behind --
        # and back off. Re-run on every rotation frame: _preview_runs() kept
        # only what lies over this dome, so it's cheap.
        c = self.bulkhead_canvas
        c.delete("bulk_strand")
        # Single circuits only exist for a layup's first cycle, not for a
        # continued wind's progress.
        self._bulk_single_check.state(["disabled"] if self.partial_point is not None else ["!disabled"])
        c.itemconfigure("bulk_overlay", state="hidden" if self._bulk_single() else "normal")
        if self._bulk_geom is None or self._job is None:
            return
        job, pal = self._job, self.app.canvas_palette
        scale, jx, cy = self._bulk_geom
        cyl_end = job.chuck_offset + job.tank_length - job.dome_length
        width = max(1, math.ceil(job.bandwidth * scale))
        az = math.radians(self._bulk_spin.azimuth)
        sin, cos, create_line = math.sin, math.cos, c.create_line
        single = self._bulk_single()

        def draw(li, runs, hidden=False):
            color, dash = theme.layup_style(pal, li)
            style = dict(fill=color, width=width, capstyle=tk.ROUND, dash=dash or "", tags="bulk_strand")
            back_style = dict(fill=color, width=1, dash=(3, 3), tags="bulk_strand")
            for run in runs:
                front, back = [], []
                for x, r, a in run:
                    eff = a - az
                    px, py = jx - (x - cyl_end) * scale, cy - r * cos(eff) * scale
                    if sin(eff) >= 0:
                        front += (px, py)
                        if hidden and back:
                            # Join up where it comes round to the front.
                            back += (px, py)
                            if len(back) >= 4:
                                create_line(*back, **back_style)
                            back = []
                    else:
                        if hidden:
                            if not back and front:
                                back += front[-2:]
                            back += (px, py)
                        if len(front) >= 4:
                            create_line(*front, **style)
                        front = []
                if len(front) >= 4:
                    create_line(*front, **style)
                if hidden and len(back) >= 4:
                    create_line(*back, **back_style)

        def band(li):
            # A 90° wind covers the straight section up to the dome.
            color, _ = theme.layup_style(pal, li)
            rt = job.tank_diameter / 2
            c.create_rectangle(jx, cy - rt * scale, c.winfo_width() + 2, cy + rt * scale, fill=color, outline="",
                               tags="bulk_strand")

        if self.partial_point is not None:
            if self._progress is None or self._progress[0] != self._progress_key():
                return
            progress = self._progress[1]
            if isinstance(progress, Exception):
                return
            for li in range(progress.location.point.layup + 1):
                if li in progress.covered and self._bulk_outline:
                    color, _ = theme.layup_style(pal, li)
                    c.create_polygon(self._bulk_outline, fill=color, outline=pal["tank_outline"], width=2,
                                     tags="bulk_strand")
                    continue
                if job.layups[li].hoop:
                    band(li)
                if li in progress.runs:
                    draw(li, progress.runs[li][2])
        else:
            runs = None
            if self._result is not None and self._result[0] == job:
                runs = self._runs
            elif self._partial is not None and self._partial[0] == job:
                runs = self._partial[1]
            if runs is None:
                return
            indices = range(len(job.layups)) if self.show_all_var.get() else [self.app.active_layup]
            for li in indices:
                if li not in runs:
                    continue
                if job.layups[li].hoop:
                    band(li)
                draw(li, runs[li][3] if single else runs[li][2], hidden=single)
        # The Bulkhead Angle's construction (its line and arc, the turning
        # circle, the opening's angle, the eye's ▼) goes over every circuit --
        # and away for a single one, which it would only clutter.
        c.itemconfigure("bulk_overlay", state="hidden" if single else "normal")
        c.tag_raise("bulk_overlay")
        c.tag_raise("bulk_caption")
        c.tag_raise("turn_arrows")

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
