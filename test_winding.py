"""Headless tests for the winding core (no Tk needed): python -m unittest -v"""
import hashlib
import io
import math
import unittest
from dataclasses import replace

import winding
from winding import Layup, WindingJob, Move, CycleComplete, LayupStart


def gcode(job):
    buf = io.StringIO()
    winding.write_gcode(buf, job)
    return buf.getvalue().splitlines()


def body(lines):
    # Everything after the settings header, minus the per-layup markers.
    return [ln for ln in lines[lines.index("; -----------------------") + 1:] if not ln.startswith("; LAYUP_START")]


def parse_header(lines):
    # Same "; key: value" parsing the G-code viewer uses.
    settings = {}
    for ln in lines[1:lines.index("; -----------------------")]:
        key, value = ln[1:].split(":", 1)
        settings[key.strip()] = value.strip()
    return settings


def crossings(job):
    """{(layup, circuit): angle} at which each circuit's forward and return pass
    cross the middle of the tank. Measured there, rather than at the circuit's
    first move, so turnaround zones and blending don't blur the comparison."""
    x_mid = job.chuck_offset + job.tank_length / 2
    fwd, ret, prev = {}, {}, None
    for ev in winding.iter_program(job):
        if type(ev) is not Move:
            continue
        if prev is not None and not ev.dwell and (prev.x - x_mid) * (ev.x - x_mid) <= 0:
            t = (x_mid - prev.x) / (ev.x - prev.x)
            (fwd if ev.x > prev.x else ret)[(ev.layup, ev.circuit)] = prev.a + t * (ev.a - prev.a)
        prev = ev
    return fwd, ret


def end_rotations(job, layup_index=0):
    """Rotation spent at the far (right) and chuck-side (left) end on each
    circuit of one layup, beyond the plain helix: from one mid-tank crossing to
    the next, minus the helix rotation over the tank length in between."""
    fwd, ret = crossings(job)
    helix = job.tank_length / winding.plan_layup(job, job.layups[layup_index]).pitch * 360.0
    keys = sorted(k for k in fwd if k[0] == layup_index)
    right = [ret[k] - fwd[k] - helix for k in keys]
    left = [fwd[b] - ret[a] - helix for a, b in zip(keys, keys[1:])]
    return right, left


def assert_perfect_grid(case, angles, n_bands, msg=""):
    # The bands' angles, taken around the tank, must be exactly 360/n_bands apart.
    a = sorted(x % 360.0 for x in angles)
    gaps = [b - x for x, b in zip(a, a[1:])] + [a[0] + 360.0 - a[-1]]
    case.assertEqual(len(a), n_bands, msg)
    case.assertLess(max(abs(g - 360.0 / n_bands) for g in gaps), 1e-6, msg)


MULTI = WindingJob(layups=(
    Layup(passes=3, pattern_number=3, wind_angle=45.0, turnaround_angle=270.0),
    Layup(passes=2, pattern_number=5, wind_angle=70.0, turnaround_angle=90.0),
    Layup(passes=2, pattern_number=1, wind_angle=20.0, turnaround_angle=180.0),
))

# A layup setting: wind over the whole dome, turning at the tank's very ends.
FULL_DOME = dict(bulkhead_auto=False, bulkhead_angle=0.0)


def legacy_job(tank_length, layups=(Layup(),), **settings):
    """The job a file from before the straight length and the bulkhead angle
    restores to: its end-to-end `tank_length`, every layup over the whole dome."""
    header = {"tank_length": str(tank_length), "end_cap_type": settings.get("end_cap_type", "Round"),
              "tank_diameter": str(settings.get("tank_diameter", 200.0)),
              "end_cap_diameter": str(settings.get("end_cap_diameter", 50.0))}
    return WindingJob(straight_length=winding.LEGACY_VALUES["straight_length"](header),
                      layups=tuple(replace(layup, **FULL_DOME) for layup in layups), **settings)


# The Excel generator's "Transcendence Model Tank PN0003" helical layers, on its
# 250 mm tank with a 7 mm tow: 1480 mm helical traverse and the sheet's minimum
# turnaround of 2 x (90 - angle) at each end.
def excel_layer(angle, pattern, cycles, zone=0.0):
    return WindingJob(straight_length=1480.0, tank_diameter=250.0, end_cap_type="Flat", bandwidth=7.0,
                      turnaround_zone=zone, turnaround_zone_auto=False,
                      layups=(Layup(passes=cycles, pattern_number=pattern, wind_angle=angle,
                                    turnaround_angle=2 * (90 - angle)),))


class GoldenMaster(unittest.TestCase):
    """Pins the exact G-code body (everything after the settings header) so an
    unintended change to the motion shows up. Regenerate these deliberately,
    and only together with a change that is meant to alter the output. (Last
    regenerated for the Additional X-Axis Travel's default of 0. Restored the
    way a file from before the straight length and the Bulkhead Angle is --
    over the whole dome -- every body is exactly the one before; the
    "legacy_*" cases pin that.)"""
    CASES = {
        "default": (WindingJob(), 20354, "54ac4292e760f9c3fda3d900b86bb0e51f79378a92f4bc1b99ab7ce24d1d58ac"),
        "legacy_default": (legacy_job(1000.0, layups=(Layup(x_extension=30.0),)), 22933,
                           "1e1b0a6609df329f3fa76f4c1246653f474facd0aa27b186d38cff97df83d10a"),
        "legacy_no_x_extension": (legacy_job(1000.0, layups=(Layup(x_extension=0.0),)), 19244,
                                  "cf1cabb300f9de26e94f909550379ffc041e9838582a8e2a193aa7a97588a2fb"),
        "legacy_hoop_between_helicals": (
            legacy_job(1200.0, tank_diameter=220.0, bandwidth=6.0, max_move_time=1.0,
                       layups=(Layup(passes=2, pattern_number=3, wind_angle=40.0, turnaround_angle=200.0,
                                     x_extension=30.0),
                               Layup(hoop=True, x_extension=30.0),
                               Layup(passes=1, pattern_number=5, wind_angle=25.0, turnaround_angle=160.0,
                                     x_extension=30.0))),
            11761, "00437c24c90ac8161d34c2644380c55dcb887c198ab4b228bf122d6614418463"),
        # Flat end caps have no bulkhead: exactly as before the X travel and the bulkhead existed.
        "flat_p1_nohome": (
            WindingJob(end_cap_type="Flat", home_before_wind=False,
                       layups=(Layup(passes=4, pattern_number=1, wind_angle=60.0, turnaround_angle=180.0),)),
            2217, "596e0e10cb90eb4ef9b78768bcf977353269bcbc00b4ba9d826b646e8fb5856b"),
        "round_p5_steep": (
            WindingJob(straight_length=800.0, bandwidth=8.0,
                       layups=(Layup(passes=2, pattern_number=5, wind_angle=70.0, turnaround_angle=90.0),)),
            4604, "b14c7176c7586d47577b5d06458816c8f67987496cb3a12ff86e4832abb4d3ca"),
        "round_small_dwell": (
            WindingJob(straight_length=480.0, tank_diameter=160.0, end_cap_diameter=40.0, eye_width=35.0,
                       min_spacing=6.0, max_surface_speed=150.0,
                       layups=(Layup(passes=3, pattern_number=2, wind_angle=30.0, turnaround_angle=10.0),)),
            3341, "ffbc189f48536f2ab82d8cf1784e129ed9d6a7587f1516474c53dddcc104053b"),
        "excel_style_zone": (
            WindingJob(straight_length=1400.0, tank_diameter=250.0, bandwidth=7.0, turnaround_zone=80.0,
                       turnaround_zone_auto=False,
                       layups=(Layup(passes=3, pattern_number=5, wind_angle=12.0, turnaround_angle=156.0),
                               Layup(passes=2, pattern_number=7, wind_angle=54.0, turnaround_angle=72.0))),
            27450, "d2fe19dfd2ad50f4331a5cdeab05a6a218b317925b138bdedae4b792b00fefd9"),
        # A 90° wind between two helical layups: the first layup's last
        # turnaround running on to the hoop's start, the hoop pass, and the
        # next layup turning around at the far end first.
        "hoop_between_helicals": (
            WindingJob(straight_length=1000.0, tank_diameter=220.0, bandwidth=6.0, max_move_time=1.0,
                       layups=(Layup(passes=2, pattern_number=3, wind_angle=40.0, turnaround_angle=200.0),
                               Layup(hoop=True),
                               Layup(passes=1, pattern_number=5, wind_angle=25.0, turnaround_angle=160.0))),
            9285, "daaedf5115a7ba14e2ae39b45c987549c87a8115bbfe57b2f205770927e43c0b"),
        # A double 90° wind (there, a pause, and back), and a layup turning at
        # a Bulkhead Angle of its own.
        "double_hoop_custom_bulkhead": (
            WindingJob(straight_length=700.0, max_move_time=1.0,
                       layups=(Layup(passes=1, pattern_number=3, wind_angle=40.0, bulkhead_auto=False,
                                     bulkhead_angle=60.0),
                               Layup(hoop=True, double=True),
                               Layup(passes=1, pattern_number=2, wind_angle=25.0))),
            4330, "ea15927bf7c547cc0f61a6cd8bfdc9898995ff80faa28fd0103b36febe95b87b"),
    }

    def test_output_unchanged(self):
        for name, (job, n_lines, digest) in self.CASES.items():
            with self.subTest(name):
                lines = body(gcode(job))
                self.assertEqual(len(lines), n_lines)
                self.assertEqual(hashlib.sha256("\n".join(lines).encode()).hexdigest(), digest)


class PatternAlignment(unittest.TestCase):
    def test_bands_tile_the_tank_exactly(self):
        # Every layup's forward AND return passes must land on a perfect grid:
        # passes x pattern_number bands, evenly spaced all the way around.
        fwd, ret = crossings(MULTI)
        for li, layup in enumerate(MULTI.layups):
            n = layup.passes * layup.pattern_number
            with self.subTest(layup=li + 1):
                assert_perfect_grid(self, [a for k, a in fwd.items() if k[0] == li], n, "forward")
                assert_perfect_grid(self, [a for k, a in ret.items() if k[0] == li], n, "return")

    def test_circuits_step_by_the_skip(self):
        # Within a cycle each circuit moves `skip` pattern slots on; after a
        # cycle's last circuit it also moves by the cycle shift.
        fwd, _ = crossings(MULTI)
        for li, layup in enumerate(MULTI.layups):
            plan = winding.plan_layup(MULTI, layup)
            n = layup.pattern_number
            for i in range(1, plan.total_circuits):
                target = plan.skip * 360.0 / n + (plan.shift_degrees if i % n == 0 else 0.0)
                step = (fwd[(li, i)] - fwd[(li, i - 1)] - target) % 360.0
                self.assertAlmostEqual(min(step, 360.0 - step), 0.0, places=6, msg=f"layup {li + 1}, circuit {i}")

    def test_skip_matches_the_excel_sheet(self):
        # The sheet's "degrees per cycle" is the next multiple of 360/p above its
        # minimum circuit rotation; on the sheet's own layers that is 9 slots for
        # 12 deg / pattern 5 and 16 slots for 20 deg / pattern 7.
        self.assertEqual(winding.plan_layup(excel_layer(12, 5, 23), excel_layer(12, 5, 23).layups[0]).skip, 9)
        self.assertEqual(winding.plan_layup(excel_layer(20, 7, 16), excel_layer(20, 7, 16).layups[0]).skip, 16)

    def test_skip_is_coprime_so_every_slot_is_visited(self):
        # With a composite pattern number, a skip sharing a factor with it would
        # keep revisiting a subset of the slots.
        for p in (4, 6, 8, 9, 10, 12):
            for base in (100.0, 359.0, 1234.5, 5000.0):
                k = winding.compute_pattern_skip(base, p, shift=1.0)
                self.assertEqual(math.gcd(k, p), 1)
                self.assertGreaterEqual(k * 360.0 / p, base)
        job = excel_layer(35, 6, 12)
        fwd, _ = crossings(job)
        first_cycle_slots = {round((fwd[(0, i)] - fwd[(0, 0)]) % 360.0 / 60.0) % 6 for i in range(6)}
        self.assertEqual(first_cycle_slots, set(range(6)))

    def test_extra_rotation_stays_below_two_slots(self):
        # Rounding to ANY slot (the Excel method) instead of only the adjacent one
        # keeps the extra turnaround rotation under 2 x 360/p for a prime p (the
        # old method added up to a full turn).
        for angle, p in ((12, 5), (20, 7), (54, 7), (45, 3), (60, 2)):
            job = excel_layer(angle, p, 10)
            plan = winding.plan_layup(job, job.layups[0])
            self.assertGreaterEqual(plan.extra_within, 0.0)
            self.assertLess(plan.extra_within, 2 * 360.0 / p)

    def test_both_ends_get_the_same_turnaround(self):
        # The far end's turnaround is identical on every circuit, the chuck end's
        # stays within one cycle shift of it, and over the layup both ends get
        # the same total rotation -- with the sheet's small minimum turnarounds,
        # where the old method piled thousands of degrees onto the chuck end.
        for angle, p, cycles in ((12, 5, 23), (20, 7, 16), (54, 7, 10)):
            for zone in (0.0, 80.0):
                with self.subTest(angle=angle, zone=zone):
                    job = excel_layer(angle, p, cycles, zone)
                    plan = winding.plan_layup(job, job.layups[0])
                    right, left = end_rotations(job)
                    self.assertLess(max(right) - min(right), 1e-6)
                    self.assertLess(max(abs(l - right[0]) for l in left), plan.shift_degrees + 1e-6)
                    # `left` has one entry fewer (the last circuit's chuck-end turn
                    # isn't followed by another crossing): compare like with like.
                    self.assertLess(abs(sum(left) - sum(right[:-1])), plan.shift_degrees + 1e-6)

    def test_full_coverage_cycles_close_the_pattern(self):
        # With "Cycles for Full Coverage" cycles, bands overlap evenly (>= 100 %)
        # and the pattern closes exactly on itself.
        for angle, p in ((12, 5), (20, 7), (54, 7), (45, 3)):
            cycles = math.ceil(winding.compute_cycles_for_full_coverage(250.0, angle, 7.0, p))
            job = excel_layer(angle, p, cycles)
            plan = winding.plan_layup(job, job.layups[0])
            self.assertGreaterEqual(plan.coverage, 1.0)
            self.assertAlmostEqual(plan.shift_degrees * cycles, 360.0 / p, places=9)


