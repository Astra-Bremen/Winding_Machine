"""Material and rough strength estimates for a wound tank.

The method is the old Excel generator's "Rough Strength assumption (assumes 54
degree equivalent winding)" (sheet cells M1:N8), fed with this app's own wind:

    fiber mass      = tow length x tow linear density                  (E63)
    resin mass      = fiber mass x (1 - fiber fraction) / fiber fraction (F63)
    wall thickness  = composite mass / (area x composite density)        (N3)
    hoop stress     = pressure x radius / thickness  (thin-walled tank)  (N5)
    allowable stress = fiber strength x fiber fraction
                       x strength translation x laminate factor          (N7)
    safety factor   = allowable stress / hoop stress                     (N8)

One deliberate difference: the sheet spreads the whole tank's composite mass
over the cylinder (its thickness formula adds the dome area without
multiplying it by the density, so the domes count for almost nothing). The
dome and turnaround material doesn't carry the cylinder's hoop stress, so here
only the fiber actually laid on the straight section counts toward its
thickness. The sheet's figure is still worked out (`sheet_thickness`,
`sheet_safety_factor`) for comparison.

A rough estimate, like the sheet's: not a substitute for a proper design
analysis or burst testing.
"""
import math
from dataclasses import dataclass, fields
from typing import NamedTuple


@dataclass(frozen=True)
class Material:
    """The estimate's inputs. Defaults are the Excel sheet's values."""
    tow_density: float = 1.6           # g per m of tow (sheet D4, "fiber mass per meter")
    fiber_fraction: float = 0.55       # fiber share of the composite's mass (D5, "fiber to resin ratio")
    composite_density: float = 1750.0  # kg/m3 of the cured composite (N2, "Material density")
    fiber_strength: float = 5100.0     # MPa (N6, "Fiber Ultimate Tensile Strength")
    translation: float = 0.85          # share of the fiber strength the laminate realizes (N7's 0.85)
    laminate_factor: float = 0.30      # share of that the winding carries in hoop direction (N7's 0.3, "54 degree equivalent")
    pressure: float = 70.0             # operating pressure, bar (N4, "Operational Pressure")

    def __post_init__(self):
        for f in fields(self):
            object.__setattr__(self, f.name, float(getattr(self, f.name)))


KEYS = tuple(f.name for f in fields(Material))
HEADER_PREFIX = "estimate_"


def errors(material):
    """Why these inputs can't give an estimate (empty if they can)."""
    m, problems = material, []
    if m.tow_density <= 0: problems.append("Tow linear density must be greater than 0.")
    if not 0 < m.fiber_fraction <= 1: problems.append("Fiber mass fraction must be between 0 and 1.")
    if m.composite_density <= 0: problems.append("Composite density must be greater than 0.")
    if m.fiber_strength <= 0: problems.append("Fiber tensile strength must be greater than 0.")
    if not 0 < m.translation <= 1: problems.append("Strength translation must be between 0 and 1.")
    if not 0 < m.laminate_factor <= 1: problems.append("Laminate factor must be between 0 and 1.")
    if m.pressure <= 0: problems.append("Operating pressure must be greater than 0.")
    return problems


def masses(tow_mm, material):
    """(fiber, resin) mass in kg for `tow_mm` of tow."""
    fiber = tow_mm / 1000.0 * material.tow_density / 1000.0
    return fiber, fiber * (1.0 - material.fiber_fraction) / material.fiber_fraction


class Estimate(NamedTuple):
    fiber_mass: float        # kg, whole tank
    resin_mass: float        # kg
    thickness: float         # mm, composite wall on the straight section (None if there's none)
    hoop_stress: float       # MPa at the operating pressure (None without a thickness)
    allowable_stress: float  # MPa
    safety_factor: float     # allowable / hoop stress (None without a thickness)
    burst_pressure: float    # bar: the pressure at which the hoop stress reaches the allowable
    sheet_thickness: float   # mm, the Excel sheet's way (whole tank's mass over the cylinder)
    sheet_safety_factor: float

    @property
    def total_mass(self):
        return self.fiber_mass + self.resin_mass


def estimate(job, result, material):
    """The estimate for a winding.WindingJob from its winding.SimulationResult."""
    m = material
    fiber, resin = masses(result.total_tow, m)
    cyl_fiber, cyl_resin = masses(result.cylinder_tow, m)
    radius = job.tank_diameter / 2.0                                   # mm
    cyl_length = job.tank_length - 2.0 * job.dome_length               # mm
    cyl_area = math.pi * job.tank_diameter / 1000.0 * max(0.0, cyl_length) / 1000.0  # m2
    pressure = m.pressure / 10.0                                       # MPa
    allowable = m.fiber_strength * m.fiber_fraction * m.translation * m.laminate_factor

    def from_thickness(t):
        if not t or t <= 0:
            return None, None, None
        hoop = pressure * radius / t
        return hoop, allowable / hoop, m.pressure * allowable / hoop

    thickness = (cyl_fiber + cyl_resin) / (cyl_area * m.composite_density) * 1000.0 if cyl_area > 0 else None
    hoop, sf, burst = from_thickness(thickness)
    # The sheet's N3: its dome term (a sphere of the tank's diameter) is added
    # without the density, i.e. practically all mass counts on the cylinder.
    sphere_area = 4.0 * math.pi * (job.tank_diameter / 2000.0) ** 2
    sheet_denominator = cyl_area * m.composite_density + sphere_area
    sheet_thickness = (fiber + resin) / sheet_denominator * 1000.0 if sheet_denominator > 0 else None
    sheet_sf = from_thickness(sheet_thickness)[1]
    return Estimate(fiber, resin, thickness, hoop, allowable, sf, burst, sheet_thickness, sheet_sf)


# --- G-code settings header ---

def header_items(material, result=None):
    """Settings-header entries: the inputs (restored when the file is reopened)
    and, when available, what they gave -- so the file shows the calculation
    it was made with."""
    items = [(HEADER_PREFIX + key, getattr(material, key)) for key in KEYS]
    if result is not None:
        e = result
        for key, value in (("fiber_mass_kg", e.fiber_mass), ("resin_mass_kg", e.resin_mass),
                           ("thickness_mm", e.thickness), ("hoop_stress_mpa", e.hoop_stress),
                           ("safety_factor", e.safety_factor), ("burst_pressure_bar", e.burst_pressure)):
            if value is not None:
                items.append((f"{HEADER_PREFIX}result_{key}", f"{value:.3f}"))
    return items


def material_from_header(settings):
    """The Material recorded in a parsed settings header, or None if the file
    has none (it keeps the defaults for anything missing)."""
    values = {}
    for key in KEYS:
        raw = settings.get(HEADER_PREFIX + key)
        if raw is not None:
            try:
                values[key] = float(raw)
            except ValueError:
                pass
    return Material(**values) if values else None
