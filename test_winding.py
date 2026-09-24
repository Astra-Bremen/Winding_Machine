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


def circuit_starts(job):
    # {(layup, circuit): continuous A angle at the circuit's first move}
    starts, prev_a, prev_key = {}, 0.0, None
    for ev in winding.iter_program(job):
        if type(ev) is Move:
            key = (ev.layup, ev.circuit)
            if key != prev_key:
                starts[key] = prev_a
                prev_key = key
            prev_a = ev.a
    return starts


MULTI = WindingJob(layups=(
    Layup(passes=3, pattern_number=3, wind_angle=45.0, turnaround_angle=270.0),
    Layup(passes=2, pattern_number=5, wind_angle=70.0, turnaround_angle=90.0),
    Layup(passes=2, pattern_number=1, wind_angle=20.0, turnaround_angle=180.0),
))


class SingleLayupGoldenMaster(unittest.TestCase):
    """A single-layup program must keep producing exactly the G-code the original
    single-pattern implementation produced. The hashes are of its output body
    (everything after the settings header)."""
    CASES = {
        "default": (WindingJob(), 12083, "861e6ddb5e7c436ddf58604cb7f4c806991b99daadaf3662f42c039662cb208a"),
        "flat_p1_optimized_nohome": (
            WindingJob(end_cap_type="Flat", optimize_trajectory=True, home_before_wind=False,
                       layups=(Layup(passes=4, pattern_number=1, wind_angle=60.0, turnaround_angle=180.0),)),
            1619, "40bb511604baa5725f386104d51f1c65ac11f82123b98937b85f8cbb21ae77b3"),
        "round_p5_steep_optimized": (
            WindingJob(bandwidth=8.0, optimize_trajectory=True,
                       layups=(Layup(passes=2, pattern_number=5, wind_angle=70.0, turnaround_angle=90.0),)),
            4027, "ea61a69f8e365d7a714d51b1904b43d5f7d2fe5506579dfbf9a60506861d3270"),
        "round_small_dwell": (
            WindingJob(tank_length=640.0, tank_diameter=160.0, end_cap_diameter=40.0, eye_width=35.0,
                       min_spacing=6.0, max_surface_speed=150.0,
                       layups=(Layup(passes=3, pattern_number=2, wind_angle=30.0, turnaround_angle=10.0),)),
            1557, "21b0c143dd4376ade62684ccfdd2e245fcf148b0a9f0a9fb1dd24143ea474272"),
    }

    def test_output_unchanged(self):
        for name, (job, n_lines, digest) in self.CASES.items():
            with self.subTest(name):
                lines = body(gcode(job))
                self.assertEqual(len(lines), n_lines)
                self.assertEqual(hashlib.sha256("\n".join(lines).encode()).hexdigest(), digest)


class MultiLayupProgram(unittest.TestCase):
    def test_every_layup_keeps_its_own_pattern(self):
        # Within each layup, consecutive circuit start angles must step by exactly
        # 360/pattern_number within a cycle, and by the one-band shift between
        # cycles -- the same guarantee a single-layup program has.
        starts = circuit_starts(MULTI)
        for li, layup in enumerate(MULTI.layups):
            plan = winding.plan_layup(MULTI, layup)
            n = layup.pattern_number
            for i in range(1, plan.total_circuits):
                target = plan.shift_degrees if i % n == 0 else 360.0 / n
                step = (starts[(li, i)] - starts[(li, i - 1)] - target) % 360.0
                self.assertAlmostEqual(min(step, 360.0 - step), 0.0, places=6, msg=f"layup {li + 1}, circuit {i}")

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