class AutoCycles(unittest.TestCase):
    def test_auto_layup_winds_exactly_full_coverage(self):
        # The fewest whole cycles that reach 100 %: one cycle fewer would leave gaps.
        for angle, p in ((12, 5), (20, 7), (45, 3), (54, 7), (70, 5)):
            job = WindingJob(tank_diameter=250.0, bandwidth=7.0,
                             layups=(Layup(passes=1, pattern_number=p, wind_angle=angle, auto_cycles=True),))
            layup = job.layups[0]
            needed = winding.compute_cycles_for_full_coverage(250.0, angle, 7.0, p)
            self.assertEqual(layup.passes, math.ceil(needed))
            self.assertGreaterEqual(winding.plan_layup(job, layup).coverage, 1.0)
            self.assertLess((layup.passes - 1) / needed, 1.0)

    def test_auto_follows_every_input_and_custom_stays_put(self):
        auto, custom = Layup(passes=7, auto_cycles=True), Layup(passes=7)
        base = WindingJob(layups=(auto, custom))
        for changed in (dict(tank_diameter=300.0), dict(bandwidth=3.0)):
            job = WindingJob(**changed, layups=(auto, custom))
            self.assertNotEqual(job.layups[0].passes, base.layups[0].passes, changed)
            self.assertEqual(job.layups[1].passes, 7)
        steeper = WindingJob(layups=(Layup(passes=7, wind_angle=70.0, auto_cycles=True),))
        denser = WindingJob(layups=(Layup(passes=7, pattern_number=6, auto_cycles=True),))
        self.assertNotEqual(steeper.layups[0].passes, base.layups[0].passes)
        self.assertNotEqual(denser.layups[0].passes, base.layups[0].passes)

    def test_uncomputable_auto_is_left_to_validation(self):
        job = WindingJob(layups=(Layup(passes=4, wind_angle=90.0, auto_cycles=True),))
        self.assertEqual(job.layups[0].passes, 4)
        self.assertTrue(winding.geometry_errors(job))

    def test_auto_flag_round_trips_and_old_files_stay_custom(self):
        job = WindingJob(layups=(Layup(auto_cycles=True), Layup(passes=12, wind_angle=60.0)))
        restored = winding.layups_from_header(parse_header(gcode(job)))
        self.assertEqual([l.auto_cycles for l in restored], [True, False])
        self.assertEqual(WindingJob(layups=restored).layups, job.layups)
        # A layup line from before the flag existed keeps its cycle count as
        # written (and, older than the Additional X-Axis Travel, turned at the ends).
        old = {"layups": "1", "layup_1": "passes=12 pattern_number=3 wind_angle=45.0 turnaround_angle=270.0"}
        self.assertEqual(winding.layups_from_header(old), [Layup(passes=12, x_extension=0.0, **FULL_DOME)])


def gcode_moves(lines):
    """(dx, dy, da, feed) of every G1 exactly as the machine runs it, from the
    written coordinates, starting at the machine origin (where G28 leaves it):
    axes a G1 doesn't name keep their position, and A follows the "G92 A..."
    redefinitions."""
    moves, pos = [], [0.0, 0.0, 0.0]
    for ln in lines:
        if ln.startswith("G92"):
            pos[2] = float(ln.split("A", 1)[1])
            continue
        if not ln.startswith("G1"):
            continue
        w = {tok[0]: float(tok[1:]) for tok in ln.split(";")[0].split()[1:]}
        new = [w.get("X", pos[0]), w.get("Y", pos[1]), w.get("A", pos[2])]
        moves.append((new[0] - pos[0], new[1] - pos[1], new[2] - pos[2], w["F"]))
        pos = new
    return moves


# Slow rotation and a near-hoop layup: long moves everywhere, before splitting.
SLOW = WindingJob(max_surface_speed=60.0, turnaround_zone=0.0, turnaround_zone_auto=False, layups=(
    Layup(passes=2, pattern_number=3, wind_angle=45.0, turnaround_angle=270.0),
    Layup(passes=1, pattern_number=1, wind_angle=89.0, turnaround_angle=90.0)))


class MoveSplitting(unittest.TestCase):
    def test_no_move_takes_longer_than_max_move_time(self):
        for limit in (0.25, 0.5, 2.0):
            with self.subTest(limit=limit):
                job = WindingJob(**{k: getattr(SLOW, k) for k in winding.GLOBAL_KEYS if k != "max_move_time"},
                                 max_move_time=limit, layups=SLOW.layups)
                durations = [ev.duration for ev in winding.iter_program(job) if type(ev) is Move]
                self.assertLessEqual(max(durations), limit + 1e-9)
                # ...measured on the file as written, too (length / F).
                for dx, dy, da, f in gcode_moves(gcode(job)):
                    self.assertLessEqual(math.sqrt(dx * dx + dy * dy + da * da) / f * 60.0, limit * 1.01)

    def test_splitting_keeps_the_exact_path_time_and_rotation(self):
        # Every piece lies on its original move's straight line, so the machine
        # traces the same path; total time and final angle are unchanged.
        whole = WindingJob(**{k: getattr(SLOW, k) for k in winding.GLOBAL_KEYS if k != "max_move_time"},
                           max_move_time=1e9, layups=SLOW.layups)
        big = [ev for ev in winding.iter_program(whole) if type(ev) is Move]
        small = [ev for ev in winding.iter_program(SLOW) if type(ev) is Move]
        self.assertGreater(len(small), len(big))
        self.assertAlmostEqual(sum(m.duration for m in small), sum(m.duration for m in big), places=6)
        start = winding.start_position(SLOW)
        prev_end, j = (start[0], start[1], 0.0), 0
        for mv in big:
            while True:
                piece = small[j]
                j += 1
                # Same fraction of the way along the move in X, Y and A.
                fracs = [(p - s) / (e - s) for p, s, e in zip((piece.x, piece.y, piece.a), prev_end, (mv.x, mv.y, mv.a))
                         if abs(e - s) > 1e-9]
                self.assertLess(max(fracs) - min(fracs), 1e-9)
                self.assertEqual((piece.layup, piece.circuit, piece.dwell), (mv.layup, mv.circuit, mv.dwell))
                if abs(piece.a - mv.a) < 1e-9 and abs(piece.x - mv.x) < 1e-9:
                    break
            prev_end = (mv.x, mv.y, mv.a)
        self.assertEqual(j, len(small))

    def test_max_move_time_validation(self):
        job = WindingJob(max_move_time=0.05)
        self.assertIn("Max Move Time must be at least 0.1 s.", winding.geometry_errors(job))


class MaxRotationSpeed(unittest.TestCase):
    def test_no_written_move_turns_faster_than_the_limit(self):
        # For every G1 as the machine will run it: rotation rate = F * |da| / length.
        # Never above Max Rotation Speed.
        for job in (SLOW, MULTI, WindingJob(turnaround_zone=80.0, turnaround_zone_auto=False),
                    excel_layer(12, 5, 4, zone=80.0)):
            with self.subTest(speed=job.max_surface_speed, zone=job.turnaround_zone):
                limit = job.max_a_speed
                moves = [(dx, dy, da, f) for dx, dy, da, f in gcode_moves(gcode(job)) if da]
                rates = [f * abs(da) / math.sqrt(dx * dx + dy * dy + da * da) for dx, dy, da, f in moves]
                self.assertLessEqual(max(rates), limit)
        # (That every move runs at one of the limits: see MaxFilamentSpeed.)

    def test_initial_positioning_move_is_capped_too(self):
        # Its F equals the limit, so any rotation it involves can't exceed it.
        first = next(ln for ln in gcode(SLOW) if ln.startswith("G1"))
        self.assertLessEqual(float(first.rsplit("F", 1)[1]), SLOW.max_a_speed)

    def test_rotation_limit_too_low_to_write(self):
        self.assertIn("Max Rotation Speed is too low for this tank diameter.",
                      winding.geometry_errors(WindingJob(max_surface_speed=0.001)))


def mid_tank_speeds(job, lines):
    """{layup index: (x speed mm/s, F)} of the written move crossing the middle
    of the tank on each layup's first forward pass."""
    x_mid = job.chuck_offset + job.tank_length / 2
    layup, pos, found = -1, [0.0, 0.0, 0.0], {}
    for ln in lines:
        if ln.startswith("; LAYUP_START:"):
            layup = int(ln.split(":")[1]) - 1
        elif ln.startswith("G92"):
            pos[2] = float(ln.split("A", 1)[1])
        elif ln.startswith("G1"):
            w = {tok[0]: float(tok[1:]) for tok in ln.split()[1:]}
            new = [w.get("X", pos[0]), w.get("Y", pos[1]), w.get("A", pos[2])]
            dx, dy, da = (n - p for n, p in zip(new, pos))
            if layup >= 0 and layup not in found and dx > 0 and pos[0] < x_mid <= new[0]:
                found[layup] = (dx / (math.sqrt(dx * dx + dy * dy + da * da) / w["F"] * 60.0), w["F"])
            pos = new
    return found


def wind_moves(job, lines):
    """(dx, dy, da, F, r) of every G1 of the wind itself (from the first layup
    on), exactly as the machine runs it, with r the tank radius at the move's
    end -- where the fiber it lays is measured, as the writer does."""
    radius = winding._tank_geometry(job).radius
    moves, pos, winding_started = [], [0.0, 0.0, 0.0], False
    for ln in lines:
        if ln.startswith("; LAYUP_START"):
            winding_started = True
        elif ln.startswith("G92"):
            pos[2] = float(ln.split("A", 1)[1])
        elif ln.startswith("G1"):
            w = {tok[0]: float(tok[1:]) for tok in ln.split()[1:]}
            new = [w.get("X", pos[0]), w.get("Y", pos[1]), w.get("A", pos[2])]
            if winding_started:
                moves.append((new[0] - pos[0], new[1] - pos[1], new[2] - pos[2], w["F"], radius(new[0])))
            pos = new
    return moves


def rates(move):
    """(rotation deg/min, fiber mm/s) of a written move."""
    dx, dy, da, f, r = move
    length = math.sqrt(dx * dx + dy * dy + da * da)
    tow = math.sqrt(dx * dx + (r * math.radians(da)) ** 2)
    return f * abs(da) / length, f / 60.0 * tow / length


