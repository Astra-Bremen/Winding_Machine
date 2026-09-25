"""Winding math and G-code generation for the CFRP Winder.

Pure Python with no Tkinter dependency, so everything that decides where the
machine moves can be exercised headless (see test_winding.py). The G-code
writer (`write_gcode`) and the Settings Preview's simulation (`simulate`) are
both driven by the exact same motion generator (`iter_program`), so the preview
and estimates can never drift from what actually gets written to the file.

A winding program is a sequence of *layups*. Each layup winds its own number of
cycles with its own winding angle, pattern number and turnaround dwell, one
after the other on the same tank; the tank geometry, machine settings, tow
width and trajectory optimization are shared by the whole program.
"""
import itertools
import math
from dataclasses import dataclass, field, fields, replace
from typing import NamedTuple

# --- HARDCODED MACHINE LIMITS / GEOMETRY ---
MAX_X = 5250.0        # carriage's physical X-axis travel limit (mm)
Y_REFERENCE = 550.0   # eye distance from the tank centerline at Y=0, before subtracting the eye arm (mm)
Y_TRAVEL = 180.0      # Y-axis travel; commanded Y is clamped to [0, Y_TRAVEL] (mm)
STEP_SIZE = 5.0       # X distance covered by one traversal G-code move (mm)
MIN_MOVE_TIME = 0.1   # lowest allowed Max Move Time (s); shorter moves only load the controller

# --- VISUALIZATION-ONLY RENDERING RESOLUTION ---
# Caps how many degrees of rotation may separate two consecutive strand-path
# points recorded for the 3D previews. Purely a rendering knob (see simulate) --
# it has no effect on the G-code itself.
RENDER_MAX_DEG_PER_STEP = 15.0


def _coerce_fields(obj):
    # Settings arrive from Tk variables, parsed G-code headers and tests alike;
    # normalizing every numeric field to its declared type keeps comparisons,
    # hashing and the written settings header ("50.0", not "50") consistent no
    # matter where the values came from.
    for f in fields(obj):
        value = getattr(obj, f.name)
        if f.type in (float, "float"):
            object.__setattr__(obj, f.name, float(value))
        elif f.type in (int, "int"):
            object.__setattr__(obj, f.name, int(value))
        elif f.type in (bool, "bool"):
            object.__setattr__(obj, f.name, bool(value))


@dataclass(frozen=True)
class Layup:
    """One switchable winding pattern: a number of cycles wound at one angle."""
    passes: int = 10                 # number of cycles
    pattern_number: int = 3          # strands (circuits) per cycle
    wind_angle: float = 45.0         # degrees from the mandrel axis
    turnaround_angle: float = 270.0  # minimum dwell rotation at each turnaround (deg)
    # True: `passes` follows the cycles needed for 100 % coverage, recomputed by
    # the WindingJob whenever anything it depends on changes. False: `passes`
    # is used exactly as given.
    auto_cycles: bool = False

    def __post_init__(self):
        _coerce_fields(self)


@dataclass(frozen=True)
class WindingJob:
    """Every setting that affects the generated G-code. Frozen and hashable, so
    a job doubles as the cache key for the preview's background simulation."""
    chuck_offset: float = 50.0
    home_before_wind: bool = True
    # Where the wind starts (machine X, mm) when homing first: the machine moves
    # the eye there after G28 and pauses so the fiber can be attached, then
    # winds toward +X from there. Auto: where the chuck-side dome ends and the
    # tank turns straight. Without homing the wind starts at the tank's end.
    start_x: float = 0.0
    start_x_auto: bool = True
    eye_arm_length: float = 370.0
    eye_width: float = 20.0
    min_spacing: float = 10.0
    max_surface_speed: float = 220.0
    # Longest time (s) any single G-code move may take; longer ones are split
    # into equal pieces. Klipper can't interrupt a move it has queued, so this
    # bounds how long the machine keeps going after a pause (see iter_program).
    max_move_time: float = 0.5
    end_cap_type: str = "Round"
    tank_length: float = 1000.0
    tank_diameter: float = 200.0
    end_cap_diameter: float = 50.0
    bandwidth: float = 5.0
    # Length (mm) at each tank end over which the turnaround rotation is spread
    # while the carriage runs out to the end and back, instead of one pure
    # rotation at the end. 0 = pure rotation at the end. See compute_dwell_blend.
    turnaround_zone: float = 80.0
    optimize_trajectory: bool = False
    # PAUSE between layups, so the fiber can be checked after every pattern change.
    pause_after_layup: bool = True
    layups: tuple = field(default=(Layup(),))

    def __post_init__(self):
        _coerce_fields(self)
        # Auto-cycle layups get their cycle count resolved here, once, against
        # this job's own tank and tow -- so every consumer (motion, estimates,
        # settings header, cache key) sees the same, never-stale number.
        object.__setattr__(self, "layups", tuple(self._resolve_cycles(layup) for layup in self.layups))
        # Same for an auto start position.
        if self.start_x_auto:
            object.__setattr__(self, "start_x", self.chuck_offset + self.dome_length)

    @property
    def wind_start_x(self):
        """Machine X where the wind begins: the Start Wind at X setting after
        homing, otherwise the tank's chuck-side end (the machine then can't be
        assumed to know where anything else is)."""
        return self.start_x if self.home_before_wind else self.chuck_offset

    @property
    def start_x_range(self):
        # From the tank's chuck-side end up to where the first pass still has
        # room for the far end's full turnaround zone (or Optimize Trajectory's
        # easing) before it turns around.
        x_end = self.chuck_offset + self.tank_length
        return self.chuck_offset, x_end - STEP_SIZE * max(self.turnaround_zone_steps, 3)

    def _resolve_cycles(self, layup):
        if not layup.auto_cycles:
            return layup
        cycles = full_coverage_cycles(self.tank_diameter, layup.wind_angle, self.bandwidth, layup.pattern_number)
        # Not computable (e.g. an angle outside 0-90 deg): leave the layup as it
        # is; geometry_errors() reports the actual problem.
        return layup if cycles is None or cycles == layup.passes else replace(layup, passes=cycles)

    @property
    def dome_length(self):
        # Axial length of each spherical dome ("ld"); zero for flat end caps.
        if self.end_cap_type != "Round":
            return 0.0
        rt, rc = self.tank_diameter / 2, self.end_cap_diameter / 2
        return min(math.sqrt(max(0, rt**2 - rc**2)), self.tank_length / 2)

    @property
    def max_a_speed(self):
        # Max Rotation Speed as an A-axis rate (deg/min) for this tank diameter.
        return surface_speed_to_deg_per_min(self.max_surface_speed, self.tank_diameter)

    @property
    def total_cycles(self):
        return sum(layup.passes for layup in self.layups)

    @property
    def turnaround_zone_steps(self):
        # The zone is covered by whole traversal steps, so it rounds up to a
        # multiple of STEP_SIZE.
        return max(0, math.ceil(self.turnaround_zone / STEP_SIZE - 1e-9))


