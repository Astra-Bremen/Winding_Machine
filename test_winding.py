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

# The Excel generator's "Transcendence Model Tank PN0003" helical layers, on its
# 250 mm tank with a 7 mm tow: 1480 mm helical traverse and the sheet's minimum
# turnaround of 2 x (90 - angle) at each end.
def excel_layer(angle, pattern, cycles, zone=0.0):
    return WindingJob(tank_length=1480.0, tank_diameter=250.0, end_cap_type="Flat", bandwidth=7.0, turnaround_zone=zone,
                      layups=(Layup(passes=cycles, pattern_number=pattern, wind_angle=angle,
                                    turnaround_angle=2 * (90 - angle)),))


class GoldenMaster(unittest.TestCase):
    """Pins the exact G-code body (everything after the settings header) so an
    unintended change to the motion shows up. Regenerate these deliberately,
    and only together with a change that is meant to alter the output. (Last
    regenerated for Max Feedrate, which lowers the F of moves that turning at
    Max Rotation Speed would push past it -- only excel_style_zone's 12 deg
    layup at the default 300 mm/s; the path itself is unchanged.)"""
    CASES = {
        "default": (WindingJob(), 12018, "acd2c9e7a1f820065a678423382bc51a62ef071f792a1aab23a5ac551257ada2"),
        "flat_p1_optimized_nohome": (
            WindingJob(end_cap_type="Flat", optimize_trajectory=True, home_before_wind=False,
                       layups=(Layup(passes=4, pattern_number=1, wind_angle=60.0, turnaround_angle=180.0),)),
            1614, "668a2386254849efc181cac5b312bba60c9fc8b2ec70cad83fcad26b61cf3e06"),
        "round_p5_steep_optimized": (
            WindingJob(bandwidth=8.0, optimize_trajectory=True,
                       layups=(Layup(passes=2, pattern_number=5, wind_angle=70.0, turnaround_angle=90.0),)),
            4000, "6c725086fb2d1fdc4ab4ea0d1f69c4ca089c9b624b188824abefc2f72a2ede48"),
        "round_small_dwell": (
            WindingJob(tank_length=640.0, tank_diameter=160.0, end_cap_diameter=40.0, eye_width=35.0,
                       min_spacing=6.0, max_surface_speed=150.0,
                       layups=(Layup(passes=3, pattern_number=2, wind_angle=30.0, turnaround_angle=10.0),)),
            1543, "82a8cf44e24e320b9fc981ef91dcf47bdb065f20509e6116702cf864f59bb47d"),
        "excel_style_zone": (
            WindingJob(tank_length=1640.0, tank_diameter=250.0, bandwidth=7.0, turnaround_zone=80.0,
                       layups=(Layup(passes=3, pattern_number=5, wind_angle=12.0, turnaround_angle=156.0),
                               Layup(passes=2, pattern_number=7, wind_angle=54.0, turnaround_angle=72.0))),
            19027, "9e4e4c56a453fccfed8a71929238c3960d746b37e121eb80be5b0a638713f9e1"),
        # A 90° wind between two helical layups: the lead-in over the dome, the
        # hoop pass, and the next layup starting from the far end.
        "hoop_between_helicals": (
            WindingJob(tank_length=1200.0, tank_diameter=220.0, bandwidth=6.0, max_move_time=1.0,
                       layups=(Layup(passes=2, pattern_number=3, wind_angle=40.0, turnaround_angle=200.0),
                               Layup(hoop=True),
                               Layup(passes=1, pattern_number=5, wind_angle=25.0, turnaround_angle=160.0))),
            5875, "c866ae9ea3506da3e80fdb1450f11f2ff7b31ccdec2586fa39cbc35555a652fd"),
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
        # A layup line from before the flag existed keeps its cycle count as written.
        old = {"layups": "1", "layup_1": "passes=12 pattern_number=3 wind_angle=45.0 turnaround_angle=270.0"}
        self.assertEqual(winding.layups_from_header(old), [Layup(passes=12)])


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
SLOW = WindingJob(max_surface_speed=60.0, turnaround_zone=0.0, layups=(
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
        # Never above Max Rotation Speed, and every rotating move uses (nearly)
        # all of it -- unless it runs at Max Feedrate instead (the 12 deg layer).
        for job in (SLOW, MULTI, WindingJob(turnaround_zone=80.0, optimize_trajectory=True),
                    excel_layer(12, 5, 4, zone=80.0)):
            with self.subTest(speed=job.max_surface_speed, zone=job.turnaround_zone):
                limit = job.max_a_speed
                moves = [(dx, dy, da, f) for dx, dy, da, f in gcode_moves(gcode(job)) if da]
                rates = [f * abs(da) / math.sqrt(dx * dx + dy * dy + da * da) for dx, dy, da, f in moves]
                self.assertLessEqual(max(rates), limit)
                for rate, (_, _, _, f) in zip(rates, moves):
                    self.assertTrue(rate > 0.99 * limit or f > 0.99 * job.max_feed, (rate, f))

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


class MaxFeedrate(unittest.TestCase):
    JOBS = (MULTI, excel_layer(12, 5, 4, zone=80.0), SLOW,
            WindingJob(max_feedrate=120.0, layups=(Layup(passes=2, wind_angle=30.0), Layup(passes=1, wind_angle=75.0))))

    def test_no_written_feed_exceeds_it(self):
        for job in self.JOBS:
            with self.subTest(feedrate=job.max_feedrate, layups=len(job.layups)):
                feeds = [f for *_, f in gcode_moves(gcode(job))]
                self.assertLessEqual(max(feeds), job.max_feed)

    def test_it_holds_the_carriage_back_at_low_angles(self):
        # 12 deg on a 250 mm tank: turning at Max Rotation Speed alone would run
        # the carriage at over 1 m/s. Capped, it runs at (nearly) Max Feedrate.
        job = excel_layer(12, 5, 4)
        x_speed, feed = mid_tank_speeds(job, gcode(job))[0]
        self.assertGreater(job.max_surface_speed / math.tan(math.radians(12)), 1000.0)
        self.assertLessEqual(x_speed, job.max_feedrate)
        self.assertGreater(x_speed, 0.95 * job.max_feedrate)
        self.assertEqual(feed, job.max_feed)

    def test_wind_speed_names_the_limit_the_program_runs_at(self):
        # wind_speed() predicts the mid-tank speed of every layup -- 20 deg is
        # held back by Max Feedrate, 45 and 70 deg turn at Max Rotation Speed.
        speeds = mid_tank_speeds(MULTI, gcode(MULTI))
        for i, layup in enumerate(MULTI.layups):
            ws = winding.wind_speed(MULTI, layup)
            with self.subTest(angle=layup.wind_angle):
                self.assertEqual(ws.limit, "feed" if layup.wind_angle < 30 else "rotation")
                self.assertAlmostEqual(speeds[i][0], ws.x_speed, delta=0.01 * ws.x_speed)
                self.assertAlmostEqual(speeds[i][1], ws.feed, delta=0.01 * ws.feed)
                limit, reached = (MULTI.max_feed, ws.feed) if ws.limit == "feed" else (MULTI.max_a_speed, ws.rotation)
                self.assertAlmostEqual(reached, limit, delta=1e-6 * limit)

    def test_the_limit_follows_both_settings(self):
        layup = Layup(wind_angle=30.0)
        self.assertEqual(winding.wind_speed(WindingJob(max_feedrate=300.0), layup).limit, "feed")
        self.assertEqual(winding.wind_speed(WindingJob(max_feedrate=500.0), layup).limit, "rotation")
        self.assertEqual(winding.wind_speed(WindingJob(max_feedrate=500.0, max_surface_speed=400.0), layup).limit, "feed")
        self.assertIsNone(winding.wind_speed(WindingJob(), Layup(wind_angle=90.0)))

    def test_estimated_time_is_the_time_the_file_takes(self):
        # The preview's time comes from the same capped feed rates.
        job = replace(excel_layer(12, 5, 4, zone=80.0), home_before_wind=False)
        wind = gcode_moves(gcode(job))[1:]  # after the move from the origin to the start
        written = sum(math.sqrt(dx * dx + dy * dy + da * da) / f * 60.0 for dx, dy, da, f in wind)
        self.assertAlmostEqual(winding.simulate(job).total_time, written, delta=0.001 * written)

    def test_travel_is_capped_too(self):
        # On a thin tank Max Rotation Speed is a huge A rate; the travel to the
        # start must still respect Max Feedrate.
        job = WindingJob(tank_diameter=40.0, end_cap_diameter=10.0, max_feedrate=50.0,
                         layups=(Layup(passes=1, wind_angle=45.0),))
        self.assertGreater(job.max_a_speed, job.max_feed)
        lines = gcode(job)
        setup = [ln for ln in lines[:lines.index("PAUSE")] if ln.startswith("G1")]
        self.assertTrue(all(float(ln.rsplit("F", 1)[1]) == job.max_feed for ln in setup))

    def test_validation_and_old_files(self):
        self.assertIn("Max Feedrate must be at least 1 mm/s.", winding.geometry_errors(WindingJob(max_feedrate=0.5)))
        # It never changes the path, so a file from before it existed simply
        # restores the default.
        self.assertNotIn("max_feedrate", winding.LEGACY_VALUES)
        self.assertIn(("max_feedrate", 300.0), winding.settings_items(WindingJob()))


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
        self.assertTrue(any(message in e for e in winding.geometry_errors(WindingJob(start_x_auto=False, start_x=1000.0))))
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


def partial(job, point, rehome=False):
    buf = io.StringIO()
    winding.write_gcode(buf, job, resume=winding.Resume(point, rehome))
    return buf.getvalue().splitlines()


class PauseAfterLayup(unittest.TestCase):
    def test_pause_between_layups_only(self):
        lines = gcode(TWO_LAYUPS)
        i = lines.index("; LAYUP_START:2")
        self.assertEqual(lines[i - 1], "PAUSE")
        self.assertEqual(lines[i - 3:i - 1][0], "G92 A0")  # after the last cycle's reset
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
        full = gcode(TWO_LAYUPS)
        rest = body[body.index("; LAYUP_START:1") + 2:]  # skip the cut-short first move
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


class TurnaroundZone(unittest.TestCase):
    def test_zone_replaces_the_pure_rotation_dwell(self):
        # With a zone, the only pure-rotation move left is the program's very
        # last one (nothing follows it to spread the rest of its rotation into),
        # and no rotation is lost or added along the way.
        plain = excel_layer(20, 7, 4)
        zoned = excel_layer(20, 7, 4, zone=80.0)
        moves = [ev for ev in winding.iter_program(zoned) if type(ev) is Move]
        n_dwell = sum(mv.dwell for mv in moves)  # that one move may be split in pieces
        self.assertGreaterEqual(n_dwell, 1)
        self.assertTrue(all(mv.dwell for mv in moves[-n_dwell:]))
        plain_final = [ev for ev in winding.iter_program(plain) if type(ev) is Move][-1].a
        self.assertAlmostEqual(moves[-1].a, plain_final, places=6)

    def test_zone_spreads_rotation_over_its_length(self):
        # The turnaround rotation happens across the whole zone: every step in
        # it turns the mandrel by more than the plain helix would.
        job = excel_layer(20, 7, 2, zone=80.0)
        plan = winding.plan_layup(job, job.layups[0])
        helix_step = winding.STEP_SIZE / plan.pitch * 360.0
        x_end = job.chuck_offset + job.tank_length
        prev, zone_steps = None, []
        for ev in winding.iter_program(job):
            if type(ev) is Move and prev is not None and ev.circuit == 0 and ev.x > prev.x and ev.x > x_end - 80.0 + 1e-9:
                zone_steps.append(ev.a - prev.a)
            prev = ev if type(ev) is Move else prev
        self.assertEqual(len(zone_steps), 16)
        self.assertTrue(all(step > helix_step + 1.0 for step in zone_steps))

    def test_zone_validation(self):
        self.assertEqual(winding.geometry_errors(excel_layer(20, 7, 4, zone=740.0)), [])
        self.assertIn("Turnaround Zone must be at most half the Tank Length.",
                      winding.geometry_errors(excel_layer(20, 7, 4, zone=745.0)))
        self.assertIn("Turnaround Zone can't be negative.", winding.geometry_errors(excel_layer(20, 7, 4, zone=-1.0)))

    def test_preview_runs_still_split_at_each_turnaround(self):
        result = winding.simulate(excel_layer(20, 7, 2, zone=80.0))
        self.assertTrue(all(len(runs) == 2 for runs in result.layups[0].strand_runs))


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
            self.assertTrue(x_start - 1e-9 <= mv.x <= x_end + 1e-9)
            self.assertTrue(0.0 <= mv.y <= winding.Y_TRAVEL)
            prev_a, prev_layup = mv.a, mv.layup
        for li, layup in enumerate(MULTI.layups):
            circuits = {mv.circuit for mv in moves if mv.layup == li}
            self.assertEqual(circuits, set(range(layup.passes * layup.pattern_number)))

    def test_optimize_trajectory_conserves_rotation(self):
        # Blending moves dwell rotation around but never adds or loses any -- also
        # across layup transitions, where the blend carries into the next layup.
        plain = [ev for ev in winding.iter_program(MULTI) if type(ev) is Move]
        eased_job = WindingJob(**{k: getattr(MULTI, k) for k in winding.GLOBAL_KEYS if k != "optimize_trajectory"},
                               optimize_trajectory=True, layups=MULTI.layups)
        eased = [ev for ev in winding.iter_program(eased_job) if type(ev) is Move]
        self.assertEqual(len(plain), len(eased))
        self.assertAlmostEqual(plain[-1].a, eased[-1].a, places=6)

    def test_gcode_markers_and_axis_resets(self):
        lines = gcode(MULTI)
        self.assertEqual([ln for ln in lines if ln.startswith("; LAYUP_START")],
                         ["; LAYUP_START:1", "; LAYUP_START:2", "; LAYUP_START:3"])
        # One reset per cycle within the wind (plus the one zeroing A at resume).
        wind = lines[lines.index("; LAYUP_START:1"):]
        self.assertEqual(sum(ln == "G92 A0" for ln in wind), MULTI.total_cycles)
        self.assertEqual(lines[-2:], [f"; CYCLE_COMPLETE:{MULTI.total_cycles}", "G92 A0"])
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
        self.assertEqual(winding.layups_from_header(legacy), [Layup(10, 3, 45.0, 270.0)])
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
        self.assertLess(moves[2][0].x, last)            # ...the next layup winds back from there
        self.assertAlmostEqual(moves[2][-1].x, x_end)   # and, flipped, ends at the far end
        self.assertAlmostEqual(moves[3][-1].x, first)   # hoop 2 runs back to the chuck side
        self.assertGreater(moves[4][0].x, first)        # so the last layup starts there again
        self.assertAlmostEqual(moves[4][-1].x, x_start)

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

    def test_lead_in_runs_at_the_previous_layups_angle(self):
        moves = layup_moves(HOOPS)
        first, last = HOOPS.hoop_span
        for hoop, before in ((1, 0), (3, 2)):
            lead = [m for m in moves[hoop] if not first - 1e-9 <= m.x <= last + 1e-9]
            lead = [moves[before][-1]] + lead + [next(m for m in moves[hoop] if first - 1e-9 <= m.x <= last + 1e-9)]
            with self.subTest(hoop=hoop + 1):
                self.assertGreater(len(lead), 5)  # over the whole dome
                pitch = winding.plan_layup(HOOPS, HOOPS.layups[before]).pitch
                for rate in deg_per_mm(lead):
                    self.assertAlmostEqual(rate, 360.0 / pitch, places=6)
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
                                              turnaround_angle=30.0, auto_cycles=True)))
        self.assertEqual(body(gcode(a)), body(gcode(b)))
        self.assertEqual(winding.geometry_errors(WindingJob(layups=(Layup(hoop=True, wind_angle=95.0, passes=0),))), [])

    def test_straight_section_too_short(self):
        # Round caps on a 200 mm tank: domes of ~97 mm each leave 6 mm straight.
        job = WindingJob(tank_length=200.0, bandwidth=8.0, turnaround_zone=0.0, layups=(Layup(), Layup(hoop=True)))
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
        lead_in = layup_moves(HOOPS)[1]
        lead_in_rotation = next(m.a for m in lead_in if abs(m.x - first) < 1e-9) - location.a_offset
        self.assertAlmostEqual(location.x, first + (20000.0 - lead_in_rotation) / 360.0 * HOOPS.bandwidth, places=6)
        part = partial(HOOPS, point)
        full = gcode(HOOPS)
        rest = part[part.index("; LAYUP_START:2") + 2:]
        self.assertEqual(full[full.index(rest[0]):], rest)
        progress = winding.progress(HOOPS, winding.ProgramPoint(2, 0, 0.0))
        self.assertNotIn(1, progress.covered)  # drawn band by band, not as a solid tank
        self.assertIn(1, progress.runs)


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
        job = WindingJob(tank_length=winding.MAX_X)
        self.assertEqual(winding.geometry_errors(job), [])
        self.assertEqual(len(winding.validate(job)), 1)

    def test_values_are_normalized(self):
        self.assertEqual(Layup(passes=4.0, wind_angle=45), Layup(passes=4, wind_angle=45.0))
        self.assertIsInstance(WindingJob(tank_length=1000).tank_length, float)


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


if __name__ == "__main__":
    unittest.main()