# A tank so thin that Max Rotation Speed alone would exceed the MAX_FEED failsafe.
THIN = WindingJob(tank_diameter=30.0, end_cap_diameter=10.0, chuck_offset=50.0,
                  layups=(Layup(passes=1, pattern_number=1, wind_angle=45.0),))


class MaxFilamentSpeed(unittest.TestCase):
    @staticmethod
    def jobs():
        return (MULTI, excel_layer(12, 5, 4, zone=80.0), SLOW, HOOPS,
                WindingJob(max_filament_speed=120.0, layups=(Layup(passes=2, wind_angle=30.0),
                                                             Layup(passes=1, wind_angle=75.0))))

    def test_no_written_move_lays_fiber_faster_than_the_limit(self):
        for job in self.jobs():
            with self.subTest(filament=job.max_filament_speed, layups=len(job.layups)):
                fiber = [rates(m)[1] for m in wind_moves(job, gcode(job))]
                self.assertLessEqual(max(fiber), job.max_filament_speed * (1 + 1e-9))

    def test_every_move_runs_at_the_limit_it_reaches_first(self):
        # Whichever applies to the move: the mandrel at Max Rotation Speed, the
        # fiber at Max Filament Speed, or F at the failsafe -- within rounding.
        for job in self.jobs() + (THIN,):
            with self.subTest(filament=job.max_filament_speed, layups=len(job.layups)):
                for move in wind_moves(job, gcode(job)):
                    rotation, fiber = rates(move)
                    self.assertTrue(rotation > 0.99 * job.max_a_speed or fiber > 0.99 * job.max_filament_speed
                                    or move[3] > 0.99 * winding.MAX_FEED, move)

    def test_no_feed_above_the_failsafe(self):
        self.assertGreater(THIN.max_a_speed, winding.MAX_FEED)
        lines = gcode(THIN)
        feeds = [f for *_, f in gcode_moves(lines)]
        self.assertEqual(max(feeds), winding.MAX_FEED)
        # The travel to the start lays no fiber: Max Rotation Speed's rate, capped.
        setup = [ln for ln in lines[:lines.index("PAUSE")] if ln.startswith("G1")]
        self.assertTrue(all(float(ln.rsplit("F", 1)[1]) == winding.MAX_FEED for ln in setup))

    def test_it_holds_the_carriage_back_at_low_angles(self):
        # 12 deg on a 250 mm tank: turning at Max Rotation Speed alone would run
        # the carriage at over 1 m/s. Mid-tank the fiber runs at (nearly) Max
        # Filament Speed instead, the carriage at that times cos(12 deg).
        job = excel_layer(12, 5, 4)
        x_speed, _ = mid_tank_speeds(job, gcode(job))[0]
        self.assertGreater(job.max_surface_speed / math.tan(math.radians(12)), 1000.0)
        self.assertAlmostEqual(x_speed, job.max_filament_speed * math.cos(math.radians(12)), delta=0.01 * x_speed)

    def test_wind_speed_names_the_limit_the_program_runs_at(self):
        # wind_speed() predicts every layup's mid-tank speed and limit: the fiber
        # limit wherever turning at Max Rotation Speed would lay fiber faster
        # (220 / sin(angle) > 310 mm/s: 20 and 45 deg), rotation above that.
        speeds = mid_tank_speeds(MULTI, gcode(MULTI))
        for i, layup in enumerate(MULTI.layups):
            ws = winding.wind_speed(MULTI, layup)
            fiber_at_rotation = MULTI.max_surface_speed / math.sin(math.radians(layup.wind_angle))
            with self.subTest(angle=layup.wind_angle):
                self.assertEqual(ws.limit, "filament" if fiber_at_rotation > MULTI.max_filament_speed else "rotation")
                self.assertAlmostEqual(speeds[i][0], ws.x_speed, delta=0.01 * ws.x_speed)
                self.assertAlmostEqual(speeds[i][1], ws.feed, delta=0.01 * ws.feed)
                self.assertAlmostEqual(ws.fiber, ws.x_speed / math.cos(math.radians(layup.wind_angle)))
                limit, reached = ((MULTI.max_filament_speed, ws.fiber) if ws.limit == "filament"
                                  else (MULTI.max_a_speed, ws.rotation))
                self.assertAlmostEqual(reached, limit, delta=1e-6 * limit)

    def test_the_limit_follows_both_settings(self):
        layup = Layup(wind_angle=30.0)
        self.assertEqual(winding.wind_speed(WindingJob(), layup).limit, "filament")
        self.assertEqual(winding.wind_speed(WindingJob(max_filament_speed=500.0), layup).limit, "rotation")
        self.assertEqual(winding.wind_speed(WindingJob(max_filament_speed=500.0, max_surface_speed=400.0), layup).limit,
                         "filament")
        self.assertEqual(winding.wind_speed(THIN, Layup(wind_angle=80.0)).limit, "feed")
        self.assertIsNone(winding.wind_speed(WindingJob(), Layup(wind_angle=90.0)))

    def test_estimated_time_is_the_time_the_file_takes(self):
        # The preview's time comes from the same limited feed rates.
        job = replace(excel_layer(12, 5, 4, zone=80.0), home_before_wind=False)
        wind = gcode_moves(gcode(job))[1:]  # after the move from the origin to the start
        written = sum(math.sqrt(dx * dx + dy * dy + da * da) / f * 60.0 for dx, dy, da, f in wind)
        self.assertAlmostEqual(winding.simulate(job).total_time, written, delta=0.001 * written)

    def test_validation_and_old_files(self):
        self.assertIn("Max Filament Speed must be at least 1 mm/s.",
                      winding.geometry_errors(WindingJob(max_filament_speed=0.5)))
        # It never changes the path, so a file from before it existed simply
        # restores the default.
        self.assertNotIn("max_filament_speed", winding.LEGACY_VALUES)
        self.assertIn(("max_filament_speed", 310.0), winding.settings_items(WindingJob()))
        self.assertNotIn("max_feedrate", winding.GLOBAL_KEYS)


class WindStart(unittest.TestCase):
    def test_auto_start_is_where_the_dome_ends(self):
        job = WindingJob()
        self.assertAlmostEqual(job.wind_start_x, job.chuck_offset + job.dome_length)
        first = next(ev for ev in winding.iter_program(job) if type(ev) is Move)
        self.assertGreater(first.x, job.wind_start_x)  # winds on toward +X from there
        flat = WindingJob(end_cap_type="Flat")
        self.assertEqual(flat.wind_start_x, flat.chuck_offset)  # no dome: straight from the end

    def test_custom_start_and_no_homing(self):
        self.assertEqual(WindingJob(start_x_auto=False, start_x=300.0).wind_start_x, 300.0)
        # Without homing the start setting doesn't apply: the wind starts at the tank's end.
        self.assertEqual(WindingJob(home_before_wind=False, start_x_auto=False, start_x=300.0).wind_start_x, 50.0)

    def test_start_is_validated_only_when_homing(self):
        message = "Start Wind at X must be between"
        self.assertTrue(any(message in e for e in winding.geometry_errors(WindingJob(start_x_auto=False, start_x=10.0))))
        self.assertTrue(any(message in e for e in winding.geometry_errors(WindingJob(start_x_auto=False, start_x=1200.0))))
        self.assertFalse(winding.geometry_errors(WindingJob(home_before_wind=False, start_x_auto=False, start_x=10.0)))

    def test_later_start_keeps_the_pattern(self):
        # Starting part-way only shifts the whole program by a constant angle:
        # every layup still tiles the tank exactly.
        job = WindingJob(start_x_auto=False, start_x=400.0, layups=MULTI.layups)
        fwd, ret = crossings(job)
        for li, layup in enumerate(job.layups):
            n = layup.passes * layup.pattern_number
            assert_perfect_grid(self, [a for k, a in fwd.items() if k[0] == li], n)
            assert_perfect_grid(self, [a for k, a in ret.items() if k[0] == li], n)

    def test_move_to_start_then_pause(self):
        # G28, pull the eye back, travel along X, move in at the start, PAUSE,
        # zero A (the mandrel may be turned by hand), then wind.
        job = WindingJob()
        lines = [ln for ln in gcode(job)[gcode(job).index("; -----------------------") + 1:] if ln]
        pause = lines.index("PAUSE")
        setup = [ln for ln in lines[:pause] if ln.startswith("G1")]
        self.assertEqual(lines[0], "G28")
        self.assertTrue(setup[0].startswith("G1 Y0.000 "))
        x_moves = [ln for ln in setup if " X" in ln]
        y_moves = [ln for ln in setup[1:] if " Y" in ln]
        self.assertTrue(all(" Y" not in ln for ln in x_moves))  # X travel only while pulled back
        self.assertLess(setup.index(x_moves[-1]), setup.index(y_moves[0]))  # moves in only after arriving
        x, y = winding.start_position(job)
        self.assertAlmostEqual(float(x_moves[-1].split()[1][1:]), x, places=3)
        self.assertAlmostEqual(float(y_moves[-1].split()[1][1:]), y, places=3)
        self.assertEqual(lines[pause + 1], "G92 A0")
        self.assertFalse(any(ln == "PAUSE" for ln in gcode(WindingJob(home_before_wind=False))))

    def test_old_files_restore_their_start(self):
        # Files from before the setting existed started at the tank's end.
        settings = {"chuck_offset": "75.0"}
        value = winding.LEGACY_VALUES["start_x"]
        self.assertEqual(value(settings) if callable(value) else value, 75.0)
        self.assertIs(winding.LEGACY_VALUES["start_x_auto"], False)


TWO_LAYUPS = WindingJob(layups=(Layup(passes=2, pattern_number=3), Layup(passes=2, pattern_number=5, wind_angle=70.0)))


def after_first_move(lines, marker):
    # The lines after the first G1 that follows `marker`.
    i = lines.index(marker)
    return lines[next(j for j in range(i, len(lines)) if lines[j].startswith("G1")) + 1:]


def without_angle_updates(lines):
    # The layer display's angle is updated every second of winding counted
    # from where a program starts winding the cycle -- so a partial program
    # updates it on other lines than the complete one; everything else matches.
    return [ln for ln in lines if not ln.startswith("SET_PRINT_STATS_INFO CURRENT_LAYER=")]


def partial(job, point, rehome=False):
    buf = io.StringIO()
    winding.write_gcode(buf, job, resume=winding.Resume(point, rehome))
    return buf.getvalue().splitlines()


class PauseAfterLayup(unittest.TestCase):
    def test_pause_between_layups_only(self):
        lines = gcode(TWO_LAYUPS)
        i = lines.index("; LAYUP_START:2")
        self.assertEqual(lines[i - 1], "PAUSE")
        self.assertEqual(lines[i - 5], "G92 A0")  # after the last cycle's reset, its comment and the message
        self.assertEqual(sum(ln == "PAUSE" for ln in lines), 2)  # plus the one at the wind start
        self.assertNotEqual(lines[-1], "PAUSE")

    def test_can_be_switched_off(self):
        job = WindingJob(pause_after_layup=False, layups=TWO_LAYUPS.layups)
        self.assertEqual(sum(ln == "PAUSE" for ln in gcode(job)), 1)