def _traversal_step_count(length):
    # Number of moves traversal_steps() makes over `length` (the last may be short).
    n_full = int(length // STEP_SIZE)
    return n_full + (1 if length - n_full * STEP_SIZE > 1e-9 else 0)


GLOBAL_KEYS = tuple(f.name for f in fields(WindingJob) if f.name != "layups")
LAYUP_KEYS = tuple(f.name for f in fields(Layup))

# Settings the WindingJob can compute itself: value key -> the flag that, while
# set, makes it do so (the value given is then ignored).
AUTO_FLAGS = {"passes": "auto_cycles", "start_x": "start_x_auto"}

# How files written before a setting existed were actually wound, where that
# differs from the setting's current default: restoring such a file uses these
# values (or calls them with the file's settings header), so it regenerates
# the motion it was made with. (Max Move Time needs no entry: splitting moves
# never changes the path.)
LEGACY_VALUES = {
    "turnaround_zone": 0.0,
    "pause_after_layup": False,
    # Those winds started at the tank's chuck-side end.
    "start_x_auto": False,
    "start_x": lambda settings: float(settings["chuck_offset"]),
}


class SettingsError(ValueError):
    """A setting's input doesn't currently hold a valid number (e.g. an entry
    field left empty mid-edit). `layup_index` is None for global settings."""
    def __init__(self, key, layup_index=None):
        super().__init__(key)
        self.key = key
        self.layup_index = layup_index


# --- Validation ---

def _layup_prefix(job, index):
    return f"Layup {index + 1}: " if len(job.layups) > 1 else ""


def geometry_errors(job):
    """Problems that make the winding math itself undefined. While any exist,
    neither the preview simulation nor G-code generation can run."""
    errors = []
    if job.tank_length <= 0 or job.tank_diameter <= 0:
        errors.append("Tank Length and Tank Diameter must both be greater than 0.")
    if job.bandwidth <= 0:
        errors.append("Bandwidth / Tow Width must be greater than 0.")
    if job.max_surface_speed <= 0:
        errors.append("Max Rotation Speed must be greater than 0.")
    elif job.tank_diameter > 0 and job.max_a_speed < 1.0:
        # G-code feed rates are whole numbers of at least 1 (per minute), which
        # would already rotate faster than a limit this low.
        errors.append("Max Rotation Speed is too low for this tank diameter.")
    if job.max_move_time < MIN_MOVE_TIME:
        errors.append(f"Max Move Time must be at least {MIN_MOVE_TIME:g} s.")
    if job.home_before_wind and job.tank_length > 0:
        lo, hi = job.start_x_range
        if not lo - 1e-9 <= job.start_x <= hi + 1e-9:
            errors.append(f"Start Wind at X must be between {lo:.1f} and {hi:.1f} mm "
                          "(on the tank, clear of the far end's turnaround).")
    if job.turnaround_zone < 0:
        errors.append("Turnaround Zone can't be negative.")
    elif job.tank_length > 0 and 2 * job.turnaround_zone_steps > _traversal_step_count(job.tank_length):
        # The zones at both ends would overlap, and part of the turnaround
        # rotation would have nowhere to go.
        errors.append("Turnaround Zone must be at most half the Tank Length.")
    if not job.layups:
        errors.append("At least one layup is required.")
    for i, layup in enumerate(job.layups):
        prefix = _layup_prefix(job, i)
        if layup.passes < 1 or layup.pattern_number < 1:
            errors.append(f"{prefix}Number of Cycles and Pattern Number must both be at least 1.")
        if not 0 < layup.wind_angle < 90:
            errors.append(f"{prefix}Winding Angle must be between 0° and 90°.")
        if layup.turnaround_angle < 0:
            errors.append(f"{prefix}Turnaround / Dwell Angle can't be negative.")
    return errors


def validate(job):
    """Every problem that blocks G-code generation: the geometry errors plus the
    machine's own physical limits."""
    errors = geometry_errors(job)
    x_max = job.chuck_offset + job.tank_length
    if x_max > MAX_X:
        errors.append(f"X-Axis would reach {x_max:.0f}mm, exceeding the carriage's travel limit of {MAX_X:.0f}mm. "
                      "Reduce Tank Length or Chuck X-Offset.")
    return errors


# --- Unit conversions / machine geometry ---

def surface_speed_to_deg_per_min(mm_per_s, diameter):
    # Converts a surface (tangential) speed in mm/s into the equivalent A-axis
    # rotation rate in deg/min for the given tank diameter, so the user enters a
    # physically meaningful, diameter-independent speed instead of a raw rotation
    # rate. All downstream feed-rate/throttling math still works in deg/min.
    if diameter <= 0: return 0.0
    circumference = math.pi * diameter
    return mm_per_s * (360.0 / circumference) * 60.0


def eye_reach(eye_arm_length):
    """(closest, farthest) distance of the eye tip from the tank centerline
    across the Y-axis travel, in mm."""
    return Y_REFERENCE - eye_arm_length - Y_TRAVEL, Y_REFERENCE - eye_arm_length


def calc_R(x, x_start, x_end, r_tank, r_cap, l_dome, cap_type):
    if cap_type == "Flat": return r_tank
    cyl_start, cyl_end = x_start + l_dome, x_end - l_dome
    if x < cyl_start:
        return math.sqrt(max(r_tank**2 - (cyl_start - x)**2, r_cap**2))
    elif x > cyl_end:
        return math.sqrt(max(r_tank**2 - (x - cyl_end)**2, r_cap**2))
    return r_tank


def calc_R_eye_footprint(x, x_start, x_end, r_tank, r_cap, l_dome, cap_type, eye_width):
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
    r_max = calc_R(x, x_start, x_end, r_tank, r_cap, l_dome, cap_type)
    for i in range(n_samples + 1):
        xi = x_lo + (x_hi - x_lo) * i / n_samples
        r_max = max(r_max, calc_R(xi, x_start, x_end, r_tank, r_cap, l_dome, cap_type))
    return r_max


def calc_move(x0, y0, a0, x1, y1, a1, max_a_speed, r_next):
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


# --- Pattern / turnaround math ---

def compute_pattern_skip(base, pattern_number, shift):
    # The pattern "skip" k: every circuit's total rotation is rounded UP to
    # k * (360 / pattern_number), i.e. onto the nearest pattern slot at or past
    # the minimum rotation the traverses and dwell minimums need (`base`) --
    # ANY of the p slots, not just the one adjacent to the circuit's start.
    # This is the old Excel generator's method ("degrees per cycle" = the next
    # multiple of the "cycle width" 360/p) and caps the extra rotation added at
    # the turnarounds at about one slot (360/p) instead of up to a full turn.
    #
    # k must be coprime with p: stepping k slots at a time then visits every one
    # of the p slots once per cycle. With a common factor it would keep
    # revisiting a subset of them and leave the rest of the tank bare (the Excel
    # sheet never checked this; it only worked because its pattern numbers were
    # primes). With a single strand per cycle there are no within-cycle steps,
    # so every circuit also carries the cycle shift and k may cover less.
    interval = 360.0 / pattern_number
    needed = base - (shift if pattern_number == 1 else 0.0)
    k = max(1, math.ceil(needed / interval - 1e-9))
    while math.gcd(k, pattern_number) != 1:
        k += 1
    return k


def compute_dwell_extras(traverse_rotation, dwell, pattern_number, shift):
    # Dwell is a minimum, not a fixed angle: on top of 2x the dwell minimum (it
    # applies at both turnarounds), each circuit gets extra rotation so the NEXT
    # circuit starts exactly on the pattern -- k slots on (see
    # compute_pattern_skip) within a cycle, plus the cycle shift after a cycle's
    # last circuit. Both extras use the SAME skip, so they differ only by the
    # shift (a fraction of one band width), which keeps the turnarounds nearly
    # identical from circuit to circuit. Returns (extra_within, extra_between, k).
    base = traverse_rotation + 2 * dwell
    k = compute_pattern_skip(base, pattern_number, shift)
    extra_within = k * (360.0 / pattern_number) - base
    return extra_within, extra_within + shift, k


def compute_turnaround_balance_offset(total_circuits, n_strands, dwell, extra_within, extra_between):
    # The outbound (right) turnaround's dwell must stay a TRUE CONSTANT across
    # every circuit -- not just small on average -- or the return traversal's
    # pattern breaks. Here's why: the forward-traversal start angles already
    # form a correct progression (stepping k pattern slots within a cycle, plus
    # the cycle shift between cycles); the forward-END angles are
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
    # extra rotation over the whole layup makes the running total spent
    # on the right and left turnarounds come out exactly equal.
    #
    # Every circuit carries its extra (the layup's last one too, which leaves
    # the pattern closed for whatever follows), and the extras only differ by
    # the cycle shift, so K sits within a fraction of a band of every circuit's
    # half -- the left turnaround matches the right one to within a few degrees
    # on every circuit, not just on average. (An earlier version aligned each
    # circuit to the ADJACENT slot, whose extras swung by up to a full turn
    # between circuits, and gave the layup's last circuit none at all; that
    # forced K below the dwell minimum, so small dwell settings piled the
    # difference onto the chuck-side turnaround.)
    if total_circuits <= 0:
        return 0.0
    n_between = total_circuits // n_strands  # every cycle's last circuit
    n_within = total_circuits - n_between
    sum_extra = n_between * extra_between + n_within * extra_within
    k_ideal = sum_extra / (2.0 * total_circuits)
    # Clamped so neither turnaround's dwell (dwell + K, or dwell + extra_i - K)
    # can ever go negative.
    min_extra = extra_between if n_within == 0 else min(extra_within, extra_between)
    return max(0.0, min(k_ideal, dwell + min_extra))


def compute_dwell_blend(dwell_amount, optimize, zone_steps=0, n_steps=3, max_fraction=0.3, max_deg=20.0):
    # Turnaround zone (zone_steps > 0): the old Excel generator's turnaround.
    # There is no pure-rotation move at all: the whole turnaround rotation is
    # spread evenly over the last `zone_steps` traversal steps running out to
    # the tank end (half of it) and the first `zone_steps` running back (the
    # other half), on top of their normal helix rotation. The Excel sheet did
    # exactly this over the bulkhead -- its two 80 mm "bulkhead height
    # compensation" moves out and back, each with a quarter of the circuit's
    # turnaround rotation. The fiber then turns around across the whole zone
    # instead of wrapping on top of itself as a ring at one X position, which
    # builds up over hundreds of circuits. Takes precedence over Optimize
    # Trajectory, which only eases a small part of a pure-rotation dwell.
    #
    # "Optimize Trajectory": at a turnaround, the machine otherwise goes from
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
    if dwell_amount <= 0:
        return [], [], dwell_amount
    if zone_steps > 0:
        per_step = dwell_amount / (2 * zone_steps)
        tail_blend = [per_step] * zone_steps
        head_blend = [per_step] * zone_steps
        return head_blend, tail_blend, dwell_amount - sum(tail_blend) - sum(head_blend)
    if not optimize:
        return [], [], dwell_amount
    blend_each_side = min(max_deg, dwell_amount * max_fraction)
    weights = list(range(1, n_steps + 1))
    wsum = float(sum(weights))
    tail_blend = [blend_each_side * w / wsum for w in weights]
    head_blend = [blend_each_side * w / wsum for w in reversed(weights)]
    core_dwell = dwell_amount - sum(tail_blend) - sum(head_blend)
    return head_blend, tail_blend, core_dwell


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


def compute_cycles_for_full_coverage(dt, wind_angle, bandwidth, pattern_number):
    # Each cycle lays `pattern_number` strands evenly spread around the full
    # circumference, and the layup's cycles share out the gap between two
    # strands evenly (see plan_layup). So the tank is fully covered once
    # (total bands needed for one wrap) / pattern_number cycles have run, where
    # total bands = circumference / effective tow-width (tow-width widened by
    # 1/cos(wind_angle) to account for the helix angle, same conversion used
    # for band_degrees elsewhere). Fractional; see full_coverage_cycles.
    if wind_angle <= 0 or wind_angle >= 90 or bandwidth <= 0 or pattern_number <= 0:
        return 0.0
    circumference = math.pi * dt
    effective_bandwidth = bandwidth / math.cos(math.radians(wind_angle))
    total_bands = circumference / effective_bandwidth
    return total_bands / pattern_number


def full_coverage_cycles(dt, wind_angle, bandwidth, pattern_number):
    """The fewest whole cycles that cover the tank 100 % -- what an auto-cycle
    layup winds -- or None if the inputs don't allow computing it."""
    cycles = compute_cycles_for_full_coverage(dt, wind_angle, bandwidth, pattern_number)
    if cycles <= 0:
        return None
    # The tolerance keeps float noise on an exact fit (e.g. 30.000000000001)
    # from costing a whole extra cycle.
    return max(1, math.ceil(cycles - 1e-9))


class LayupPlan(NamedTuple):
    """Per-layup constants derived once from the job, shared by the motion
    generator and the instant (non-simulated) estimates."""
    pitch: float            # axial advance per mandrel revolution (mm)
    shift_degrees: float    # rotation added between consecutive cycles (the closing shift)
    band_degrees: float     # angular width of one band around the tank (tow width / cos(angle))
    total_circuits: int
    skip: int               # pattern slots stepped per circuit (see compute_pattern_skip)
    extra_within: float     # alignment rotation after a circuit that stays within its cycle
    extra_between: float    # alignment rotation after a cycle's last circuit
    right_offset: float     # fixed share of the extra rotation given to every far-end turnaround

    @property
    def coverage(self):
        # Fraction of the surface the layup's bands cover: each cycle moves the
        # pattern on by shift_degrees, so 1.0 means bands exactly edge to edge,
        # above 1.0 they overlap evenly, below 1.0 evenly spaced gaps remain.
        return self.band_degrees / self.shift_degrees

    def extra_after(self, circuit, n_strands):
        # The alignment rotation owed at the end of `circuit`. The layup's last
        # circuit gets it too: that closes the pattern, so a following identical
        # layup winds exactly on top of this one (the Excel sheet's "layers").
        return self.extra_between if (circuit + 1) % n_strands == 0 else self.extra_within


def plan_layup(job, layup):
    dt, lt = job.tank_diameter, job.tank_length
    p = layup.pattern_number
    pitch = (math.pi * dt) / math.tan(math.radians(layup.wind_angle))
    band_degrees = (job.bandwidth / math.cos(math.radians(layup.wind_angle)) / (math.pi * dt)) * 360.0
    # Closing shift: the gap between two neighbouring pattern slots (360/p) is
    # divided evenly over the layup's cycles, so after its last cycle the bands
    # meet the first ones exactly -- evenly overlapping when the layup has at
    # least "Cycles for Full Coverage" cycles, instead of one lumped overlap
    # seam from stepping a whole band width per cycle. Same idea as the Excel
    # sheet's (360/p) / circuits-for-coverage shift, but applied once per cycle
    # rather than smeared over every circuit: the sheet's smeared version
    # offsets each pattern slot by a different fraction of a band, which leaves
    # uncovered strips up to several mm wide at some slot seams.
    shift_degrees = (360.0 / p) / layup.passes
    total_circuits = p * layup.passes
    traverse_rotation = (2.0 * lt / pitch) * 360.0
    extra_within, extra_between, skip = compute_dwell_extras(traverse_rotation, layup.turnaround_angle, p, shift_degrees)
    right_offset = compute_turnaround_balance_offset(total_circuits, p, layup.turnaround_angle, extra_within, extra_between)
    return LayupPlan(pitch, shift_degrees, band_degrees, total_circuits, skip, extra_within, extra_between, right_offset)


# --- The motion generator ---

class Move(NamedTuple):
    x: float
    y: float
    a: float          # continuous mandrel angle (deg) -- never reset, unlike the written A value
    feed: float       # G-code F for this move
    duration: float   # seconds
    tow: float        # tow length laid by this move (mm)
    r: float          # tank radius at the target X (mm)
    layup: int        # index into job.layups
    circuit: int      # circuit index within its layup
    dwell: bool       # True for a pure-rotation turnaround move (none with a turnaround zone)


class CycleComplete(NamedTuple):
    number: int       # 1-based, counted across the whole program
    layup: int
    a: float          # continuous mandrel angle at the end of the cycle


class LayupStart(NamedTuple):
    index: int


class _TankGeometry(NamedTuple):
    x_start: float
    x_end: float
    r_tank: float
    r_cap: float
    l_dome: float
    cap_type: str

    def radius(self, x):
        return calc_R(x, self.x_start, self.x_end, self.r_tank, self.r_cap, self.l_dome, self.cap_type)


def _tank_geometry(job):
    return _TankGeometry(job.chuck_offset, job.chuck_offset + job.tank_length, job.tank_diameter / 2,
                         job.end_cap_diameter / 2, job.dome_length, job.end_cap_type)


def _eye_y_function(job, geom):
    # Y keeps the eye's whole footprint min_spacing clear of the tank surface,
    # clamped to the axis travel. Returned as a closure so the per-step hot loop
    # doesn't re-derive the constant geometry every 5 mm.
    y_base = Y_REFERENCE - job.eye_arm_length
    args = (geom.x_start, geom.x_end, geom.r_tank, geom.r_cap, geom.l_dome, geom.cap_type, job.eye_width)
    space = job.min_spacing

    def eye_y(x):
        return max(0.0, min(Y_TRAVEL, y_base - (calc_R_eye_footprint(x, *args) + space)))
    return eye_y


def start_position(job):
    """(x, y) of the carriage before the first traversal."""
    x = job.wind_start_x
    return x, _eye_y_function(job, _tank_geometry(job))(x)


def iter_program(job):
    """Yields every event of the winding program in machine order: a LayupStart
    before each layup, a Move per G-code move, and a CycleComplete after each
    cycle. The job must be free of geometry_errors()."""
    geom = _tank_geometry(job)
    x_start, x_end = geom.x_start, geom.x_end
    eye_y = _eye_y_function(job, geom)
    max_a_speed = job.max_a_speed
    n_layups = len(job.layups)
    zone_steps = job.turnaround_zone_steps
    max_move_time = job.max_move_time

    def moves(x0, y0, a0, x1, y1, a1, layup_index, circuit, dwell):
        # One straight (X, Y, A) move, split into equal pieces if it would take
        # longer than Max Move Time. Klipper's pause only stops it from reading
        # further lines: everything already queued -- about 2 s of motion, plus
        # whatever remains of a longer move -- still runs, and a queued move is
        # never cut short. Near-hoop steps and turnaround rotations can take
        # several seconds each (more at low rotation speeds), so bounding every
        # move's duration is what bounds the stop. A G1 moves all axes linearly
        # together, so the pieces trace exactly the same path at the same speed:
        # no corners for the planner to slow down at, and the pattern, time and
        # tow are unchanged.
        _, duration, _ = calc_move(x0, y0, a0, x1, y1, a1, max_a_speed, 0.0)
        n = max(1, math.ceil(duration / max_move_time - 1e-9))
        px, py, pa = x0, y0, a0
        for k in range(1, n + 1):
            if k == n:
                qx, qy, qa = x1, y1, a1
            else:
                f = k / n
                qx, qy, qa = x0 + (x1 - x0) * f, y0 + (y1 - y0) * f, a0 + (a1 - a0) * f
            qr = geom.radius(qx)
            feed, piece_duration, tow = calc_move(px, py, pa, qx, qy, qa, max_a_speed, qr)
            yield Move(qx, qy, qa, feed, piece_duration, tow, qr, layup_index, circuit, dwell)
            px, py, pa = qx, qy, qa

    # The wind starts at wind_start_x, so its very first pass runs from there
    # (not from the tank's end) toward +X. Every circuit still turns the mandrel
    # by exactly the same amount, so this only shifts the whole program by a
    # constant angle: the pattern is untouched, and the part skipped (the
    # first pass over the chuck-side dome) is one band where every band
    # overlaps anyway.
    x, a = job.wind_start_x, 0.0
    y = eye_y(x)
    cycle_number = 0
    pending_head_blend = []  # blend carried from the previous dwell into this traversal's first steps
    for li, layup in enumerate(job.layups):
        yield LayupStart(li)
        plan = plan_layup(job, layup)
        n_strands = layup.pattern_number
        for i in range(plan.total_circuits):
            extra = plan.extra_after(i, n_strands)
            # See compute_turnaround_balance_offset: the right (outbound)
            # turnaround always gets the exact same fixed offset, every circuit,
            # so the return traversal's own pattern stays in step; the left
            # (return-side) turnaround absorbs whatever's left of this circuit's
            # `extra`, exactly as it always has to for the forward traversal's
            # pattern to land correctly next circuit.
            for direction in (1, -1):
                target_x = x_end if direction == 1 else x_start
                dwell_amount = layup.turnaround_angle + (plan.right_offset if direction == 1 else (extra - plan.right_offset))
                head_blend, tail_blend, core_dwell = compute_dwell_blend(dwell_amount, job.optimize_trajectory, zone_steps)
                if li == n_layups - 1 and i == plan.total_circuits - 1 and direction == -1:
                    # Nothing follows the program's very last dwell to absorb a
                    # head blend into, so fold it back into the dwell move itself
                    # instead of losing that rotation. Between layups the blend
                    # simply carries over into the next layup's first traversal,
                    # just like it does between circuits.
                    core_dwell += sum(head_blend)
                    head_blend = []
                for x_next, a_delta in traversal_steps(x, target_x, STEP_SIZE, plan.pitch, pending_head_blend, tail_blend):
                    y_next = eye_y(x_next)
                    a_next = a + a_delta
                    yield from moves(x, y, a, x_next, y_next, a_next, li, i, False)
                    x, y, a = x_next, y_next, a_next
                pending_head_blend = head_blend
                # A turnaround zone spreads all of the rotation over the steps,
                # leaving nothing (but float dust) for a pure-rotation move.
                if abs(core_dwell) > 1e-6:
                    a_next = a + core_dwell
                    yield from moves(x, y, a, x, y, a_next, li, i, True)
                    a = a_next
            if (i + 1) % n_strands == 0:
                cycle_number += 1
                yield CycleComplete(cycle_number, li, a)


# --- Preview simulation ---

@dataclass
class LayupResult:
    time: float = 0.0   # seconds
    tow: float = 0.0    # mm
    cylinder_tow: float = 0.0  # mm of it laid on the straight (cylindrical) section
    # strand_runs[strand] -> list of runs, each run a list of raw (x, r, angle_deg)
    # points along one single-direction traversal of the layup's FIRST cycle.
    strand_runs: list = field(default_factory=list)


@dataclass
class SimulationResult:
    total_time: float
    total_tow: float
    layups: list  # one LayupResult per job layup

    @property
    def cylinder_tow(self):
        return sum(layup.cylinder_tow for layup in self.layups)


class _RunRecorder:
    """Collects raw (x, r, angle_deg) strand points from a stream of Moves for
    the 3D previews, instead of pre-projected screen coordinates -- the
    front/back split and (px, py) projection depend on the viewer's chosen
    azimuth, which can change (e.g. via the rotate-view buttons) without
    re-running the program, so that work is left to the caller. Points are
    grouped into "runs": one per single-direction traversal, broken at every
    turnaround (a pure-rotation dwell or, with a turnaround zone, wherever the
    traverse reverses), so the caller knows which points to connect."""

    def __init__(self, geom):
        self.geom = geom
        self.run = self.key = self.sink = None

    def add(self, ev, prev, sink):
        # `prev` is the (x, r, angle) the move starts from; `sink` the list its
        # run belongs in, or None to leave the move out.
        x0, _, a0 = prev
        key = None if ev.dwell or sink is None else (ev.layup, ev.circuit, ev.x > x0)
        if self.run is not None and key != self.key:
            self.close()
        if key is None:
            return
        if self.run is None:
            self.run, self.key, self.sink = [prev], key, sink
        # STEP_SIZE is a fixed X distance, not an angular one, so near the steep
        # end of the wind-angle range (where pitch shrinks toward the tow width)
        # a single step can sweep close to -- or, right at the computed max
        # angle, exactly -- a full revolution. Recording only the step's
        # endpoint then aliases the helix for rendering: whole visible arcs can
        # fall entirely between two samples (dropped strand segments), and at the
        # point where one step's sweep is an exact multiple of 360 deg, every
        # sample lands at the same rotational phase, which flattens the drawn
        # path into what looks like a shallow straight line instead of a tight
        # helix. Subdividing by rotation (not distance) fixes the rendering
        # without touching the motion or the time/tow accumulation, so the
        # G-code this mirrors is completely unaffected; only how densely the
        # already-correct path gets sampled for drawing changes.
        a_delta = ev.a - a0
        n_sub = max(1, min(60, math.ceil(abs(a_delta) / RENDER_MAX_DEG_PER_STEP)))
        for k in range(1, n_sub + 1):
            frac = k / n_sub
            fx = x0 + (ev.x - x0) * frac
            fr = ev.r if k == n_sub else self.geom.radius(fx)
            self.run.append((fx, fr, a0 + a_delta * frac))

    def close(self):
        if self.run is not None:
            self.sink.append(self.run)
        self.run = self.key = self.sink = None


def simulate(job):
    """Runs the whole program for the time/tow estimates, and records each
    layup's first-cycle strand paths for the 3D preview (see _RunRecorder)."""
    geom = _tank_geometry(job)
    results = [LayupResult(strand_runs=[[] for _ in range(l.pattern_number)]) for l in job.layups]
    total_time, total_tow = 0.0, 0.0
    # The straight section, for the strength estimate: a move counts as laid
    # there when its midpoint is (moves are 5 mm steps, so that's exact enough).
    cyl_lo, cyl_hi = geom.x_start + geom.l_dome, geom.x_end - geom.l_dome
    recorder = _RunRecorder(geom)
    prev = (job.wind_start_x, geom.radius(job.wind_start_x), 0.0)
    for ev in iter_program(job):
        if type(ev) is not Move:
            continue
        total_time, total_tow = total_time + ev.duration, total_tow + ev.tow
        res = results[ev.layup]
        res.time += ev.duration
        res.tow += ev.tow
        if cyl_lo <= (prev[0] + ev.x) / 2 <= cyl_hi:
            res.cylinder_tow += ev.tow
        first_cycle = ev.circuit < job.layups[ev.layup].pattern_number
        recorder.add(ev, prev, res.strand_runs[ev.circuit] if first_cycle else None)
        prev = (ev.x, ev.r, ev.a)
    recorder.close()
    return SimulationResult(total_time, total_tow, results)


# --- Continuing an interrupted wind ---

class ProgramPoint(NamedTuple):
    """A point in the program to continue an interrupted wind from."""
    layup: int      # 0-based
    cycle: int      # 0-based, within the layup
    # Mandrel angle since the cycle began (deg): exactly the A the machine
    # shows, since A is reset to 0 at every cycle. The mandrel never turns
    # backwards, so within a cycle it pins down a single point -- unlike X,
    # which every pass crosses.
    angle: float = 0.0


class Resume(NamedTuple):
    """How a partial program continues from `point`. With `rehome` (for a
    severed fiber), it homes X and Y, moves the eye to the point and pauses so
    the fiber can be reattached; without it, the eye must already be there."""
    point: ProgramPoint
    rehome: bool = False


class PointError(ValueError):
    """The requested point doesn't exist in the program."""


class Location(NamedTuple):
    point: ProgramPoint
    x: float
    y: float
    r: float            # tank radius under the eye
    a: float            # continuous mandrel angle
    a_offset: float     # continuous angle at the start of the point's cycle (A = a - a_offset)
    elapsed: float      # seconds of winding before the point
    tow: float          # mm of tow laid before the point
    cycle_number: int   # the point's cycle, 1-based across the whole program


def _check_point(job, point):
    if not 0 <= point.layup < len(job.layups):
        raise PointError(f"There is no Layup {point.layup + 1}.")
    passes = job.layups[point.layup].passes
    if not 0 <= point.cycle < passes:
        raise PointError(f"Layup {point.layup + 1} has cycles 1 to {passes}.")
    if point.angle < 0:
        raise PointError("The mandrel angle can't be negative.")


def _walk_to(job, point, on_move=None):
    """Walks the program up to `point`. Returns (location, rest), where `rest`
    iterates the program's events from the point on -- starting with what's
    left of the move the point lies on. `on_move(move, prev)` is shown every
    move before the point (the last one cut off at it), with `prev` the
    (x, r, angle) it starts from. Raises PointError for a point that doesn't
    exist, e.g. an angle beyond the end of its cycle."""
    _check_point(job, point)
    geom = _tank_geometry(job)
    events = iter_program(job)
    layup, cycle, a_offset, cycle_number = -1, 0, 0.0, 1
    px, py = start_position(job)
    pa, pr = 0.0, geom.radius(px)
    elapsed = tow = 0.0
    for ev in events:
        kind = type(ev)
        if kind is LayupStart:
            layup, cycle = ev.index, 0
            continue
        if kind is CycleComplete:
            if (layup, cycle) == (point.layup, point.cycle):
                raise PointError(f"Cycle {point.cycle + 1} of Layup {point.layup + 1} ends at "
                                 f"A {pa - a_offset:.1f}°.")
            a_offset, cycle, cycle_number = ev.a, cycle + 1, cycle_number + 1
            continue
        if (layup, cycle) == (point.layup, point.cycle) and point.angle <= ev.a - a_offset + 1e-6:
            start = pa - a_offset
            f = 0.0 if point.angle <= start else (point.angle - start) / (ev.a - a_offset - start)
            x, y, a = px + (ev.x - px) * f, py + (ev.y - py) * f, pa + (ev.a - pa) * f
            r = geom.radius(x)
            if on_move is not None and f > 0:
                on_move(Move(x, y, a, ev.feed, ev.duration * f, ev.tow * f, r, ev.layup, ev.circuit, ev.dwell), (px, pr, pa))
            location = Location(point, x, y, r, a, a_offset, elapsed + ev.duration * f, tow + ev.tow * f, cycle_number)
            rest = [] if f >= 1 - 1e-12 else [
                Move(ev.x, ev.y, ev.a, ev.feed, ev.duration * (1 - f), ev.tow * (1 - f), ev.r, ev.layup, ev.circuit, ev.dwell)]
            return location, itertools.chain(rest, events)
        if on_move is not None:
            on_move(ev, (px, pr, pa))
        elapsed, tow = elapsed + ev.duration, tow + ev.tow
        px, py, pa, pr = ev.x, ev.y, ev.a, ev.r
    raise PointError("That point is past the end of the program.")


def locate(job, point):
    """Where the machine is at `point` (a Location). Raises PointError."""
    return _walk_to(job, point)[0]


class Progress(NamedTuple):
    """How the tank looks at a point of the program, for the preview."""
    location: Location
    covered: tuple      # earlier layups that cover the whole tank: drawn as a solid layer
    # layup -> raw (x, r, angle) runs of every pass wound so far (see
    # _RunRecorder), for the point's own layup and any earlier one with gaps.
    runs: dict


def progress(job, point):
    _check_point(job, point)
    covered = tuple(i for i in range(point.layup) if plan_layup(job, job.layups[i]).coverage >= 0.995)
    runs = {i: [] for i in range(point.layup + 1) if i not in covered}
    recorder = _RunRecorder(_tank_geometry(job))
    location, _ = _walk_to(job, point, lambda ev, prev: recorder.add(ev, prev, runs.get(ev.layup)))
    recorder.close()
    return Progress(location, covered, runs)


# --- G-code file format ---

def settings_items(job):
    """(key, value) pairs written as the G-code file's settings header. Global
    settings use their WindingJob field names; each layup is one
    `layup_<n>: key=value ...` line, so a file can be reopened and every
    setting restored (see settings_from_header)."""
    items = [(key, getattr(job, key)) for key in GLOBAL_KEYS]
    items.append(("layups", len(job.layups)))
    for n, layup in enumerate(job.layups, 1):
        items.append((f"layup_{n}", " ".join(f"{key}={getattr(layup, key)}" for key in LAYUP_KEYS)))
    return items


def layups_from_header(settings):
    """Rebuilds the layup list from a parsed settings header ({key: raw string}).
    Also reads files written before multi-layup support, which stored a single
    pattern's settings as top-level keys. Settings a file predates keep their
    defaults -- in particular, its cycle counts stay exactly as written (not
    auto). Returns None if there's nothing to restore."""
    def parse(values):
        parsed = {}
        for f in fields(Layup):
            if f.name in values:
                raw = values[f.name].strip()
                parsed[f.name] = raw.lower() in ("1", "true", "yes", "on") if f.type in (bool, "bool") else float(raw)
        return Layup(**parsed)

    try:
        count = int(float(settings.get("layups", 0)))
    except ValueError:
        count = 0
    layups = []
    for n in range(1, count + 1):
        raw = settings.get(f"layup_{n}")
        if raw is None:
            continue
        try:
            layups.append(parse(dict(token.split("=", 1) for token in raw.split() if "=" in token)))
        except ValueError:
            continue
    if layups:
        return layups
    legacy_keys = ("passes", "pattern_number", "wind_angle", "turnaround_angle")
    if all(key in settings for key in legacy_keys):
        try:
            return [parse({key: settings[key] for key in legacy_keys})]
        except ValueError:
            return None
    return None


def resume_items(resume):
    """Settings-header entries recording how a partial program was built, so
    reopening the file restores the conditions it continues from."""
    p = resume.point
    return [("partial", True), ("partial_layup", p.layup + 1), ("partial_cycle", p.cycle + 1),
            ("partial_angle", f"{p.angle:.3f}"), ("partial_rehome", resume.rehome)]


def resume_from_header(settings):
    """The Resume a partial program was built with, from its parsed settings
    header -- or None for a complete program."""
    if settings.get("partial", "").strip().lower() != "true":
        return None
    try:
        point = ProgramPoint(int(float(settings["partial_layup"])) - 1, int(float(settings["partial_cycle"])) - 1,
                             float(settings["partial_angle"]))
    except (KeyError, ValueError):
        return None
    return Resume(point, settings.get("partial_rehome", "").strip().lower() == "true")


def _set_a(angle):
    # "G92 A<angle>": declares the mandrel's current angle without moving it.
    return "G92 A0" if abs(angle) < 5e-4 else f"G92 A{angle:.3f}"


def _rotation_limited_feed(dx, dy, da, max_a_speed):
    # G-code F is the speed along the whole (X, Y, A) move, so the mandrel
    # turns at F * |da| / length. The largest whole F that keeps that at or
    # below Max Rotation Speed is floor(max * length / |da|): rounding down, so
    # no move -- however it was split or rounded -- ever turns faster than the
    # limit, and each still runs within a hair of it. A move without rotation
    # runs at Max Rotation Speed along its length.
    da = abs(da)
    if da <= 0:
        return int(max_a_speed)
    length = math.sqrt(dx * dx + dy * dy + da * da)
    return max(1, math.floor(max_a_speed * length / da))


def _split_travel(start, end, max_distance):
    # Evenly spaced stops from `start` to `end`, none further apart than
    # max_distance (the last one is `end` itself).
    n = max(1, math.ceil(abs(end - start) / max_distance - 1e-9))
    return [start + (end - start) * k / n for k in range(1, n + 1)]


def _write_move_to_start(out, job, x, y, message, angle=0.0):
    # After homing, bring the eye to (x, y) without crossing the tank: pull it
    # fully back first (Y0 is the far end of its travel, away from the tank;
    # G28 normally leaves it there already), travel along X, and only then
    # move in to winding distance. Then PAUSE, so the fiber can be attached
    # there; winding resumes from the machine. Assumes G28 leaves the carriage
    # at X0 Y0, the app's machine origin. Travel runs at Max Rotation Speed's
    # rate along the move, split like every other move so none takes longer
    # than Max Move Time.
    feed = int(job.max_a_speed)
    max_distance = feed / 60.0 * job.max_move_time
    out.write("; --- MOVE TO WIND START ---\n")
    out.write(f"G1 Y{0.0:.3f} F{feed}\n")
    for xi in _split_travel(0.0, x, max_distance):
        out.write(f"G1 X{xi:.3f} F{feed}\n")
    for yi in _split_travel(0.0, y, max_distance):
        out.write(f"G1 Y{yi:.3f} F{feed}\n")
    out.write(f"; {message}\n")
    out.write("PAUSE\n")
    # The mandrel may have been turned by hand while attaching the fiber: its
    # angle at resume is declared to be `angle` (the wind's zero, or the A of
    # the point a partial program continues from), so the first move doesn't
    # turn it back.
    out.write(_set_a(angle) + "\n")


def write_gcode(out, job, start_gcode="", end_gcode="", resume=None, extra_header=()):
    """Writes the program for `job` to the text stream `out`; the job must pass
    validate(). With a Resume, writes a partial program instead: the same
    moves as the complete one from resume.point on, in the same A frame, so it
    continues an interrupted wind seamlessly (raises PointError for a point
    that doesn't exist). `extra_header` adds (key, value) pairs to the settings
    header, e.g. the material estimate the file was made with."""
    out.write("; --- WINDER SETTINGS ---\n")
    for key, value in settings_items(job) + (resume_items(resume) if resume else []) + list(extra_header):
        out.write(f"; {key}: {value}\n")
    out.write(f"; ld: {job.dome_length:.3f}\n; -----------------------\n\n")
    if resume is None:
        x, y = start_position(job)
        angle = a_offset = 0.0
        events = iter_program(job)
        if job.home_before_wind:
            out.write("G28\n")
            _write_move_to_start(out, job, x, y, "Attach the fiber here, then resume on the machine to start winding")
        else:
            # Skip homing: just define wherever the carriage/mandrel currently
            # is as the zero reference for this wind, without moving.
            out.write("G92 A0\n")
    else:
        location, events = _walk_to(job, resume.point)
        x, y, a_offset = location.x, location.y, location.a_offset
        angle = location.a - location.a_offset
        if resume.rehome:
            # X and Y only: the mandrel keeps its angle, so the fiber already
            # wound stays lined up with the rest of the program.
            out.write("G28 X Y\n")
            _write_move_to_start(out, job, x, y, "Reattach the fiber here, then resume on the machine to continue winding",
                                 angle)
        else:
            # The eye is already at the point; the mandrel's angle there is
            # declared to be the program's A at the point.
            out.write(_set_a(angle) + "\n")
    if start_gcode: out.write(start_gcode + "\n")
    # No rotation is planned here, and with F = Max Rotation Speed any rotation
    # the machine does need (e.g. an A axis not homed to 0) can't be faster.
    out.write(f"G1 X{x:.3f} Y{y:.3f} A{angle:.3f} F{int(job.max_a_speed)}\n")
    if resume is not None:
        out.write(f"; LAYUP_START:{resume.point.layup + 1}\n")
    _write_events(out, job, events, x, y, angle, a_offset)
    if end_gcode: out.write(end_gcode + "\n")


def _write_events(out, job, events, x, y, angle, a_offset):
    # The machine executes the coordinates as written (3 decimals), so feed
    # rates are computed from those -- relative to the previous written
    # position -- not from the unrounded ones.
    wx, wy, wa = float(f"{x:.3f}"), float(f"{y:.3f}"), float(f"{angle:.3f}")
    for ev in events:
        if type(ev) is Move:
            gx, gy, ga = f"{ev.x:.3f}", f"{ev.y:.3f}", f"{(ev.a - a_offset):.3f}"
            nx, ny, na = float(gx), float(gy), float(ga)
            out.write(f"G1 X{gx} Y{gy} A{ga} F{_rotation_limited_feed(nx - wx, ny - wy, na - wa, job.max_a_speed)}\n")
            wx, wy, wa = nx, ny, na
        elif type(ev) is CycleComplete:
            out.write(f"; CYCLE_COMPLETE:{ev.number}\n")
            # Reset the firmware's A-axis position to 0 without moving, so the
            # accumulated rotation over a long wind never approaches the axis's
            # +/-9999999 limit. Subsequent A values are written relative to this.
            out.write("G92 A0\n")
            a_offset, wa = ev.a, 0.0
        else:
            if ev.index > 0 and job.pause_after_layup:
                # The pattern changes here: stop so the fiber can be checked
                # (e.g. for slipping) before the next layup starts.
                out.write(f"; Layup {ev.index} complete - check the fiber, then resume on the machine\n")
                out.write("PAUSE\n")
            out.write(f"; LAYUP_START:{ev.index + 1}\n")
