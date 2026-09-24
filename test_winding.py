"""Headless tests for the winding core (no Tk needed): python -m unittest -v"""
import hashlib
import io
import math
import unittest

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
    and only together with a change that is meant to alter the output. (They
    were last regenerated when pattern alignment switched to the skip/closing
    shift method; that kept every move and only changed its angles.)"""
    CASES = {
        "default": (WindingJob(), 12083, "e24b815801de0318481f53d7832537fd4f2cfed74af42e8be888929015e78b16"),
        "flat_p1_optimized_nohome": (
            WindingJob(end_cap_type="Flat", optimize_trajectory=True, home_before_wind=False,
                       layups=(Layup(passes=4, pattern_number=1, wind_angle=60.0, turnaround_angle=180.0),)),
            1619, "ae063326f2efa6d2c0c2c38c55279721457eda0e226e61f7ceb29f7707a258fd"),
        "round_p5_steep_optimized": (
            WindingJob(bandwidth=8.0, optimize_trajectory=True,
                       layups=(Layup(passes=2, pattern_number=5, wind_angle=70.0, turnaround_angle=90.0),)),
            4027, "5e4e3055ec0f089d9b723b3d8df4966249d7c5a149b8a3caeb21cbd64fece696"),
        "round_small_dwell": (
            WindingJob(tank_length=640.0, tank_diameter=160.0, end_cap_diameter=40.0, eye_width=35.0,
                       min_spacing=6.0, max_surface_speed=150.0,
                       layups=(Layup(passes=3, pattern_number=2, wind_angle=30.0, turnaround_angle=10.0),)),
            1557, "046b2ac743628ce1d5917727c294830f5131b3eab041686aa59d2813aad0d542"),
        "excel_style_zone": (
            WindingJob(tank_length=1640.0, tank_diameter=250.0, bandwidth=7.0, turnaround_zone=80.0,
                       layups=(Layup(passes=3, pattern_number=5, wind_angle=12.0, turnaround_angle=156.0),
                               Layup(passes=2, pattern_number=7, wind_angle=54.0, turnaround_angle=72.0))),
            19038, "a4fde294cb2e47fc9e7fb5c43b13fb0d0d2f4a309458af01c213dfefac8acb15"),
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


class TurnaroundZone(unittest.TestCase):
    def test_zone_replaces_the_pure_rotation_dwell(self):
        # With a zone, the only pure-rotation move left is the program's very
        # last one (nothing follows it to spread the rest of its rotation into),
        # and no rotation is lost or added along the way.
        plain = excel_layer(20, 7, 4)
        zoned = excel_layer(20, 7, 4, zone=80.0)
        moves = [ev for ev in winding.iter_program(zoned) if type(ev) is Move]
        self.assertEqual(sum(mv.dwell for mv in moves), 1)
        self.assertTrue(moves[-1].dwell)
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
        self.assertEqual(sum(ln == "G92 A0" for ln in lines), MULTI.total_cycles)
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