class PartialProgram(unittest.TestCase):
    def test_from_a_layup_start_is_the_complete_programs_tail(self):
        full, part = gcode(TWO_LAYUPS), partial(TWO_LAYUPS, winding.ProgramPoint(1, 0, 0.0))
        tail = lambda lines: lines[lines.index("; LAYUP_START:2") + 1:]
        self.assertEqual(tail(part), tail(full))
        self.assertNotIn("G28", part)  # the eye is already there
        self.assertNotIn("PAUSE", part)  # the pause this continues from has been had

    def test_from_mid_cycle_continues_in_the_machines_a_frame(self):
        point = winding.ProgramPoint(0, 1, 500.0)
        part = partial(TWO_LAYUPS, point)
        body = part[part.index("; -----------------------") + 2:]
        self.assertEqual(body[0], "G92 A500.000")  # the machine's A there is declared
        location = winding.locate(TWO_LAYUPS, point)
        self.assertEqual(body[1].split()[1:4], [f"X{location.x:.3f}", f"Y{location.y:.3f}", "A500.000"])
        # From there on, the same lines as the complete program.
        full = without_angle_updates(gcode(TWO_LAYUPS))
        rest = without_angle_updates(after_first_move(body, "; LAYUP_START:1"))  # skip the cut-short first move
        start = full.index(rest[0])
        self.assertEqual(full[start:start + len(rest)], rest)
        self.assertEqual(full[start:], rest)

    def test_the_rest_of_the_wind_is_exactly_what_remains(self):
        point = winding.ProgramPoint(1, 1, 250.0)
        location = winding.locate(TWO_LAYUPS, point)
        whole = winding.simulate(TWO_LAYUPS).total_time
        moves = gcode_moves(partial(TWO_LAYUPS, point))
        start = next(i for i, (dx, dy, da, f) in enumerate(moves) if da > 0)  # skip the positioning
        remaining = sum(math.sqrt(dx * dx + dy * dy + da * da) / f * 60 for dx, dy, da, f in moves[start:])
        self.assertAlmostEqual(location.elapsed + remaining, whole, delta=whole * 0.002)

    def test_rehome_homes_x_and_y_moves_there_and_pauses(self):
        point = winding.ProgramPoint(1, 0, 120.0)
        part = partial(TWO_LAYUPS, point, rehome=True)
        body = part[part.index("; -----------------------") + 2:]
        self.assertEqual(body[0], "G28 X Y")  # the mandrel keeps its angle
        pause = body.index("PAUSE")
        self.assertEqual(body[pause + 1], "G92 A120.000")
        location = winding.locate(TWO_LAYUPS, point)
        travel = [ln for ln in body[:pause] if ln.startswith("G1")]
        self.assertTrue(travel[0].startswith("G1 Y0.000"))
        self.assertAlmostEqual(float(travel[-1].split()[1][1:]), location.y, places=3)

    def test_header_records_how_it_was_made(self):
        resume = winding.Resume(winding.ProgramPoint(1, 1, 42.5), rehome=True)
        buf = io.StringIO()
        winding.write_gcode(buf, TWO_LAYUPS, resume=resume)
        self.assertEqual(winding.resume_from_header(parse_header(buf.getvalue().splitlines())), resume)
        self.assertIsNone(winding.resume_from_header(parse_header(gcode(TWO_LAYUPS))))

    def test_points_that_dont_exist(self):
        for point, message in ((winding.ProgramPoint(5, 0, 0.0), "There is no Layup 6."),
                               (winding.ProgramPoint(0, 2, 0.0), "Layup 1 has cycles 1 to 2."),
                               (winding.ProgramPoint(0, 0, -1.0), "can't be negative")):
            with self.assertRaises(winding.PointError) as ctx:
                winding.locate(TWO_LAYUPS, point)
            self.assertIn(message, str(ctx.exception))
        with self.assertRaises(winding.PointError) as ctx:
            winding.locate(TWO_LAYUPS, winding.ProgramPoint(0, 0, 1e6))
        self.assertIn("ends at A", str(ctx.exception))

    def test_progress_shows_what_is_wound(self):
        # Before the point: full-coverage layups as a solid layer, the point's
        # own layup as the bands wound so far.
        job = WindingJob(layups=(Layup(auto_cycles=True), Layup(passes=3, wind_angle=70.0)))
        progress = winding.progress(job, winding.ProgramPoint(1, 1, 100.0))
        self.assertEqual(progress.covered, (0,))
        self.assertEqual(list(progress.runs), [1])
        # A full cycle (3 circuits out and back) plus the start of the next.
        self.assertGreaterEqual(len(progress.runs[1]), 6)


def klipper_junctions(job):
    """Klipper-for-CNC's look-ahead (toolhead.Move.calc_junction) replayed over
    the written program, at its default square_corner_velocity (5 mm/s) and an
    acceleration of 3000: [(junction speed allowed, commanded speed, deflection
    deg)] for every junction between two moves of the same helical layup."""
    scv, accel = 5.0, 3000.0
    jd = scv * scv * (math.sqrt(2) - 1) / accel
    out, prev, layup, pos = [], None, None, [0.0, 0.0, 0.0]
    for ln in gcode(job):
        if ln.startswith("; LAYUP_START:"):
            layup, prev = int(ln.split(":")[1]) - 1, None
        elif ln.startswith("G92"):
            pos[2] = float(ln.split("A", 1)[1])
        elif ln.startswith("G1"):
            w = {tok[0]: float(tok[1:]) for tok in ln.split()[1:]}
            new = [w.get("X", pos[0]), w.get("Y", pos[1]), w.get("A", pos[2])]
            d = [n - p for n, p in zip(new, pos)]
            pos = new
            length = math.sqrt(sum(c * c for c in d))
            if layup is None or job.layups[layup].hoop or length == 0:
                prev = None
                continue
            r, v = [c / length for c in d], w["F"] / 60.0
            if prev is not None:
                pr, plength, pv = prev
                cos_t = -sum(a * b for a, b in zip(r, pr))
                s, c = math.sqrt(max(.5 * (1 - cos_t), 0.)), math.sqrt(max(.5 * (1 + cos_t), 0.))
                v2 = min(v, pv) ** 2
                if 1 - s > 0 and c > 0:
                    v2 = min(v2, s / (1 - s) * jd * accel, min(length, plength) * accel * .5 * s / c)
                out.append((math.sqrt(v2), min(v, pv), math.degrees(math.acos(max(-1., min(1., -cos_t))))))
            prev = (r, length, v)
    return out


class SmoothTurnaround(unittest.TestCase):
    def test_auto_zone_is_the_dome(self):
        job = WindingJob()
        self.assertAlmostEqual(job.turnaround_zone, job.dome_length)
        self.assertEqual(WindingJob(end_cap_type="Flat").turnaround_zone, winding.FLAT_TURNAROUND_ZONE)
        self.assertEqual(WindingJob(turnaround_zone=30.0, turnaround_zone_auto=False).turnaround_zone, 30.0)
        # At most half the tank (only flat end caps can be that short: a dome is at most half).
        self.assertEqual(WindingJob(end_cap_type="Flat", straight_length=80.0).turnaround_zone, 40.0)
        self.assertIs(winding.LEGACY_VALUES["turnaround_zone_auto"], False)  # older files keep their zone

    def test_klipper_takes_every_turnaround_at_speed(self):
        # The old zone met the helix at a 42 deg kink, where Klipper's junction
        # deviation braked the carriage to 12 mm/s. The curve turns at most
        # TURN_MAX_DEFLECTION per move (plus coordinate rounding), so Klipper
        # never has to slow much below the commanded speed anywhere in a layup.
        for job in (WindingJob(layups=(Layup(passes=3),)), MULTI, HOOPS, excel_layer(12, 5, 4, zone=50.0),
                    WindingJob(end_cap_type="Flat", layups=(Layup(passes=2, wind_angle=30.0),))):
            with self.subTest(layups=len(job.layups), zone=job.turnaround_zone):
                junctions = klipper_junctions(job)
                self.assertLess(max(d for *_, d in junctions), winding.TURN_MAX_DEFLECTION + 0.2)
                # (A pass's last, shorter step can land right where a turnaround
                # begins: Klipper takes a bend after a very short move a little
                # slower -- MULTI's 20° layup has one such junction at 96 %.)
                self.assertGreater(min(v / commanded for v, commanded, _ in junctions), 0.95)
        # Without a zone it's the pure rotation at the end: the old full stop.
        self.assertLess(min(v / c for v, c, _ in klipper_junctions(excel_layer(12, 5, 2))), 0.1)

    def test_reversal_at_the_tank_end_inside_the_zone(self):
        job = WindingJob(layups=(Layup(passes=2, x_extension=0.0, **FULL_DOME),))
        x_start, x_end = job.chuck_offset, job.chuck_offset + job.tank_length
        moves = [ev for ev in winding.iter_program(job) if type(ev) is Move]
        xs = [m.x for m in moves]
        self.assertAlmostEqual(max(xs), x_end)
        self.assertAlmostEqual(min(xs), x_start)
        # Turning around only inside the zones: between them the carriage keeps going one way.
        lo, hi = x_start + job.turnaround_zone, x_end - job.turnaround_zone
        for p, q in zip(moves, moves[1:]):
            if lo < p.x < hi and lo < q.x < hi:
                self.assertNotEqual(p.x, q.x)
        # The only pure rotation is the program's very last move (nothing runs out of it).
        n_dwell = sum(m.dwell for m in moves)
        self.assertTrue(all(m.dwell for m in moves[-n_dwell:]) and n_dwell >= 1)

    def test_rotation_per_turnaround_is_unchanged(self):
        # The curve turns the mandrel by exactly what a turnaround always did
        # (the helix in and out of the zone plus the dwell), so the pattern and
        # every point after it are where they were with a pure rotation at the
        # end -- across layup changes too.
        on_the_spot = WindingJob(**{k: getattr(MULTI, k) for k in winding.GLOBAL_KEYS
                                    if k not in ("turnaround_zone", "turnaround_zone_auto")},
                                 turnaround_zone=0.0, turnaround_zone_auto=False, layups=MULTI.layups)
        for curved, spot in zip(crossings(MULTI), crossings(on_the_spot)):
            self.assertEqual(curved.keys(), spot.keys())
            for key in curved:
                self.assertAlmostEqual(curved[key], spot[key], places=6)
        last = lambda job: [ev for ev in winding.iter_program(job) if type(ev) is Move][-1].a
        self.assertAlmostEqual(last(MULTI), last(on_the_spot), places=6)

    def test_zone_validation(self):
        self.assertEqual(winding.geometry_errors(excel_layer(20, 7, 4, zone=740.0)), [])
        self.assertIn("Turnaround Zone must be at most half the tank's whole length.",
                      winding.geometry_errors(excel_layer(20, 7, 4, zone=745.0)))
        self.assertIn("Turnaround Zone can't be negative.", winding.geometry_errors(excel_layer(20, 7, 4, zone=-1.0)))

    def test_preview_runs_still_split_at_each_turnaround(self):
        result = winding.simulate(excel_layer(20, 7, 2, zone=80.0))
        self.assertTrue(all(len(runs) == 2 for runs in result.layups[0].strand_runs))


