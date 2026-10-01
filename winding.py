"""Winding math and G-code generation for the CFRP Winder.

Pure Python with no Tkinter dependency, so everything that decides where the
machine moves can be exercised headless (see test_winding.py). The G-code
writer (`write_gcode`) and the Settings Preview's simulation (`simulate`) are
both driven by the exact same motion generator (`iter_program`), so the preview
and estimates can never drift from what actually gets written to the file.

A winding program is a sequence of *layups*. Each layup winds its own number of
cycles with its own winding angle, pattern number and turnaround dwell, one
after the other on the same tank; the tank geometry, machine settings, tow
width and trajectory optimization are shared by the whole program. A layup can
instead be a 90° wind (`Layup.hoop`): a single pass over the straight section,
each turn laid one band width on from the last.
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
# Failsafe: the highest G-code feed rate (F, mm/min) any move may use, whatever
# the speed settings allow -- measured like F, along the whole move with A in
# degrees. Keep it at or below the controller's max_velocity (mm/s) x 60.
MAX_FEED = 40000.0
HOOP_ANGLE_THRESHOLD = 85.0  # a Winding Angle entered above this makes the layup a 90° wind (the app's rule)
LEAD_IN_ANGLE = 45.0  # angle of the run onto a 90° wind's start when no helical layup precedes it
FLAT_TURNAROUND_ZONE = 50.0  # Auto turnaround zone with flat end caps (no dome to turn on), mm
# Most a turnaround's direction may change from one G-code move to the next,
# in (X, Y, A) space (deg). Klipper takes such a junction at full speed: at its
# default square_corner_velocity (5 mm/s) it allows about 350 mm/s.
TURN_MAX_DEFLECTION = 1.5

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
    # A 90° wind: one pass along the straight section only (never over the
    # domes), each turn one band width on from the last, so the bands lie edge
    # to edge. It ends at the other end of the tank, so every layup after it
    # starts from there and winds the other way (see start_sides). The helical
    # settings (HOOP_IGNORED) are kept, but not used while this is set.
    hoop: bool = False

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
    # The fastest the fiber may leave the eye (mm/s): the tow laid per second.
    # Every move runs as fast as both this and Max Rotation Speed allow (see
    # calc_move) -- at low winding angles, where the carriage covers far more
    # ground than the mandrel turns, this is what holds the carriage back. The
    # default matches the speeds the former 300 mm/s Max Feedrate gave the
    # low-angle layers it limited (see wind_speed).
    max_filament_speed: float = 310.0
    # Longest time (s) any single G-code move may take; longer ones are split
    # into equal pieces. Klipper can't interrupt a move it has queued, so this
    # bounds how long the machine keeps going after a pause (see iter_program).
    max_move_time: float = 0.5
    end_cap_type: str = "Round"
    tank_length: float = 1000.0
    tank_diameter: float = 200.0
    end_cap_diameter: float = 50.0
    bandwidth: float = 5.0
    # Length (mm) at each tank end over which the carriage turns around: it runs
    # into the zone on the helix, slows smoothly to a stop at the very end while
    # the mandrel keeps turning, and runs back out (see turnaround_curve).
    # Auto: the dome, so the fiber turns around beyond the straight section
    # (FLAT_TURNAROUND_ZONE with flat end caps). 0 = turn on the spot.
    turnaround_zone: float = 0.0
    turnaround_zone_auto: bool = True
    # PAUSE between layups, so the fiber can be checked after every pattern change.
    pause_after_layup: bool = True
    layups: tuple = field(default=(Layup(),))

    def __post_init__(self):
        _coerce_fields(self)
        # Auto-cycle layups get their cycle count resolved here, once, against
        # this job's own tank and tow -- so every consumer (motion, estimates,
        # settings header, cache key) sees the same, never-stale number.
        object.__setattr__(self, "layups", tuple(self._resolve_cycles(layup) for layup in self.layups))
        # Same for an auto turnaround zone: the dome, at most half the tank.
        if self.turnaround_zone_auto:
            zone = self.dome_length if self.end_cap_type == "Round" else FLAT_TURNAROUND_ZONE
            object.__setattr__(self, "turnaround_zone", max(0.0, min(zone, self.tank_length / 2)))
        # Same for an auto start position: where the dome ends and the tank
        # turns straight -- or, for a 90° first layup, right where it starts.
        if self.start_x_auto:
            start = self.hoop_span[0] if self.layups and self.layups[0].hoop else self.chuck_offset + self.dome_length
            object.__setattr__(self, "start_x", start)

    @property
    def wind_start_x(self):
        """Machine X where the wind begins: the Start Wind at X setting after
        homing, otherwise the tank's chuck-side end (the machine then can't be
        assumed to know where anything else is)."""
        return self.start_x if self.home_before_wind else self.chuck_offset

    @property
    def start_x_range(self):
        # From the tank's chuck-side end up to where the first pass still has
        # room for the far end's whole turnaround zone before it turns around.
        x_end = self.chuck_offset + self.tank_length
        return self.chuck_offset, x_end - max(self.turnaround_zone, 3 * STEP_SIZE)

    def _resolve_cycles(self, layup):
        if layup.hoop:
            # Its one pass is its one cycle.
            return layup if layup.passes == 1 else replace(layup, passes=1)
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
    def hoop_span(self):
        """(first, last) X of the band's center over a 90° wind: the whole band
        stays on the straight section, its edges flush with where the domes
        begin (the tank's ends for flat end caps)."""
        half = self.bandwidth / 2
        return (self.chuck_offset + self.dome_length + half,
                self.chuck_offset + self.tank_length - self.dome_length - half)

    @property
    def hoop_angle(self):
        # The angle a 90° wind actually winds at: one band width of advance per
        # turn -- just short of 90°.
        return math.degrees(math.atan2(math.pi * self.tank_diameter, self.bandwidth))

    @property
    def max_a_speed(self):
        # Max Rotation Speed as an A-axis rate (deg/min) for this tank diameter.
        return surface_speed_to_deg_per_min(self.max_surface_speed, self.tank_diameter)

    @property
    def total_cycles(self):
        return sum(layup.passes for layup in self.layups)



GLOBAL_KEYS = tuple(f.name for f in fields(WindingJob) if f.name != "layups")
LAYUP_KEYS = tuple(f.name for f in fields(Layup))
# The layup settings a 90° wind doesn't use (it always winds exactly one pass).
HOOP_IGNORED = ("passes", "pattern_number", "wind_angle", "turnaround_angle")

# Settings the WindingJob can compute itself: value key -> the flag that, while
# set, makes it do so (the value given is then ignored).
AUTO_FLAGS = {"passes": "auto_cycles", "start_x": "start_x_auto", "turnaround_zone": "turnaround_zone_auto"}

# How files written before a setting existed were actually wound, where that
# differs from the setting's current default: restoring such a file uses these
# values (or calls them with the file's settings header), so it regenerates
# the motion it was made with. (Max Move Time and Max Filament Speed need no
# entry: neither ever changes the path, only how fast it is run.) Turnarounds
# are smooth curves since files of 2026-10; older files keep their zone length,
# but their turnarounds are now wound as curves too.
LEGACY_VALUES = {
    "turnaround_zone": 0.0,
    "turnaround_zone_auto": False,
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
    if job.max_filament_speed < 1.0:
        errors.append("Max Filament Speed must be at least 1 mm/s.")
    if job.max_move_time < MIN_MOVE_TIME:
        errors.append(f"Max Move Time must be at least {MIN_MOVE_TIME:g} s.")
    if job.home_before_wind and job.tank_length > 0:
        lo, hi = job.start_x_range
        if not lo - 1e-9 <= job.start_x <= hi + 1e-9:
            errors.append(f"Start Wind at X must be between {lo:.1f} and {hi:.1f} mm "
                          "(on the tank, clear of the far end's turnaround).")
    if job.turnaround_zone < 0:
        errors.append("Turnaround Zone can't be negative.")
    elif job.tank_length > 0 and 2 * job.turnaround_zone > job.tank_length + 1e-9:
        # The zones at both ends would overlap.
        errors.append("Turnaround Zone must be at most half the Tank Length.")
    if not job.layups:
        errors.append("At least one layup is required.")
    for i, layup in enumerate(job.layups):
        prefix = _layup_prefix(job, i)
        if layup.hoop:
            first, last = job.hoop_span
            if job.tank_length > 0 and job.bandwidth > 0 and last <= first:
                errors.append(f"{prefix}The straight section is too short for a 90° wind "
                              "(it must be longer than the band is wide).")
            continue
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


def calc_move(x0, y0, a0, x1, y1, a1, max_a_speed, r_next, max_filament=math.inf):
    # The combined (X,Y,A) feed rate (G-code F, mm/min) is the fastest that
    # keeps every limit, whichever is reached first on this move: the mandrel
    # turning at max_a_speed (deg/min), the fiber leaving the eye at
    # max_filament (mm/min of tow, laid at radius r_next), and the MAX_FEED
    # failsafe. At low winding angles the fiber limit sets the pace -- the
    # rotation limit alone would drive the carriage far faster than it can
    # go -- and toward 90 deg the rotation limit does. A move without rotation
    # runs at max_a_speed's rate along its length, within the same limits.
    dx, dy, da = abs(x1 - x0), abs(y1 - y0), abs(a1 - a0)
    dist_klip = math.sqrt(dx**2 + dy**2 + da**2)
    tow_len = math.sqrt(dx**2 + (r_next * math.radians(da))**2)
    act_f = max_a_speed * dist_klip / da if da > 0 and dist_klip > 0 else max_a_speed
    if tow_len > 0:
        act_f = min(act_f, max_filament * dist_klip / tow_len)
    act_f = min(act_f, MAX_FEED)
    time_sec = (dist_klip / act_f) * 60.0 if act_f > 0 else 0
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


def traversal_steps(x_start, x_end, step_size, pitch):
    # Yields (x_next, a_delta) for one straight helix run from x_start to x_end
    # (the sign of x_end - x_start picks the direction), stepping by step_size
    # (the last step may be shorter, to land exactly on x_end). a_delta is that
    # step's rotation in degrees.
    direction = 1.0 if x_end >= x_start else -1.0
    total_dist = abs(x_end - x_start)
    if total_dist <= 1e-9:
        return
    n_full = int(total_dist // step_size)
    remainder = total_dist - n_full * step_size
    dists = [step_size] * n_full
    if remainder > 1e-9:
        dists.append(remainder)
    x = x_start
    for d in dists:
        x_next = x + direction * d
        yield x_next, (d / pitch) * 360.0
        x = x_next


def turnaround_curve(x_turn, direction, zone, k_in, k_out, dwell, eye_y):
    """One turnaround at the tank end x_turn, reached moving in `direction`
    (+1/-1): the eye runs into the zone (the last `zone` mm) on the incoming
    helix (k_in deg of rotation per mm of X), slows smoothly to a stop at
    x_turn while the mandrel keeps turning, and runs back out on the outgoing
    helix (k_out deg/mm) -- turning the mandrel by exactly what the old
    turnaround did: the helix over the zone, in and out, plus `dwell`.

    Why a curve: Klipper plans every junction between two G1 moves from the
    angle between their directions in (X, Y, A) space, A in degrees counting
    like mm, and caps the speed there by its junction deviation
    (square_corner_velocity, 5 mm/s by default). The former turnaround zone
    added a fixed extra rotation to every step in it, so its moves met the
    helix at a 42 deg kink, and Klipper braked from 250 mm/s to 12 mm/s there:
    the machine seemed to stop where the zone began. This curve leaves the
    helix along its own direction, turns gradually, and rejoins the next
    helix along its direction; it's cut into moves that each turn by at most
    TURN_MAX_DEFLECTION, which Klipper takes at full speed.

    Shape, for theta from 0 to pi (the reversal at pi/2):
        X(theta) = x_turn - direction * zone * (1 - sin theta)
        A'(theta) = zone * c(theta) * (1 + gamma * sin^2 theta)
    with c running smoothly from k_in to k_out. A'/X' then matches the incoming
    helix at theta = 0 and the outgoing one at pi -- and so does the
    curvature, zero like the helix's: the curve doesn't demand a sudden
    sideways acceleration where it begins either. gamma sets the total
    rotation, most of the dwell being turned near the reversal, where the
    carriage is slow; 1 + gamma * sin^2 theta stays positive (gamma >= -1/2
    for any dwell >= 0), so the mandrel never turns backwards.

    Returns (tail, head): the (x, y, a) points of the run in -- the reversal is
    the last one, a measured from where the curve begins -- and of the run back
    out, a measured from the reversal. Without a zone, the tail is a pure
    rotation by `dwell` at the end, and the head is empty."""
    if zone <= 0:
        return [(x_turn, eye_y(x_turn), dwell)], []
    gamma = 2.0 * (2.0 / math.pi * (1.0 + dwell / (zone * (k_in + k_out))) - 1.0)
    dk = k_out - k_in

    known = {}

    def point(t):
        if t in known:
            return known[t]
        x = x_turn - direction * zone * (1.0 - math.sin(t))
        sin_t = math.sin(t)
        c_int = k_in * t + dk * (t - sin_t) / 2                       # integral of c
        sin2_int = t / 2 - math.sin(2 * t) / 4                         # integral of sin^2
        cs2_int = k_in * sin2_int + dk / 2 * (sin2_int - sin_t ** 3 / 3)  # integral of c * sin^2
        known[t] = x, eye_y(x), zone * (c_int + gamma * cs2_int)
        return known[t]

    # Split theta until every junction -- including where the curve meets the
    # straight helix at either end -- turns by at most TURN_MAX_DEFLECTION.
    lead = (x_turn - direction * (zone + STEP_SIZE), None, -k_in * STEP_SIZE)
    thetas = [math.pi * i / 16 for i in range(17)]
    for _ in range(20):
        pts = [point(t) for t in thetas]
        a_end = pts[-1][2]
        ends = ([(lead[0], eye_y(lead[0]), lead[2])] + pts +
                [(pts[-1][0] - direction * STEP_SIZE, eye_y(pts[-1][0] - direction * STEP_SIZE), a_end + k_out * STEP_SIZE)])
        bad = {i - 1 for i in range(1, len(ends) - 1) if _deflection(ends[i - 1], ends[i], ends[i + 1]) > TURN_MAX_DEFLECTION}
        if not bad:
            break
        refined = [thetas[0]]
        for i in range(1, len(thetas)):
            if i in bad or i - 1 in bad:
                refined.append((thetas[i - 1] + thetas[i]) / 2)
            refined.append(thetas[i])
        thetas = refined
    pts = [point(t) for t in thetas]
    mid = thetas.index(math.pi / 2)
    a_mid = pts[mid][2]
    return pts[1:mid + 1], [(x, y, a - a_mid) for x, y, a in pts[mid + 1:]]


def _deflection(p, q, r):
    # Angle (deg) between moves p->q and q->r in (X, Y, A) space, as Klipper
    # sees the junction at q.
    u = [q[i] - p[i] for i in range(3)]
    v = [r[i] - q[i] for i in range(3)]
    nu, nv = math.sqrt(sum(c * c for c in u)), math.sqrt(sum(c * c for c in v))
    if nu <= 1e-12 or nv <= 1e-12:
        return 0.0
    return math.degrees(math.acos(max(-1.0, min(1.0, sum(a * b for a, b in zip(u, v)) / (nu * nv)))))


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


def helix_pitch(tank_diameter, wind_angle):
    # Axial advance (mm) per mandrel revolution of a helix at wind_angle.
    return (math.pi * tank_diameter) / math.tan(math.radians(wind_angle))


def plan_layup(job, layup):
    dt, lt = job.tank_diameter, job.tank_length
    p = layup.pattern_number
    pitch = helix_pitch(dt, layup.wind_angle)
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


def layup_pitch(job, layup):
    # Axial advance per revolution while winding the layup: one band width for
    # a 90° wind, the helix's for any other.
    return job.bandwidth if layup.hoop else helix_pitch(job.tank_diameter, layup.wind_angle)


def start_sides(job):
    """Which end each layup starts from: 1 at the chuck side (winding toward
    +X first), -1 at the far end. A helical layup ends where it started (every
    circuit goes there and back); a 90° wind ends at the other end, so the
    layups after it start from there."""
    sides, side = [], 1
    for layup in job.layups:
        sides.append(side)
        if layup.hoop:
            side = -side
    return sides


class WindSpeed(NamedTuple):
    """How fast a layup winds the middle of the tank -- its plain helix, clear
    of the turnarounds -- and which speed limit sets that pace."""
    x_speed: float    # carriage speed (mm/s)
    rotation: float   # mandrel speed (deg/min)
    fiber: float      # fiber leaving the eye (mm/s)
    feed: float       # G-code F (mm/min)
    # "rotation" (Max Rotation Speed), "filament" (Max Filament Speed) or
    # "feed" (the MAX_FEED failsafe, before either of them)
    limit: str


def wind_speed(job, layup):
    """The layup's WindSpeed, or None while its speed isn't defined (an angle
    outside 0-90 deg, no tank diameter, or a speed limit not above 0)."""
    if not ((layup.hoop or 0 < layup.wind_angle < 90) and job.tank_diameter > 0 and job.bandwidth > 0
            and job.max_a_speed > 0 and job.max_filament_speed > 0):
        return None
    # Mid-tank the eye holds its distance (no Y motion) and the mandrel turns
    # 360/pitch degrees per mm of X. Per mm of X, a move is then
    # sqrt(1 + (deg/mm)^2) long measured the way F is (A in degrees), and lays
    # sqrt(1 + (r * rad(deg/mm))^2) mm of fiber on the tank. Each limit allows
    # a feed; the lowest sets the pace, exactly as calc_move() decides it for
    # every move.
    deg_per_mm = 360.0 / layup_pitch(job, layup)
    length_per_mm = math.sqrt(1.0 + deg_per_mm * deg_per_mm)
    tow_per_mm = math.sqrt(1.0 + (job.tank_diameter / 2 * math.radians(deg_per_mm)) ** 2)
    feeds = {"rotation": job.max_a_speed * length_per_mm / deg_per_mm,
             "filament": job.max_filament_speed * 60.0 * length_per_mm / tow_per_mm,
             "feed": MAX_FEED}
    limit = min(feeds, key=lambda name: (feeds[name], name != "rotation"))
    feed = feeds[limit]
    x_speed = feed / 60.0 / length_per_mm
    return WindSpeed(x_speed, feed * deg_per_mm / length_per_mm, x_speed * tow_per_mm, feed, limit)


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
    max_a_speed, max_filament = job.max_a_speed, job.max_filament_speed * 60.0
    n_layups = len(job.layups)
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
        # tow are unchanged. The fiber the move lays grows with the radius, so
        # it's estimated at the larger end (a 5 mm step's largest radius is at
        # one of its ends): no piece then runs longer than the estimate allows.
        r_max = max(geom.radius(x0), geom.radius(x1))
        _, duration, _ = calc_move(x0, y0, a0, x1, y1, a1, max_a_speed, r_max, max_filament)
        n = max(1, math.ceil(duration / max_move_time - 1e-9))
        px, py, pa = x0, y0, a0
        for k in range(1, n + 1):
            if k == n:
                qx, qy, qa = x1, y1, a1
            else:
                f = k / n
                qx, qy, qa = x0 + (x1 - x0) * f, y0 + (y1 - y0) * f, a0 + (a1 - a0) * f
            qr = geom.radius(qx)
            feed, piece_duration, tow = calc_move(px, py, pa, qx, qy, qa, max_a_speed, qr, max_filament)
            yield Move(qx, qy, qa, feed, piece_duration, tow, qr, layup_index, circuit, dwell)
            px, py, pa = qx, qy, qa

    def traverse(x, y, a, target_x, pitch, layup_index, circuit):
        # One straight helix run (see traversal_steps), the eye following the tank.
        for x_next, a_delta in traversal_steps(x, target_x, STEP_SIZE, pitch):
            y_next, a_next = eye_y(x_next), a + a_delta
            yield from moves(x, y, a, x_next, y_next, a_next, layup_index, circuit, False)
            x, y, a = x_next, y_next, a_next

    def curve(x, y, a, points, layup_index, circuit):
        # Part of a turnaround curve: (x, y, a relative to `a`) points.
        a0 = a
        for qx, qy, qa in points:
            yield from moves(x, y, a, qx, qy, a0 + qa, layup_index, circuit, False)
            x, y, a = qx, qy, a0 + qa

    def slope(layup):
        return 360.0 / plan_layup(job, layup).pitch  # deg of rotation per mm of X

    # A layup turns around the same way, at each end, again and again (its
    # dwells take only a few distinct values): each curve is worked out once.
    curves = {}

    def turnaround(*args):
        if args not in curves:
            curves[args] = turnaround_curve(*args, eye_y)
        return curves[args]

    # The wind starts at wind_start_x, so its very first pass runs from there
    # (not from the tank's end) toward +X. Every circuit still turns the mandrel
    # by exactly the same amount, so this only shifts the whole program by a
    # constant angle: the pattern is untouched, and the part skipped (the
    # first pass over the chuck-side dome) is one band where every band
    # overlaps anyway. The same holds for a layup that follows a 90° wind: its
    # first pass starts where the hoop ended.
    x, a = job.wind_start_x, 0.0
    y = eye_y(x)
    cycle_number = 0
    pending_head = []  # the run back out of the last turnaround: the next pass begins with it
    lead_in_pitch = helix_pitch(job.tank_diameter, LEAD_IN_ANGLE)  # replaced by each helical layup's own
    for li, (layup, side) in enumerate(zip(job.layups, start_sides(job))):
        yield LayupStart(li)
        if layup.hoop:
            # A 90° wind: from its end of the straight section to the other, in
            # one pass, each turn one band width on from the last. The eye gets
            # to its start at the previous layup's angle -- over the dome, the
            # fiber is already lying at that angle (see LEAD_IN_ANGLE when no
            # helical layup precedes it).
            first, last = job.hoop_span
            start, end = (first, last) if side == 1 else (last, first)
            for ev in traverse(x, y, a, start, lead_in_pitch, li, 0):
                yield ev
                x, y, a = ev.x, ev.y, ev.a
            for ev in traverse(x, y, a, end, job.bandwidth, li, 0):
                yield ev
                x, y, a = ev.x, ev.y, ev.a
            cycle_number += 1
            yield CycleComplete(cycle_number, li, a)
            continue
        plan = plan_layup(job, layup)
        lead_in_pitch = plan.pitch
        k = 360.0 / plan.pitch
        n_strands = layup.pattern_number
        # The next layup's helix, which this layup's last turnaround runs out
        # onto -- none at the end of the program, nor before a 90° wind, whose
        # run onto the straight section isn't a helical pass.
        following = job.layups[li + 1] if li < n_layups - 1 else None
        k_next = slope(following) if following is not None and not following.hoop else None
        for i in range(plan.total_circuits):
            extra = plan.extra_after(i, n_strands)
            # Each circuit goes out from the layup's starting end and back
            # (`side`: the chuck side, or the far end after a 90° wind -- the
            # pattern is then simply mirrored). See
            # compute_turnaround_balance_offset: the outbound turnaround always
            # gets the exact same fixed offset (`right_offset`), every circuit,
            # so the return traversal's own pattern stays in step; the
            # return-side turnaround absorbs whatever's left of this circuit's
            # `extra`, exactly as it always has to for the outbound traversal's
            # pattern to land correctly next circuit.
            for direction in (side, -side):
                x_turn = x_end if direction == 1 else x_start
                dwell_amount = layup.turnaround_angle + (plan.right_offset if direction == side else (extra - plan.right_offset))
                # The run back out of the previous turnaround, then the helix
                # up to this end's turnaround zone.
                for ev in curve(x, y, a, pending_head, li, i):
                    yield ev
                    x, y, a = ev.x, ev.y, ev.a
                # A pass that starts closer to the end than the zone (after a
                # 90° wind) turns around over what's left.
                zone = min(job.turnaround_zone, abs(x_turn - x))
                for ev in traverse(x, y, a, x_turn - direction * zone, plan.pitch, li, i):
                    yield ev
                    x, y, a = ev.x, ev.y, ev.a
                last_pass = i == plan.total_circuits - 1 and direction == -side
                k_out = (k_next if last_pass else k) if zone > 0 else None
                tail, head = turnaround(x_turn, direction, zone, k, k_out if k_out is not None else k, dwell_amount)
                if zone <= 0:
                    # No zone: the whole turnaround is one rotation at the end.
                    a_next = a + tail[0][2]
                    yield from moves(x, y, a, x, y, a_next, li, i, True)
                    a = a_next
                else:
                    for ev in curve(x, y, a, tail, li, i):
                        yield ev
                        x, y, a = ev.x, ev.y, ev.a
                    if k_out is None:
                        # Nothing runs out of this turnaround: the dwell share of
                        # its second half is turned at the end instead, along
                        # the curve's own direction there (pure rotation), so
                        # without a kink. (Its helix share, winding back out of
                        # the zone, has nowhere to go.)
                        a_next = a + head[-1][2] - zone * k
                        yield from moves(x, y, a, x, y, a_next, li, i, True)
                        a, head = a_next, []
                pending_head = head
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
    # One strand per pattern slot; a 90° wind's single pass is one strand.
    strands = [1 if l.hoop else l.pattern_number for l in job.layups]
    results = [LayupResult(strand_runs=[[] for _ in range(n)]) for n in strands]
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
        first_cycle = ev.circuit < strands[ev.layup]
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
    # A 90° wind covers only the straight section, so it's drawn band by band.
    covered = tuple(i for i in range(point.layup)
                    if not job.layups[i].hoop and plan_layup(job, job.layups[i]).coverage >= 0.995)
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


def _limited_feed(dx, dy, da, r, max_a_speed, max_filament):
    # G-code F is the speed along the whole (X, Y, A) move, so the mandrel
    # turns at F * |da| / length and the fiber leaves the eye at F * tow /
    # length (tow laid at radius r). The largest whole F that keeps both at or
    # below their limits, and F itself at or below MAX_FEED: rounded down, so
    # no move -- however it was split or rounded -- ever exceeds any of them,
    # and each still runs within a hair of the one that governs it. A move
    # without rotation runs at Max Rotation Speed's rate along its length.
    da = abs(da)
    length = math.sqrt(dx * dx + dy * dy + da * da)
    feed = max_a_speed if da <= 0 else max_a_speed * length / da
    tow = math.sqrt(dx * dx + (r * math.radians(da)) ** 2)
    if tow > 0:
        feed = min(feed, max_filament * length / tow)
    return max(1, math.floor(min(feed, MAX_FEED)))


def _travel_feed(job):
    # Moves that aren't part of the wind (getting to its start) lay no fiber:
    # Max Rotation Speed's rate, within MAX_FEED -- so any rotation they do
    # involve can't exceed the limit either.
    return _limited_feed(0.0, 0.0, 0.0, 0.0, job.max_a_speed, math.inf)


def _split_travel(start, end, max_distance):
    # Evenly spaced stops from `start` to `end`, none further apart than
    # max_distance (the last one is `end` itself).
    n = max(1, math.ceil(abs(end - start) / max_distance - 1e-9))
    return [start + (end - start) * k / n for k in range(1, n + 1)]


def _write_move_to_start(out, job, x, y, status, message, angle=0.0):
    # After homing, bring the eye to (x, y) without crossing the tank: pull it
    # fully back first (Y0 is the far end of its travel, away from the tank;
    # G28 normally leaves it there already), travel along X, and only then
    # move in to winding distance. Then PAUSE, so the fiber can be attached
    # there; winding resumes from the machine. Assumes G28 leaves the carriage
    # at X0 Y0, the app's machine origin. Travel runs at the travel feed (see
    # _travel_feed), split like every other move so none takes longer than
    # Max Move Time.
    feed = _travel_feed(job)
    max_distance = feed / 60.0 * job.max_move_time
    out.write("; --- MOVE TO WIND START ---\n")
    out.write(f"G1 Y{0.0:.3f} F{feed}\n")
    for xi in _split_travel(0.0, x, max_distance):
        out.write(f"G1 X{xi:.3f} F{feed}\n")
    for yi in _split_travel(0.0, y, max_distance):
        out.write(f"G1 Y{yi:.3f} F{feed}\n")
    out.write(f"; {message}\n")
    out.write(_say(status, message))
    out.write("PAUSE\n")
    # The mandrel may have been turned by hand while attaching the fiber: its
    # angle at resume is declared to be `angle` (the wind's zero, or the A of
    # the point a partial program continues from), so the first move doesn't
    # turn it back.
    out.write(_set_a(angle) + "\n")


def write_gcode(out, job, start_gcode="", end_gcode="", resume=None, extra_header=(), on_progress=None):
    """Writes the program for `job` to the text stream `out`; the job must pass
    validate(). With a Resume, writes a partial program instead: the same
    moves as the complete one from resume.point on, in the same A frame, so it
    continues an interrupted wind seamlessly (raises PointError for a point
    that doesn't exist). `extra_header` adds (key, value) pairs to the settings
    header, e.g. the material estimate the file was made with. `on_progress`,
    if given, is called every few thousand moves (and at the end) with the
    winding time written so far, in seconds -- to show how far a long file
    has got; anything it raises stops the writing."""
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
            _write_move_to_start(out, job, x, y, "Attach the fiber, then resume",
                                 "Attach the fiber here, then resume on the machine to start winding")
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
            _write_move_to_start(out, job, x, y, "Reattach the fiber, then resume",
                                 "Reattach the fiber here, then resume on the machine to continue winding", angle)
        else:
            # The eye is already at the point; the mandrel's angle there is
            # declared to be the program's A at the point.
            out.write(_set_a(angle) + "\n")
    if start_gcode: out.write(start_gcode + "\n")
    # No rotation is planned here, and at the travel feed any rotation the
    # machine does need (e.g. an A axis not homed to 0) can't be faster.
    out.write(f"G1 X{x:.3f} Y{y:.3f} A{angle:.3f} F{_travel_feed(job)}\n")
    if resume is not None:
        # That layup's start has been walked past; announce the cycle it
        # continues in instead.
        out.write(f"; LAYUP_START:{resume.point.layup + 1}\n")
    _write_events(out, job, events, x, y, angle, a_offset, on_progress,
                  first_cycle=(resume.point.layup, resume.point.cycle + 1) if resume is not None else None)
    if end_gcode: out.write(end_gcode + "\n")


PROGRESS_EVERY = 2000  # moves between write_gcode's progress reports
LAYER_UPDATE_SECONDS = 1.0  # winding time between updates of the machine's layer display (the angle)


def _write_events(out, job, events, x, y, angle, a_offset, on_progress=None, first_cycle=None):
    # The machine executes the coordinates as written (3 decimals), so feed
    # rates are computed from those -- relative to the previous written
    # position -- not from the unrounded ones.
    #
    # Each cycle is held back until it's complete: its status lines come
    # first, and the layer display's total among them is the angle the cycle
    # ends at (see _cycle_status) -- known only once it has been wound. A
    # partial program starts inside `first_cycle` (layup index, cycle number
    # within the layup).
    wx, wy, wa = float(f"{x:.3f}"), float(f"{y:.3f}"), float(f"{angle:.3f}")
    max_a_speed, max_filament = job.max_a_speed, job.max_filament_speed * 60.0
    radius = _tank_geometry(job).radius
    n_layups = len(job.layups)
    # The program-wide cycle count before each layup: a CycleComplete's number
    # minus this is the cycle's number within its layup.
    layup_starts = list(itertools.accumulate((layup.passes for layup in job.layups), initial=0))
    written, n_moves = 0.0, 0
    held = []  # lines of the cycle being wound
    cycle = (*first_cycle, wa) if first_cycle else None  # (layup, number, angle it starts at)
    since_update = 0.0
    for ev in events:
        if type(ev) is Move:
            written += ev.duration
            n_moves += 1
            if on_progress is not None and n_moves % PROGRESS_EVERY == 0:
                on_progress(written)
            gx, gy, ga = f"{ev.x:.3f}", f"{ev.y:.3f}", f"{(ev.a - a_offset):.3f}"
            nx, ny, na = float(gx), float(gy), float(ga)
            # The fiber is laid at the radius where the written move ends.
            feed = _limited_feed(nx - wx, ny - wy, na - wa, radius(nx), max_a_speed, max_filament)
            line = f"G1 X{gx} Y{gy} A{ga} F{feed}\n"
            if cycle is None:
                out.write(line)
            else:
                held.append(line)
                since_update += ev.duration
                if since_update >= LAYER_UPDATE_SECONDS:
                    held.append(f"SET_PRINT_STATS_INFO CURRENT_LAYER={round(na)}\n")
                    since_update = 0.0
            wx, wy, wa = nx, ny, na
        elif type(ev) is CycleComplete:
            if cycle is not None:
                out.write(_cycle_status(job, cycle[0], cycle[1], ev.a - a_offset, cycle[2]))
                out.writelines(held)
                held, cycle = [], None
            out.write(f"; CYCLE_COMPLETE:{ev.number}\n")
            # Reset the firmware's A-axis position to 0 without moving, so the
            # accumulated rotation over a long wind never approaches the axis's
            # +/-9999999 limit. Subsequent A values are written relative to this.
            out.write("G92 A0\n")
            a_offset, wa = ev.a, 0.0
            number = ev.number - layup_starts[ev.layup]
            if number < job.layups[ev.layup].passes:
                cycle, since_update = (ev.layup, number + 1, 0.0), 0.0
            elif ev.layup == n_layups - 1:
                layups = f"{n_layups} layup{'s' if n_layups != 1 else ''}"
                out.write(_say("Wind complete", f"Wind complete: {layups}, {job.total_cycles} cycles"))
        else:
            if ev.index > 0 and job.pause_after_layup:
                # The pattern changes here: stop so the fiber can be checked
                # (e.g. for slipping) before the next layup starts.
                out.write(f"; Layup {ev.index} complete - check the fiber, then resume on the machine\n")
                out.write(_say(f"Layup {ev.index}/{n_layups} done - check the fiber, then resume",
                               f"Layup {ev.index}/{n_layups} complete - check the fiber, then resume. "
                               f"Next: layup {ev.index + 1}/{n_layups} ({_layup_description(job.layups[ev.index])})"))
                out.write("PAUSE\n")
            out.write(f"; LAYUP_START:{ev.index + 1}\n")
            cycle, since_update = (ev.index, 1, 0.0), 0.0
    if cycle is not None:  # not ended by a CycleComplete (every program's cycles are)
        out.write(_cycle_status(job, cycle[0], cycle[1], wa, cycle[2]))
        out.writelines(held)
    if on_progress is not None:
        on_progress(written)


# --- Progress on the machine ---
# The program tells the machine where the wind is, with commands Klipper and
# Mainsail's standard config (mainsail.cfg) provide. SET_PRINT_STATS_INFO,
# built into Klipper, drives Mainsail's "Layer x of y", here the mandrel angle
# A within the cycle out of the angle the cycle ends at (A restarts at 0 with
# every cycle). M117 sets the status line -- layup and cycle -- ([display_status])
# and RESPOND prints to the console ([respond]). Klipper-for-CNC aborts the file on any
# command it doesn't know, so these three are the only ones used. Klipper
# reads a little ahead of the motion, so each message shows up to ~2 s before
# the machine gets there.

def _machine_text(text):
    # Plain printable ASCII, without what would end a message early: ';' starts
    # a comment, '#' and '*' end a command's arguments, and the quotes would
    # end RESPOND's MSG.
    text = text.replace("°", " deg")
    return " ".join("".join(c for c in text if " " <= c <= "~" and c not in ";#*\"'").split())


def _say(status, console=None):
    # A message on Mainsail's status line, and (in full) in the console.
    return f"M117 {_machine_text(status)}\nRESPOND MSG=\"{_machine_text(console or status)}\"\n"


def _layup_description(layup):
    return "90 deg wind, straight section" if layup.hoop else f"{layup.wind_angle:g} deg, pattern {layup.pattern_number}"


def _cycle_status(job, layup_index, cycle, final_angle, start_angle=0.0):
    # Written where a cycle starts (or a partial program continues inside
    # it). The layer display shows the mandrel angle in whole degrees: from
    # start_angle -- updated every LAYER_UPDATE_SECONDS of winding -- out of
    # final_angle, where the cycle ends. Klipper only accepts whole numbers
    # there, and a changed total restarts the count before the current value
    # applies.
    layup, n = job.layups[layup_index], len(job.layups)
    angle = "90 deg wind" if layup.hoop else f"{layup.wind_angle:g} deg"
    total = max(1, round(final_angle))
    return (f"SET_PRINT_STATS_INFO TOTAL_LAYER={total} CURRENT_LAYER={min(total, round(start_angle))}\n"
            + _say(f"Layup {layup_index + 1}/{n} - Cycle {cycle}/{layup.passes} - {angle}",
                   f"Starting cycle {cycle}/{layup.passes} of layup {layup_index + 1}/{n} "
                   f"({_layup_description(layup)})"))
