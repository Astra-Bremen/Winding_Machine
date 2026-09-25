"""Headless tests for the material/strength estimate: python -m unittest -v"""
import io
import unittest
from types import SimpleNamespace

import strength
import winding
from winding import Layup, WindingJob

# The Excel sheet's own case: a 250 mm tank, 2000 mm long, 2410.678 m of tow.
SHEET_TOW_MM = 2410677.8441808274
SHEET_JOB = SimpleNamespace(tank_diameter=250.0, tank_length=2000.0, dome_length=0.0)


class ExcelSheet(unittest.TestCase):
    def test_defaults_are_the_sheets_values(self):
        m = strength.Material()
        self.assertEqual((m.tow_density, m.fiber_fraction, m.composite_density, m.fiber_strength,
                          m.translation, m.laminate_factor, m.pressure), (1.6, 0.55, 1750.0, 5100.0, 0.85, 0.3, 70.0))

    def test_reproduces_the_sheets_numbers(self):
        # Sheet cells E63, F63, N3, N5, N7, N8.
        result = SimpleNamespace(total_tow=SHEET_TOW_MM, cylinder_tow=SHEET_TOW_MM)
        e = strength.estimate(SHEET_JOB, result, strength.Material())
        self.assertAlmostEqual(e.fiber_mass, 3.857084550689324, places=9)
        self.assertAlmostEqual(e.resin_mass, 3.1557964505639915, places=9)
        self.assertAlmostEqual(e.sheet_thickness, 2.550982762179171, places=9)
        self.assertAlmostEqual(e.allowable_stress, 715.275, places=9)
        self.assertAlmostEqual(e.sheet_safety_factor, 2.0853190802488077, places=9)


class Estimate(unittest.TestCase):
    def test_only_the_straight_section_counts_toward_the_wall(self):
        # Half the tow on the cylinder: half the wall, half the safety factor.
        full = SimpleNamespace(total_tow=SHEET_TOW_MM, cylinder_tow=SHEET_TOW_MM)
        half = SimpleNamespace(total_tow=SHEET_TOW_MM, cylinder_tow=SHEET_TOW_MM / 2)
        m = strength.Material()
        a, b = strength.estimate(SHEET_JOB, full, m), strength.estimate(SHEET_JOB, half, m)
        self.assertAlmostEqual(b.thickness, a.thickness / 2, places=9)
        self.assertAlmostEqual(b.safety_factor, a.safety_factor / 2, places=9)
        self.assertEqual(a.fiber_mass, b.fiber_mass)  # the mass is the whole tank's either way

    def test_burst_pressure_is_where_the_stress_reaches_the_allowable(self):
        result = SimpleNamespace(total_tow=SHEET_TOW_MM, cylinder_tow=SHEET_TOW_MM)
        e = strength.estimate(SHEET_JOB, result, strength.Material(pressure=100.0))
        self.assertAlmostEqual(e.burst_pressure, 100.0 * e.safety_factor, places=9)
        low = strength.estimate(SHEET_JOB, result, strength.Material(pressure=50.0))
        self.assertAlmostEqual(low.burst_pressure, e.burst_pressure, places=6)  # a property of the wall

    def test_simulation_counts_tow_on_the_straight_section(self):
        flat = winding.simulate(WindingJob(end_cap_type="Flat", layups=(Layup(passes=2),)))
        self.assertAlmostEqual(flat.cylinder_tow, flat.total_tow, places=6)  # no domes: all of it
        round_ = winding.simulate(WindingJob(layups=(Layup(passes=2),)))
        self.assertLess(round_.cylinder_tow, round_.total_tow)
        self.assertGreater(round_.cylinder_tow, 0.5 * round_.total_tow)
        self.assertAlmostEqual(sum(l.cylinder_tow for l in round_.layups), round_.cylinder_tow, places=6)

    def test_no_straight_section_no_rating(self):
        job = SimpleNamespace(tank_diameter=250.0, tank_length=250.0, dome_length=125.0)
        e = strength.estimate(job, SimpleNamespace(total_tow=1e5, cylinder_tow=0.0), strength.Material())
        self.assertIsNone(e.safety_factor)
        self.assertGreater(e.fiber_mass, 0)

    def test_input_validation(self):
        self.assertEqual(strength.errors(strength.Material()), [])
        self.assertIn("Fiber mass fraction must be between 0 and 1.",
                      strength.errors(strength.Material(fiber_fraction=1.5)))
        self.assertIn("Operating pressure must be greater than 0.", strength.errors(strength.Material(pressure=0)))


class GcodeHeader(unittest.TestCase):
    def test_inputs_and_results_round_trip_through_the_file(self):
        job = WindingJob(layups=(Layup(passes=2),))
        material = strength.Material(fiber_strength=4900.0, pressure=200.0)
        e = strength.estimate(job, winding.simulate(job), material)
        buf = io.StringIO()
        winding.write_gcode(buf, job, extra_header=strength.header_items(material, e))
        lines = buf.getvalue().splitlines()
        settings = {}
        for ln in lines[1:lines.index("; -----------------------")]:
            key, value = ln[1:].split(":", 1)
            settings[key.strip()] = value.strip()
        self.assertEqual(strength.material_from_header(settings), material)
        self.assertEqual(settings["estimate_result_safety_factor"], f"{e.safety_factor:.3f}")
        self.assertIsNone(strength.material_from_header({"tank_length": "1000"}))


if __name__ == "__main__":
    unittest.main()