class AdditionalXTravel(unittest.TestCase):
    """A layup's Additional X-Axis Travel: the eye turns around beyond the
    tank's ends, and the pattern still closes exactly."""

    def test_every_turnaround_reaches_beyond_the_ends(self):
        def visits(xs, x):
            # How often the eye arrives at x (a pure rotation there counts once).
            return sum(1 for i, v in enumerate(xs) if abs(v - x) < 1e-9 and (i == 0 or abs(xs[i - 1] - x) >= 1e-9))
        for extension in (0.0, 30.0, 47.3):
            for zone in (None, 0.0):  # auto (the dome), and turning on the spot
                for bulkhead in (FULL_DOME, {}):  # the whole dome, and the auto Bulkhead Angle
                    job = WindingJob(**({} if zone is None else dict(turnaround_zone=zone, turnaround_zone_auto=False)),
                                     layups=(Layup(passes=2, x_extension=extension, **bulkhead),))
                    with self.subTest(extension=extension, zone=job.turnaround_zone, full_dome=bool(bulkhead)):
                        xs = [ev.x for ev in winding.iter_program(job) if type(ev) is Move]
                        turn_lo, turn_hi = winding.turn_points(job, job.layups[0])
                        if bulkhead:
                            self.assertEqual((turn_lo, turn_hi), (job.chuck_offset, job.chuck_offset + job.tank_length))
                        x_lo, x_hi = turn_lo - extension, turn_hi + extension
                        self.assertAlmostEqual(min(xs), x_lo)
                        self.assertAlmostEqual(max(xs), x_hi)
                        circuits = winding.plan_layup(job, job.layups[0]).total_circuits
                        self.assertEqual(visits(xs, x_hi), circuits)
                        self.assertEqual(visits(xs, x_lo), circuits)

    def test_the_pattern_still_closes_exactly(self):
        for extension in (12.5, 47.3):
            job = WindingJob(layups=tuple(replace(layup, x_extension=extension) for layup in MULTI.layups))
            fwd, ret = crossings(job)
            for li, layup in enumerate(job.layups):
                n = layup.passes * layup.pattern_number
                with self.subTest(extension=extension, layup=li + 1):
                    assert_perfect_grid(self, [a for k, a in fwd.items() if k[0] == li], n, "forward")
                    assert_perfect_grid(self, [a for k, a in ret.items() if k[0] == li], n, "return")

    def test_the_extra_travel_is_wound_as_helix(self):
        # Beyond the plain helix along the tank, each far-end turnaround turns
        # the helix out to the turning point and back, plus its dwell.
        base = excel_layer(20, 7, 4, zone=80.0)
        job = replace(base, layups=(replace(base.layups[0], x_extension=40.0),))
        plan = winding.plan_layup(job, job.layups[0])
        right, _ = end_rotations(job)
        for rotation in right:
            self.assertAlmostEqual(rotation, 2 * 40.0 * 360.0 / plan.pitch + job.layups[0].turnaround_angle
                                   + plan.right_offset, places=6)

    def test_the_eye_keeps_clear_and_moves_smoothly_beyond_the_ends(self):
        for job in (WindingJob(layups=(Layup(passes=1, x_extension=60.0),)),
                    WindingJob(tank_diameter=300.0, end_cap_diameter=80.0, eye_width=35.0,
                               layups=(Layup(passes=1, x_extension=60.0),)),
                    WindingJob(eye_width=4.0, min_spacing=5.0, layups=(Layup(passes=1, x_extension=60.0),))):
            geom = winding._tank_geometry(job)
            eye_y = winding._eye_y_function(job, geom)
            y_base = winding.Y_REFERENCE - job.eye_arm_length
            half = job.eye_width / 2
            with self.subTest(diameter=job.tank_diameter, eye_width=job.eye_width):
                for end, out in ((geom.x_end, 1), (geom.x_start, -1)):
                    # No corner where the eye leaves the tank: the same slope on both sides.
                    h = 1e-4
                    inside = (eye_y(end) - eye_y(end - out * h)) / h
                    beyond = (eye_y(end + out * h) - eye_y(end)) / h
                    self.assertAlmostEqual(inside, beyond, places=2)
                    for u in (i * 0.25 for i in range(1, 241)):
                        y = eye_y(end + out * u)
                        # Clear of the dome while the eye's inner edge is over it...
                        if u < half:
                            self.assertLessEqual(y, y_base - (geom.radius(end + out * (u - half)) + job.min_spacing) + 1e-9)
                        # ...and of the polar opening's boss.
                        self.assertLessEqual(y, y_base - (geom.r_cap + job.min_spacing) + 1e-9)
                    self.assertEqual(eye_y(end + out * 59.0), eye_y(end + out * 60.0))  # levelled off

    def test_klipper_still_takes_the_turnarounds_at_speed(self):
        for job in (WindingJob(layups=(Layup(passes=1, pattern_number=2, wind_angle=12.0),)),
                    WindingJob(layups=(Layup(passes=1, pattern_number=2, wind_angle=60.0, x_extension=60.0),))):
            with self.subTest(angle=job.layups[0].wind_angle, extension=job.layups[0].x_extension):
                junctions = klipper_junctions(job)
                self.assertLess(max(d for *_, d in junctions), winding.TURN_MAX_DEFLECTION + 0.2)
                self.assertGreater(min(v / commanded for v, commanded, _ in junctions), 0.99)

    def test_the_preview_keeps_the_fiber_on_the_tank(self):
        job = WindingJob(layups=(Layup(passes=2, pattern_number=3, x_extension=40.0),))
        x_start, x_end = job.chuck_offset, job.chuck_offset + job.tank_length
        runs = winding.simulate(job).layups[0].strand_runs
        points = [p for strand in runs for run in strand for p in run]
        self.assertTrue(all(x_start <= x <= x_end for x, _, _ in points))
        self.assertAlmostEqual(max(x for x, _, _ in points), x_end)  # wrapped around at the end

    def test_validation_and_machine_limits(self):
        self.assertIn("Additional X-Axis Travel can't be negative.",
                      winding.geometry_errors(WindingJob(layups=(Layup(x_extension=-1.0),))))
        whole_dome = (Layup(x_extension=30.0, **FULL_DOME),)
        self.assertIn("Additional X-Axis Travel would take the carriage to X -10mm, below X0. Reduce it or "
                      "increase Chuck X-Offset.", winding.validate(WindingJob(chuck_offset=20.0, layups=whole_dome)))
        self.assertEqual(winding.validate(WindingJob(chuck_offset=30.0, layups=whole_dome)), [])
        # Turning part-way onto the dome, it reaches less far.
        self.assertEqual(winding.validate(WindingJob(chuck_offset=20.0)), [])
        partway = WindingJob(layups=(Layup(x_extension=30.0),))
        turn_lo = winding.turn_points(partway, partway.layups[0])[0]
        self.assertAlmostEqual(winding.x_travel(partway)[0], min(50.0, turn_lo - 30.0))
        long_tank = legacy_job(winding.MAX_X - 60.0, chuck_offset=50.0,  # turning at the very ends
                               layups=(Layup(x_extension=30.0),))
        self.assertIn("X-Axis would reach 5270mm", winding.validate(long_tank)[0])
        # A 90° wind never leaves the straight section: its own setting doesn't count.
        self.assertEqual(winding.validate(WindingJob(chuck_offset=20.0, layups=(Layup(hoop=True),))), [])
        self.assertEqual(winding.geometry_errors(WindingJob(layups=(Layup(hoop=True, x_extension=-5.0),))), [])

    def test_settings_header_and_old_files(self):
        job = WindingJob(layups=(Layup(x_extension=12.5), Layup(passes=3, wind_angle=60.0)))
        restored = winding.layups_from_header(parse_header(gcode(job)))
        self.assertEqual([l.x_extension for l in restored], [12.5, 0.0])  # (0: the default)
        # A file from before the setting was wound with its turnarounds at the ends.
        old = {"layups": "1", "layup_1": "passes=12 pattern_number=3 wind_angle=45.0 turnaround_angle=270.0 "
                                          "auto_cycles=False hoop=False"}
        self.assertEqual(winding.layups_from_header(old)[0].x_extension, 0.0)


class StraightLength(unittest.TestCase):
    """The tank's length is its straight section's; the domes come on top."""

    def test_the_whole_tank_is_the_straight_section_and_both_domes(self):
        job = WindingJob(straight_length=800.0)
        self.assertAlmostEqual(job.dome_length, math.sqrt(100.0 ** 2 - 25.0 ** 2))
        self.assertAlmostEqual(job.tank_length, 800.0 + 2 * job.dome_length)
        self.assertEqual(WindingJob(end_cap_type="Flat", straight_length=800.0).tank_length, 800.0)
        # A 90° wind covers exactly the straight section.
        first, last = job.hoop_span
        self.assertAlmostEqual(last - first + job.bandwidth, 800.0)
        self.assertAlmostEqual(first - job.bandwidth / 2, job.chuck_offset + job.dome_length)

    def test_the_file_records_both_lengths(self):
        job = WindingJob(straight_length=640.0)
        settings = parse_header(gcode(job))
        self.assertEqual(float(settings["straight_length"]), 640.0)
        self.assertAlmostEqual(float(settings["tank_length"]), job.tank_length, places=3)
        self.assertEqual(winding.job_from_header(settings), job)

    def test_an_old_file_gave_the_whole_length(self):
        # Before 2026-10-21 a file's tank_length was end to end, and every layup
        # wound the whole dome without going on beyond it.
        old = {"chuck_offset": "50.0", "tank_length": "1000.0", "tank_diameter": "200.0", "end_cap_diameter": "50.0",
               "end_cap_type": "Round", "layups": "1",
               "layup_1": "passes=12 pattern_number=3 wind_angle=45.0 turnaround_angle=270.0"}
        job = winding.job_from_header(old)
        self.assertAlmostEqual(job.tank_length, 1000.0)
        self.assertAlmostEqual(job.straight_length, 1000.0 - 2 * job.dome_length)
        layup = job.layups[0]
        self.assertEqual((layup.bulkhead_auto, layup.x_extension), (False, 0.0))
        self.assertEqual(winding.turn_points(job, layup), (50.0, 50.0 + job.tank_length))


class BulkheadAngle(unittest.TestCase):
    """How far onto the domes a layup winds: the angle at a dome's center from
    the axis to where the layup turns around."""
    OPENING = math.degrees(math.asin(25.0 / 100.0))  # the default tank's polar opening (end cap 50, diameter 200)

    def test_auto_is_the_geodesic_turning_point(self):
        for angle in (12.0, 30.0, 45.0, 70.0, 85.0):
            job = WindingJob(layups=(Layup(passes=1, wind_angle=angle),))
            layup = job.layups[0]
            with self.subTest(angle=angle):
                self.assertAlmostEqual(layup.bulkhead_angle, max(angle, self.OPENING))
                # It turns where the tank's radius is R * sin(winding angle) --
                # Clairaut's turning point -- or at the polar opening.
                x_turn = winding.turn_points(job, layup)[1]
                r_turn = winding._tank_geometry(job).radius(x_turn)
                self.assertAlmostEqual(r_turn, max(100.0 * math.sin(math.radians(angle)), 25.0), places=6)
        self.assertAlmostEqual(winding.opening_angle(WindingJob()), self.OPENING)

    def test_a_set_angle_and_its_limits(self):
        def cover(**layup):
            job = WindingJob(layups=(Layup(passes=1, bulkhead_auto=False, **layup),))
            return winding.bulkhead_cover(job, job.layups[0]), job
        self.assertAlmostEqual(cover(bulkhead_angle=60.0)[0], 100.0 * math.cos(math.radians(60.0)))
        full, job = cover(bulkhead_angle=5.0)  # below the opening: the whole dome
        self.assertEqual(full, job.dome_length)
        self.assertEqual(cover(bulkhead_angle=90.0)[0], 0.0)  # the straight section only
        self.assertIn("Bulkhead Angle must be between 0° and 90°.",
                      winding.geometry_errors(WindingJob(layups=(Layup(bulkhead_auto=False, bulkhead_angle=95.0),))))
        # A 90° wind and flat end caps have no bulkhead to set.
        self.assertEqual(winding.geometry_errors(WindingJob(layups=(Layup(hoop=True, bulkhead_angle=-5.0),))), [])
        flat = WindingJob(end_cap_type="Flat", layups=(Layup(bulkhead_auto=False, bulkhead_angle=60.0),))
        self.assertEqual(winding.turn_points(flat, flat.layups[0]), (flat.chuck_offset, flat.chuck_offset + flat.tank_length))

    def test_the_turnaround_stays_off_the_straight_section(self):
        # On auto, the zone is the part of the dome the layup covers: on the
        # straight section every pass is the plain helix.
        for layup in (Layup(passes=2, wind_angle=30.0), Layup(passes=2, wind_angle=70.0),
                      Layup(passes=2, wind_angle=45.0, bulkhead_auto=False, bulkhead_angle=80.0)):
            job = WindingJob(layups=(layup,))
            layup = job.layups[0]
            with self.subTest(bulkhead=layup.bulkhead_angle):
                self.assertAlmostEqual(winding.layup_zone(job, layup), winding.bulkhead_cover(job, layup))
                lo, hi = job.chuck_offset + job.dome_length, job.chuck_offset + job.tank_length - job.dome_length
                moves = [ev for ev in winding.iter_program(job) if type(ev) is Move]
                k = 360.0 / winding.plan_layup(job, layup).pitch
                for p, q in zip(moves, moves[1:]):
                    if lo - 1e-9 <= min(p.x, q.x) and max(p.x, q.x) <= hi + 1e-9 and p.x != q.x:
                        self.assertAlmostEqual((q.a - p.a) / abs(q.x - p.x), k, places=6)

    def test_the_pattern_closes_at_any_bulkhead_angle(self):
        for bulkhead in (FULL_DOME, {}, dict(bulkhead_auto=False, bulkhead_angle=62.5)):
            job = WindingJob(layups=tuple(replace(layup, **bulkhead) for layup in MULTI.layups))
            fwd, ret = crossings(job)
            for li, layup in enumerate(job.layups):
                n = layup.passes * layup.pattern_number
                with self.subTest(bulkhead=bulkhead, layup=li + 1):
                    assert_perfect_grid(self, [a for k, a in fwd.items() if k[0] == li], n, "forward")
                    assert_perfect_grid(self, [a for k, a in ret.items() if k[0] == li], n, "return")

    def test_klipper_still_takes_the_turnarounds_at_speed(self):
        for layup in (Layup(passes=1, wind_angle=20.0), Layup(passes=1, pattern_number=2, wind_angle=60.0),
                      Layup(passes=1, wind_angle=45.0, bulkhead_auto=False, bulkhead_angle=75.0)):
            job = WindingJob(layups=(layup,))
            with self.subTest(bulkhead=job.layups[0].bulkhead_angle):
                junctions = klipper_junctions(job)
                self.assertLess(max(d for *_, d in junctions), winding.TURN_MAX_DEFLECTION + 0.2)
                self.assertGreater(min(v / commanded for v, commanded, _ in junctions), 0.99)

    def test_settings_header_round_trip(self):
        job = WindingJob(layups=(Layup(), Layup(wind_angle=30.0, bulkhead_auto=False, bulkhead_angle=55.0)))
        restored = winding.layups_from_header(parse_header(gcode(job)))
        self.assertEqual([(l.bulkhead_auto, l.bulkhead_angle) for l in restored], [(True, 45.0), (False, 55.0)])
        self.assertEqual(WindingJob(layups=tuple(restored)).layups, job.layups)


class MultiLayupProgram(unittest.TestCase):

    def test_structure_and_continuity(self):
        events = list(winding.iter_program(MULTI))
        self.assertEqual([ev.index for ev in events if type(ev) is LayupStart], [0, 1, 2])
        cycles = [ev for ev in events if type(ev) is CycleComplete]
        self.assertEqual([c.number for c in cycles], list(range(1, MULTI.total_cycles + 1)))
        self.assertEqual([c.layup for c in cycles], [0, 0, 0, 1, 1, 2, 2])
        moves = [ev for ev in events if type(ev) is Move]
        x_start, x_end = MULTI.chuck_offset, MULTI.chuck_offset + MULTI.tank_length
        prev_a, prev_layup = 0.0, 0
        for mv in moves:
            self.assertGreaterEqual(mv.a, prev_a - 1e-9)  # the mandrel never reverses
            self.assertGreaterEqual(mv.layup, prev_layup)  # layups run strictly in order
            extension = MULTI.layups[mv.layup].x_extension
            self.assertTrue(x_start - extension - 1e-9 <= mv.x <= x_end + extension + 1e-9)
            self.assertTrue(0.0 <= mv.y <= winding.Y_TRAVEL)
            prev_a, prev_layup = mv.a, mv.layup
        for li, layup in enumerate(MULTI.layups):
            circuits = {mv.circuit for mv in moves if mv.layup == li}
            self.assertEqual(circuits, set(range(layup.passes * layup.pattern_number)))

    def test_gcode_markers_and_axis_resets(self):
        lines = gcode(MULTI)
        self.assertEqual([ln for ln in lines if ln.startswith("; LAYUP_START")],
                         ["; LAYUP_START:1", "; LAYUP_START:2", "; LAYUP_START:3"])
        # One reset per cycle within the wind (plus the one zeroing A at resume).
        wind = lines[lines.index("; LAYUP_START:1"):]
        self.assertEqual(sum(ln == "G92 A0" for ln in wind), MULTI.total_cycles)
        self.assertEqual(lines[-4:-2], [f"; CYCLE_COMPLETE:{MULTI.total_cycles}", "G92 A0"])
        self.assertEqual(lines[-2], "M117 Wind complete")
        # A is reset after every cycle, so written values stay near one cycle's worth
        # of rotation rather than growing with the whole program.
        written_a = [float(tok[1:]) for ln in lines if ln.startswith("G1") for tok in ln.split() if tok[0] == "A"]
        total_rotation = [ev for ev in winding.iter_program(MULTI) if type(ev) is Move][-1].a
        self.assertLess(max(written_a), total_rotation / 2)

    def test_settings_header_round_trip(self):
        settings = parse_header(gcode(MULTI))
        self.assertEqual(tuple(winding.layups_from_header(settings)), MULTI.layups)
        restored = WindingJob(**{k: (settings[k] == "True") if isinstance(getattr(MULTI, k), bool)
                                 else type(getattr(MULTI, k))(settings[k]) for k in winding.GLOBAL_KEYS},
                              layups=winding.layups_from_header(settings))
        self.assertEqual(restored, MULTI)

    def test_reads_single_pattern_files(self):
        # Files written before multi-layup support stored one pattern as top-level keys.
        legacy = {"passes": "10", "pattern_number": "3", "wind_angle": "45.0", "turnaround_angle": "270.0",
                  "bandwidth": "5.0"}
        self.assertEqual(winding.layups_from_header(legacy), [Layup(10, 3, 45.0, 270.0, x_extension=0.0, **FULL_DOME)])
        self.assertIsNone(winding.layups_from_header({"bandwidth": "5.0"}))


def layup_moves(job):
    """{layup index: [Move, ...]} in program order."""
    moves, layup = {}, None
    for ev in winding.iter_program(job):
        if type(ev) is LayupStart:
            layup = ev.index
            moves[layup] = []
        elif type(ev) is Move:
            moves[layup].append(ev)
    return moves


def deg_per_mm(moves):
    return [(q.a - p.a) / abs(q.x - p.x) for p, q in zip(moves, moves[1:]) if abs(q.x - p.x) > 1e-9]


# Helical, 90°, helical, 90°, helical.
HOOPS = WindingJob(layups=(Layup(passes=2, wind_angle=45.0), Layup(hoop=True), Layup(passes=2, wind_angle=30.0),
                           Layup(hoop=True), Layup(passes=2, pattern_number=5, wind_angle=60.0)))


class NinetyDegreeWind(unittest.TestCase):
    def test_one_pass_over_the_straight_section_edge_to_edge(self):
        job = WindingJob(layups=(Layup(hoop=True),))
        first, last = job.hoop_span
        # The band's edges are flush with the straight section's ends.
        self.assertAlmostEqual(first - job.bandwidth / 2, job.chuck_offset + job.dome_length)
        self.assertAlmostEqual(last + job.bandwidth / 2, job.chuck_offset + job.tank_length - job.dome_length)
        self.assertEqual(job.wind_start_x, first)  # auto start: right where it begins
        moves = layup_moves(job)[0]
        self.assertAlmostEqual(moves[-1].x, last)
        self.assertTrue(all(first - 1e-9 <= m.x <= last + 1e-9 for m in moves))  # one way, never on a dome
        self.assertTrue(all(b.x >= a.x for a, b in zip(moves, moves[1:])))
        # Every turn one band width on from the last: 360° per band width.
        for rate in deg_per_mm(moves):
            self.assertAlmostEqual(rate, 360.0 / job.bandwidth)
        self.assertAlmostEqual(moves[-1].a, (last - first) / job.bandwidth * 360.0)
        self.assertEqual(sum(type(ev) is CycleComplete for ev in winding.iter_program(job)), 1)
        self.assertEqual(job.layups[0].passes, 1)
        self.assertAlmostEqual(job.hoop_angle, math.degrees(math.atan(math.pi * job.tank_diameter / job.bandwidth)))

    def test_each_hoop_flips_where_the_next_layups_start(self):
        self.assertEqual(winding.start_sides(HOOPS), [1, 1, -1, -1, 1])
        moves = layup_moves(HOOPS)
        first, last = HOOPS.hoop_span
        x_start, x_end = HOOPS.chuck_offset, HOOPS.chuck_offset + HOOPS.tank_length
        self.assertAlmostEqual(moves[1][-1].x, last)    # hoop 1 ends at the far end...
        self.assertGreater(moves[2][0].x, last)         # ...the next layup turns around there
        self.assertAlmostEqual(moves[2][-1].x, last)    # and, flipped, ends at the far end (at hoop 2's start)
        self.assertAlmostEqual(moves[3][-1].x, first)   # hoop 2 runs back to the chuck side
        self.assertLess(moves[4][0].x, first)           # so the last layup turns around there
        self.assertAlmostEqual(moves[4][-1].x, winding.turn_points(HOOPS, HOOPS.layups[4])[0]
                               - HOOPS.layups[4].x_extension)

    def test_the_layup_before_runs_on_to_the_hoops_start(self):
        # Its last turnaround is a whole one, like every other (no dwell cut
        # short into a rotation at the end), and runs back out along its own
        # helix -- in the direction the hoop then winds -- right up to where
        # the hoop starts. The hoop itself never leaves the straight section.
        moves = layup_moves(HOOPS)
        first, last = HOOPS.hoop_span
        x_start, x_end = HOOPS.chuck_offset, HOOPS.chuck_offset + HOOPS.tank_length
        for hoop, start, direction in ((1, first, 1), (3, last, -1)):
            before = moves[hoop - 1]
            with self.subTest(hoop=hoop + 1):
                self.assertAlmostEqual(before[-1].x, start)
                self.assertFalse(any(m.dwell for m in before))
                # The last reversal is beyond where the layup turns (by its
                # Additional X-Axis Travel); from there on the eye heads the
                # hoop's way, on its own helix once off the dome.
                xs = [m.x for m in before]
                turn = max(i for i in range(1, len(xs) - 1) if (xs[i] - xs[i - 1]) * (xs[i + 1] - xs[i]) < 0)
                extension = HOOPS.layups[hoop - 1].x_extension
                turn_lo, turn_hi = winding.turn_points(HOOPS, HOOPS.layups[hoop - 1])
                self.assertAlmostEqual(xs[turn], turn_lo - extension if direction == 1 else turn_hi + extension)
                out = before[turn:]
                self.assertTrue(all((q.x - p.x) * direction > 0 for p, q in zip(out, out[1:])))
                dome_end = start - direction * HOOPS.bandwidth / 2
                straight = [m for m in out if (m.x - dome_end) * direction >= -1e-9]
                self.assertGreater(len(straight), 1)
                pitch = winding.plan_layup(HOOPS, HOOPS.layups[hoop - 1]).pitch
                for rate in deg_per_mm(straight):
                    self.assertAlmostEqual(rate, 360.0 / pitch, places=6)
                self.assertTrue(all(first - 1e-9 <= m.x <= last + 1e-9 for m in moves[hoop]))
                self.assertGreater((moves[hoop][0].x - start) * direction, 0)  # on the same way

    def test_the_layup_after_turns_around_at_the_tank_end(self):
        # Out of a hoop, the next layup doesn't wind back from the straight
        # section (pulling the fiber back over the hoop): it goes on the same
        # way, over the dome at its own angle, and turns around where its
        # Bulkhead Angle takes it (beyond by its Additional X-Axis Travel)
        # with its own dwell -- the helix there and back plus the dwell.
        moves = layup_moves(HOOPS)
        for after, side in ((2, 1), (4, 0)):
            x_turn = winding.turn_points(HOOPS, HOOPS.layups[after])[side]
            layup = HOOPS.layups[after]
            hoop_end = moves[after - 1][-1]
            direction = 1 if x_turn > hoop_end.x else -1
            mv = moves[after]
            with self.subTest(layup=after + 1):
                turn = next(i for i, (p, q) in enumerate(zip(mv, mv[1:])) if (q.x - p.x) * direction < 0)
                out = [hoop_end] + mv[:turn + 1]
                self.assertTrue(all((q.x - p.x) * direction > 0 for p, q in zip(out, out[1:])))
                self.assertAlmostEqual(out[-1].x, x_turn + direction * layup.x_extension)
                k = 360.0 / winding.plan_layup(HOOPS, layup).pitch
                self.assertAlmostEqual(deg_per_mm(out[:2])[0], k, places=6)  # its own angle from the start
                back = next((p, q) for p, q in zip(mv, mv[1:]) if (p.x - hoop_end.x) * (q.x - hoop_end.x) <= 0)
                p, q = back
                a_back = p.a + (hoop_end.x - p.x) / (q.x - p.x) * (q.a - p.a)
                helix = 2 * (abs(x_turn - hoop_end.x) + layup.x_extension) * k
                self.assertAlmostEqual(a_back - hoop_end.a, helix + layup.turnaround_angle, places=6)
                self.assertFalse(any(m.dwell for m in out))

    def test_layup_changes_pause_on_the_straight_section(self):
        # Every PAUSE around a hoop is where it starts or ends: the eye stands
        # on the straight section, with the fiber lying on the tank.
        first, last = HOOPS.hoop_span
        lines = gcode(HOOPS)
        x = None
        paused_at = {}
        for i, ln in enumerate(lines):
            if ln.startswith("G1") and " X" in ln:
                x = float(ln.split(" X", 1)[1].split()[0])
            elif ln.startswith("; LAYUP_START:") and lines[i - 1] == "PAUSE":
                paused_at[int(ln.split(":")[1])] = x
        self.assertEqual(sorted(paused_at), [2, 3, 4, 5])
        for layup, x_expected in ((2, first), (3, last), (4, last), (5, first)):
            self.assertAlmostEqual(paused_at[layup], x_expected, places=3, msg=f"before layup {layup}")

    def test_hoop_after_hoop_winds_back_on_the_straight_section(self):
        job = WindingJob(layups=(Layup(passes=2), Layup(hoop=True), Layup(hoop=True), Layup(passes=2, wind_angle=30.0)))
        moves = layup_moves(job)
        first, last = job.hoop_span
        self.assertAlmostEqual(moves[1][-1].x, last)
        self.assertTrue(all(first - 1e-9 <= m.x <= last + 1e-9 for m in moves[2]))
        self.assertTrue(all(b.x <= a.x for a, b in zip(moves[2], moves[2][1:])))
        self.assertAlmostEqual(moves[2][-1].x, first)
        self.assertLess(moves[3][0].x, first)  # and the helical layup after it turns around at the chuck end

    def test_a_short_turnaround_zone_never_reaches_past_the_hoop(self):
        # With flat end caps (or a zone longer than the dome) the turnaround
        # zone reaches further onto the tank than the hoop's start: the
        # turnarounds into and out of the hoop then turn over what's left, so
        # the eye never has to reverse on the straight section.
        for job in (WindingJob(end_cap_type="Flat", layups=(Layup(passes=2), Layup(hoop=True), Layup(passes=2))),
                    WindingJob(turnaround_zone=150.0, turnaround_zone_auto=False,
                               layups=(Layup(passes=2), Layup(hoop=True), Layup(passes=2)))):
            with self.subTest(cap=job.end_cap_type, zone=job.turnaround_zone):
                moves = layup_moves(job)
                first, last = job.hoop_span
                x_lo = winding.turn_points(job, job.layups[0])[0] - job.layups[0].x_extension
                x_hi = winding.turn_points(job, job.layups[2])[1] + job.layups[2].x_extension
                # Into the hoop: from the last reversal at the chuck end, only ever toward it.
                into = moves[0][max(i for i, m in enumerate(moves[0]) if abs(m.x - x_lo) < 1e-6):]
                self.assertTrue(all(b.x > a.x for a, b in zip(into, into[1:])))
                self.assertAlmostEqual(into[-1].x, first)
                # Out of it: straight on to the far end before turning back.
                out = [moves[1][-1]] + moves[2]
                out = out[:next(i for i, m in enumerate(out) if abs(m.x - x_hi) < 1e-6) + 1]
                self.assertTrue(all(b.x > a.x for a, b in zip(out, out[1:])))
                self.assertAlmostEqual(out[0].x, last)

    def test_a_double_one_winds_there_and_back(self):
        job = WindingJob(layups=(Layup(passes=2), Layup(hoop=True, double=True), Layup(passes=2, wind_angle=30.0)))
        first, last = job.hoop_span
        self.assertEqual(job.layups[1].passes, 2)  # one cycle each way
        self.assertEqual(winding.start_sides(job), [1, 1, 1])  # back where it began
        moves = layup_moves(job)[1]
        there = [m for m in moves if m.circuit == 0]
        back = [m for m in moves if m.circuit == 1]
        self.assertAlmostEqual(there[-1].x, last)
        self.assertAlmostEqual(back[-1].x, first)
        self.assertTrue(all(b.x <= a.x for a, b in zip(back, back[1:])))
        self.assertEqual([ev.layup for ev in winding.iter_program(job) if type(ev) is CycleComplete].count(1), 2)

    def test_a_double_one_moves_exactly_like_two_hoop_layups(self):
        double = WindingJob(layups=(Layup(passes=2), Layup(hoop=True, double=True), Layup(passes=2, wind_angle=30.0)))
        two = WindingJob(layups=(Layup(passes=2), Layup(hoop=True), Layup(hoop=True), Layup(passes=2, wind_angle=30.0)))
        path = lambda job: [(m.x, m.y, m.a) for m in winding.iter_program(job) if type(m) is Move]
        self.assertEqual(path(double), path(two))
        # ...and pauses where they would: at each end of the straight section.
        def pauses(job):
            lines, x, at = gcode(job), None, []
            for ln in lines:
                if ln.startswith("G1") and " X" in ln:
                    x = float(ln.split(" X", 1)[1].split()[0])
                elif ln == "PAUSE" and x is not None:
                    at.append(x)
            return at
        self.assertEqual(pauses(double), pauses(two))
        first, last = double.hoop_span
        self.assertEqual(len(pauses(double)), 4)  # attaching the fiber, then first, last, first
        self.assertEqual(pauses(double)[1:], [round(first, 3), round(last, 3), round(first, 3)])
        no_pauses = replace(double, pause_after_layup=False)
        self.assertEqual(sum(ln == "PAUSE" for ln in gcode(no_pauses)), 1)

    def test_continuing_inside_a_double_ones_way_back(self):
        job = WindingJob(layups=(Layup(passes=1), Layup(hoop=True, double=True), Layup(passes=1, wind_angle=30.0)))
        first, last = job.hoop_span
        location = winding.locate(job, winding.ProgramPoint(1, 1, 7200.0))  # 20 turns into the way back
        self.assertAlmostEqual(location.x, last - 20 * job.bandwidth, places=6)
        full = without_angle_updates(gcode(job))
        rest = without_angle_updates(after_first_move(partial(job, winding.ProgramPoint(1, 1, 7200.0)),
                                                      "; LAYUP_START:2"))
        self.assertEqual(full[full.index(rest[0]):], rest)

    def test_a_flipped_layup_keeps_its_pattern(self):
        # Started from the far end, the helical layup is simply mirrored: its
        # bands still tile the tank exactly.
        fwd, ret = crossings(HOOPS)
        for li in (2, 4):
            layup = HOOPS.layups[li]
            n = layup.passes * layup.pattern_number
            with self.subTest(layup=li + 1):
                assert_perfect_grid(self, [a for k, a in fwd.items() if k[0] == li], n, "forward")
                assert_perfect_grid(self, [a for k, a in ret.items() if k[0] == li], n, "return")

    def test_lead_in_of_a_first_hoop_started_elsewhere(self):
        # A 90° first layup started somewhere else gets there at LEAD_IN_ANGLE.
        job = WindingJob(start_x_auto=False, start_x=400.0, layups=(Layup(hoop=True),))
        lead = [m for m in layup_moves(job)[0] if m.x > job.hoop_span[0] + 1e-9][:5]
        start = winding.start_position(job)
        lead_rates = deg_per_mm([Move(start[0], start[1], 0.0, 0, 0, 0, 0, 0, 0, False)] + lead)
        self.assertLess(lead[0].x, 400.0)  # back toward the hoop's start
        for rate in lead_rates:
            self.assertAlmostEqual(rate, 360.0 / winding.helix_pitch(job.tank_diameter, winding.LEAD_IN_ANGLE))

    def test_its_helical_settings_are_ignored(self):
        a = WindingJob(layups=(Layup(), Layup(hoop=True)))
        b = WindingJob(layups=(Layup(), Layup(hoop=True, passes=9, pattern_number=7, wind_angle=20.0,
                                              turnaround_angle=30.0, auto_cycles=True, x_extension=75.0)))
        self.assertEqual(body(gcode(a)), body(gcode(b)))
        self.assertEqual(winding.geometry_errors(WindingJob(layups=(Layup(hoop=True, wind_angle=95.0, passes=0),))), [])

    def test_straight_section_too_short(self):
        # 6 mm of straight section for an 8 mm band.
        job = WindingJob(straight_length=6.0, bandwidth=8.0, layups=(Layup(), Layup(hoop=True)))
        self.assertIn("Layup 2: The straight section is too short for a 90° wind (it must be longer than the band "
                      "is wide).", winding.geometry_errors(job))

    def test_speed_and_estimates(self):
        job = WindingJob(layups=(Layup(hoop=True),))
        ws = winding.wind_speed(job, job.layups[0])
        self.assertEqual(ws.limit, "rotation")
        self.assertAlmostEqual(ws.rotation, job.max_a_speed)
        self.assertAlmostEqual(ws.x_speed, job.max_a_speed / 60.0 / 360.0 * job.bandwidth)
        result = winding.simulate(job)
        self.assertEqual(len(result.layups[0].strand_runs), 1)
        self.assertAlmostEqual(result.cylinder_tow, result.total_tow, delta=0.01 * result.total_tow)

    def test_header_round_trip_and_old_files(self):
        restored = winding.layups_from_header(parse_header(gcode(HOOPS)))
        self.assertEqual([l.hoop for l in restored], [False, True, False, True, False])
        self.assertEqual(WindingJob(layups=tuple(restored)).layups, HOOPS.layups)
        old = {"layups": "1", "layup_1": "passes=12 pattern_number=3 wind_angle=45.0 turnaround_angle=270.0"}
        self.assertFalse(winding.layups_from_header(old)[0].hoop)

    def test_continuing_inside_a_hoop(self):
        point = winding.ProgramPoint(1, 0, 20000.0)
        location = winding.locate(HOOPS, point)
        first, _ = HOOPS.hoop_span
        # The layup before ran on right up to its start: the cycle begins there.
        self.assertAlmostEqual(location.x, first + 20000.0 / 360.0 * HOOPS.bandwidth, places=6)
        part = partial(HOOPS, point)
        full = without_angle_updates(gcode(HOOPS))
        rest = without_angle_updates(after_first_move(part, "; LAYUP_START:2"))
        self.assertEqual(full[full.index(rest[0]):], rest)
        # Inside the turnaround out of the hoop, too.
        part = partial(HOOPS, winding.ProgramPoint(2, 0, 150.0))
        rest = without_angle_updates(after_first_move(part, "; LAYUP_START:3"))
        self.assertEqual(full[full.index(rest[0]):], rest)
        progress = winding.progress(HOOPS, winding.ProgramPoint(2, 0, 0.0))
        self.assertNotIn(1, progress.covered)  # drawn band by band, not as a solid tank
        self.assertIn(1, progress.runs)


STATUS = ("SET_PRINT_STATS_INFO", "M117 ", "RESPOND ")


def mainsail_layer(lines):
    """What Mainsail's "Layer x of y" shows while the file runs: Klipper-for-CNC's
    print_stats.cmd_SET_PRINT_STATS_INFO replayed over every SET_PRINT_STATS_INFO
    line. [(current, total, A of the move before it, cycle or None)] per line --
    the cycle from the last CYCLE_COMPLETE marker."""
    total = current = None
    a, shown = 0.0, []
    for ln in lines:
        if ln.startswith("G92"):
            a = float(ln.split("A", 1)[1])
        elif ln.startswith("G1"):
            a = next((float(t[1:]) for t in ln.split()[1:] if t[0] == "A"), a)
        elif ln.startswith("SET_PRINT_STATS_INFO"):
            v = {k: int(x) for k, x in (tok.split("=") for tok in ln.split()[1:])}
            new_total, new_current = v.get("TOTAL_LAYER", total), v.get("CURRENT_LAYER", current)
            if new_total == 0:
                total = current = None
            elif new_total != total:
                total, current = new_total, 0
            if total is not None and new_current is not None and new_current != current:
                current = min(new_current, total)
            shown.append((current, total, a))
    return shown


class StatusMessages(unittest.TestCase):
    def test_layer_display_shows_the_angle_within_the_cycle(self):
        # "Layer 1234 of 5760": the mandrel angle A (it restarts at 0 with every
        # cycle) out of the angle the cycle ends at, in whole degrees.
        for job in (MULTI, HOOPS):
            with self.subTest(layups=len(job.layups)):
                lines = gcode(job)
                # Each cycle's total is where its A ends, just before CYCLE_COMPLETE.
                totals, ends, a, total = [], [], 0.0, None
                for ln in lines:
                    if ln.startswith("SET_PRINT_STATS_INFO TOTAL_LAYER="):
                        totals.append(int(ln.split()[1].split("=")[1]))
                    elif ln.startswith("G1"):
                        a = next((float(t[1:]) for t in ln.split()[1:] if t[0] == "A"), a)
                    elif ln.startswith("; CYCLE_COMPLETE"):
                        ends.append(a)
                self.assertEqual(len(totals), job.total_cycles)
                for t, end in zip(totals, ends):
                    self.assertLessEqual(abs(t - end), 0.5)
                # What Mainsail shows: always the angle Klipper has just read, up to the total.
                shown = mainsail_layer(lines)
                for current, shown_total, a in shown:
                    self.assertLessEqual(current, shown_total)
                    self.assertLessEqual(abs(current - a), 0.5 if current < shown_total else shown_total)

    def test_the_angle_is_updated_about_every_second(self):
        lines = gcode(MULTI)
        since, gaps = 0.0, []
        pos = [0.0, 0.0, 0.0]
        started = False
        for ln in lines:
            if ln.startswith("; LAYUP_START"):
                started = True
            elif ln.startswith("G92"):
                pos[2] = float(ln.split("A", 1)[1])
            elif ln.startswith("G1"):
                w = {t[0]: float(t[1:]) for t in ln.split()[1:]}
                new = [w.get("X", pos[0]), w.get("Y", pos[1]), w.get("A", pos[2])]
                if started:
                    since += math.dist(new, pos) / w["F"] * 60.0
                pos = new
            elif ln.startswith("SET_PRINT_STATS_INFO") or ln.startswith("; CYCLE_COMPLETE"):
                gaps.append(since)
                since = 0.0
        # Never longer than the update interval plus one move (at most Max Move Time).
        self.assertLess(max(gaps), winding.LAYER_UPDATE_SECONDS + MULTI.max_move_time + 1e-6)

    def test_each_cycle_start_is_announced(self):
        lines = gcode(HOOPS)
        i = lines.index("; LAYUP_START:2")
        self.assertRegex(lines[i + 1], r"^SET_PRINT_STATS_INFO TOTAL_LAYER=\d+ CURRENT_LAYER=0$")
        self.assertEqual(lines[i + 2:i + 4], [
            "M117 Layup 2/5 - Cycle 1/1 - 90 deg wind",
            'RESPOND MSG="Starting cycle 1/1 of layup 2/5 (90 deg wind, straight section)"'])
        # ...including after every cycle reset within a layup: the cycle number
        # is in the status line.
        j = lines.index("; CYCLE_COMPLETE:1")
        self.assertEqual(lines[j + 1], "G92 A0")
        self.assertRegex(lines[j + 2], r"^SET_PRINT_STATS_INFO TOTAL_LAYER=\d+ CURRENT_LAYER=0$")
        self.assertEqual(lines[j + 3], "M117 Layup 1/5 - Cycle 2/2 - 45 deg")
        self.assertEqual(lines[-2:], ["M117 Wind complete", f'RESPOND MSG="Wind complete: 5 layups, {HOOPS.total_cycles} cycles"'])

    def test_pauses_say_what_to_do(self):
        lines = gcode(HOOPS)
        pauses = [i for i, ln in enumerate(lines) if ln == "PAUSE"]
        self.assertEqual(len(pauses), len(HOOPS.layups))  # the wind start, then one between each pair of layups
        self.assertEqual(lines[pauses[0] - 2], "M117 Attach the fiber, then resume")
        for i in pauses[1:]:
            self.assertRegex(lines[i - 2], r"^M117 Layup \d/5 done - check the fiber, then resume$")
            self.assertTrue(lines[i - 1].startswith('RESPOND MSG="Layup ') and "Next: layup" in lines[i - 1])

    def test_partial_program_starts_with_its_cycle(self):
        part = partial(HOOPS, winding.ProgramPoint(2, 1, 300.0))
        i = part.index("; LAYUP_START:3")
        # The layer display continues at the point's angle, out of the angle
        # that cycle ends at -- the same total as in the complete program.
        full_totals = [ln for ln in gcode(HOOPS) if ln.startswith("SET_PRINT_STATS_INFO TOTAL_LAYER=")]
        cycle_index = sum(l.passes for l in HOOPS.layups[:2]) + 1
        self.assertEqual(part[i + 1], full_totals[cycle_index].replace("CURRENT_LAYER=0", "CURRENT_LAYER=300"))
        self.assertEqual(part[i + 2], "M117 Layup 3/5 - Cycle 2/2 - 30 deg")
        self.assertEqual(mainsail_layer(part)[0][0], 300)
        rehomed = partial(HOOPS, winding.ProgramPoint(2, 1, 300.0), rehome=True)
        self.assertEqual(rehomed[rehomed.index("PAUSE") - 2], "M117 Reattach the fiber, then resume")

    def test_messages_are_safe_for_klipper(self):
        # Klipper-for-CNC aborts on anything it can't parse: plain ASCII, no ';'
        # (a comment), no '#'/'*' (end of arguments), one quoted RESPOND MSG.
        for ln in gcode(HOOPS) + gcode(WindingJob(layups=(Layup(wind_angle=37.5),))):
            if ln.startswith(STATUS):
                self.assertTrue(ln.isascii() and not any(c in ln for c in ";#*'"), ln)
                if ln.startswith("RESPOND"):
                    self.assertRegex(ln, r'^RESPOND MSG="[^"]+"$')
        self.assertEqual(winding._machine_text('45° "x"; #1 * it\'s'), "45 deg x 1 its")


class Validation(unittest.TestCase):
    def test_layup_errors_name_the_layup(self):
        job = WindingJob(layups=(Layup(), Layup(passes=0), Layup(wind_angle=90.0)))
        errors = winding.geometry_errors(job)
        self.assertEqual(len(errors), 2)
        self.assertTrue(errors[0].startswith("Layup 2:"))
        self.assertTrue(errors[1].startswith("Layup 3:"))

    def test_single_layup_errors_have_no_prefix(self):
        errors = winding.geometry_errors(WindingJob(layups=(Layup(pattern_number=0),)))
        self.assertEqual(errors, ["Number of Cycles and Pattern Number must both be at least 1."])

    def test_machine_limits_block_generation_only(self):
        job = WindingJob(straight_length=winding.MAX_X)
        self.assertEqual(winding.geometry_errors(job), [])
        self.assertEqual(len(winding.validate(job)), 1)

    def test_values_are_normalized(self):
        self.assertEqual(Layup(passes=4.0, wind_angle=45), Layup(passes=4, wind_angle=45.0))
        self.assertIsInstance(WindingJob(straight_length=1000).straight_length, float)


class Simulation(unittest.TestCase):
    def test_per_layup_results(self):
        result = winding.simulate(MULTI)
        self.assertEqual(len(result.layups), len(MULTI.layups))
        self.assertAlmostEqual(sum(lr.time for lr in result.layups), result.total_time, places=6)
        self.assertAlmostEqual(sum(lr.tow for lr in result.layups), result.total_tow, places=6)
        for layup, lr in zip(MULTI.layups, result.layups):
            self.assertGreater(lr.time, 0.0)
            # First cycle only: one forward and one return run per strand.
            self.assertEqual(len(lr.strand_runs), layup.pattern_number)
            self.assertTrue(all(len(runs) == 2 for runs in lr.strand_runs))

    def test_strand_points_stay_on_the_tank(self):
        result = winding.simulate(MULTI)
        r_tank = MULTI.tank_diameter / 2
        for lr in result.layups:
            for runs in lr.strand_runs:
                for run in runs:
                    for x, r, a in run:
                        self.assertTrue(MULTI.end_cap_diameter / 2 - 1e-9 <= r <= r_tank + 1e-9)
                        self.assertTrue(math.isfinite(a))

    def test_layups_reported_as_their_strands_are_complete(self):
        reported = []
        result = winding.simulate(HOOPS, on_layup=lambda li, runs: reported.append((li, repr(runs))))
        self.assertEqual([li for li, _ in reported], list(range(len(HOOPS.layups))))
        for li, runs in reported:
            self.assertEqual(runs, repr(result.layups[li].strand_runs), li)


def with_layup(job, index, **changes):
    layups = list(job.layups)
    layups[index] = replace(layups[index], **changes)
    return replace(job, layups=tuple(layups))


class ResumedSimulation(unittest.TestCase):
    """simulate(job, previous) picks up after the layups that didn't change --
    and must give exactly what a simulation from scratch does."""

    def assert_resumes(self, before, after, expected_start):
        previous = winding.simulate(before)
        reported = []
        resumed = winding.simulate(after, previous, on_layup=lambda li, runs: reported.append(li))
        self.assertEqual(resumed.resumed_from, expected_start)
        fresh = winding.simulate(after)
        self.assertEqual(resumed, fresh)  # every number, to the last bit
        self.assertEqual(reported, list(range(len(after.layups))))
        # ...and it can be resumed from in turn.
        again = winding.simulate(before, resumed)
        self.assertEqual(again, previous)

    def test_edited_layup(self):
        # The layup before the edited one runs out onto its helix: wound again.
        self.assert_resumes(HOOPS, with_layup(HOOPS, 4, wind_angle=50.0), 3)
        self.assert_resumes(HOOPS, with_layup(HOOPS, 3, hoop=False, wind_angle=40.0), 2)
        self.assert_resumes(HOOPS, with_layup(HOOPS, 2, passes=3), 1)

    def test_edit_near_the_start_runs_everything(self):
        self.assert_resumes(HOOPS, with_layup(HOOPS, 1, hoop=False), 0)
        self.assert_resumes(HOOPS, with_layup(HOOPS, 0, wind_angle=40.0), 0)

    def test_layups_added_and_removed(self):
        more = replace(HOOPS, layups=HOOPS.layups + (Layup(passes=1, wind_angle=20.0),))
        self.assert_resumes(HOOPS, more, 4)
        self.assert_resumes(more, HOOPS, 4)

    def test_global_change_runs_everything(self):
        self.assert_resumes(HOOPS, replace(HOOPS, straight_length=210.0), 0)
        self.assert_resumes(HOOPS, replace(HOOPS, max_filament_speed=200.0), 0)

    def test_unchanged(self):
        self.assert_resumes(HOOPS, HOOPS, 4)


class ThinPath(unittest.TestCase):
    """The preview's thinned strand paths (winding.thin_path)."""

    def check(self, points, max_deg=8.0, max_dx=15.0, max_dr=1.5):
        out = winding.thin_path(points, max_deg, max_dx, max_dr)
        self.assertIs(out[0], points[0])
        self.assertIs(out[-1], points[-1])
        where = [points.index(p) for p in out]
        self.assertEqual(where, sorted(set(where)))  # in order, a subset of the points
        for i, j in zip(where, where[1:]):
            ax, ar, aa = points[i][:3]
            # Every point skipped stays within the limits of the chord's start.
            for x, r, a in points[i + 1:j]:
                self.assertLessEqual(abs(a - aa), max_deg)
                self.assertLessEqual(abs(x - ax), max_dx)
                self.assertLessEqual(abs(r - ar), max_dr)
        return out, where

    def test_strand_paths(self):
        result = winding.simulate(MULTI)
        for lr in result.layups:
            for run in (run for runs in lr.strand_runs for run in runs):
                out, _ = self.check(run)
                self.assertLess(len(out), len(run))

    def test_x_turns_are_kept(self):
        # Out along X, back, and out again -- the angle and radius steady.
        xs = [i * 1.0 for i in range(40)] + [39.0 - i for i in range(1, 30)] + [10.0 + i for i in range(1, 20)]
        points = [(x, 50.0, 0.1 * n) for n, x in enumerate(xs)]
        _, where = self.check(points)
        self.assertIn(39, where)
        self.assertIn(39 + 29, where)

    def test_short_paths(self):
        for points in ([], [(0.0, 1.0, 0.0)], [(0.0, 1.0, 0.0), (1.0, 1.0, 50.0)]):
            self.assertEqual(winding.thin_path(points), points)


class JobFromHeader(unittest.TestCase):
    def test_round_trip(self):
        for job in (MULTI, HOOPS, excel_layer(12.0, 11, 3, zone=40.0)):
            self.assertEqual(winding.job_from_header(parse_header(gcode(job))), job)

    def test_not_a_job(self):
        self.assertIsNone(winding.job_from_header({}))
        settings = parse_header(gcode(MULTI))
        settings["straight_length"] = "long"
        self.assertIsNone(winding.job_from_header(settings))


if __name__ == "__main__":
    unittest.main()
