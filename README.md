# Winding_Machine

**CFRP Winder** is a desktop tool that generates and previews G-code for a
3-axis composite filament winding machine running Klipper. You enter the
machine, tank and winding parameters, and build up the wind as one or more
**layups**. The app shows a live preview of the winding pattern with time and
material estimates, writes a `.gcode` file, and can play that file back as a
simulation.

## Machine model

The generator assumes a winder with three axes:

| Axis | Motion | Unit |
|------|--------|------|
| **X** | Carriage moves along the mandrel axis | mm (hard travel limit `MAX_X = 5250`) |
| **Y** | Delivery eye moves toward/away from the mandrel | mm, clamped to `0 … 180` |
| **A** | Mandrel rotates | degrees |

The Y-axis zero position is 550 mm from the mandrel centerline, minus the eye
arm length. The eye tip can therefore sit between `550 − arm − 180` and
`550 − arm` mm from the tank axis. These values are hard-coded; see
[Notes for development](#notes-for-development).

## Features

- **Tank geometry:** cylindrical tank with **Round** (spherical-dome) or
  **Flat** end caps. Set the tank length, the tank diameter and the end-cap
  (polar opening) diameter.
- **Helical winding:** set the winding angle, the tow/band width and the
  pattern number (strands per cycle). Each cycle lays `pattern_number` evenly
  spaced circuits around the circumference. After each cycle the pattern moves
  on by an equal share of the gap between two strands, so the layup's cycles
  fill the surface evenly and close exactly on themselves. See
  [Pattern alignment](#pattern-alignment).
- **Multi-layup programs:** a wind is a sequence of layups, wound one after the
  other on the same tank. Each layup has its own number of cycles, pattern
  number, winding angle and turnaround dwell angle, so one program can combine,
  for example, a 45° helical layup with a near-hoop layup. The tow width, the
  turnaround zone, the Optimize Trajectory setting and all machine and tank
  settings apply to the whole program. Each layup keeps its own pattern
  alignment, and two identical layups in a row wind exactly on top of each
  other, like repeated layers.
- **Automatic cycle count:** a layup's *Number of Cycles* is set automatically
  to the fewest cycles that cover the tank 100 %, and follows any change to the
  tank diameter, tow width, winding angle or pattern number. It is shown in
  grey with an **AUTO** tag inside the field. Type a number to fix it: it turns
  to normal text and no longer changes on its own. To return it to auto, clear
  the field and click out of it (or press Enter). New layups always start on
  auto.
- **Wind start and fiber attachment:** with *Home Axes (G28) Before Winding*
  on, the wind starts at *Start Wind at X*. By default (auto, same field
  behavior as above) that is where the chuck-side dome ends and the tank turns
  straight. After homing, the machine pulls the eye fully back, travels to
  that X, moves the eye in to winding distance, and runs `PAUSE`. Attach the
  fiber, then resume on the machine: the wind runs from there toward +X. The
  mandrel's angle at resume becomes the wind's zero, so turning it by hand
  while attaching the fiber is fine. The Settings Preview marks the start on
  the tank when Layup 1 is shown. Without homing, the wind starts at the tank's
  end as before.
- **Pattern-correct, balanced turnarounds:** each turnaround has a minimum
  dwell angle. The app adds a small extra rotation so that every circuit
  starts exactly on the pattern, and gives both tank ends the same turnaround
  on every circuit.
- **Turnaround zone:** optionally spreads each turnaround over the last few
  centimetres of travel at each end instead of one pure rotation at the very
  end. This is the old Excel generator's "bulkhead height compensation", and
  it keeps fiber from building up in a ring at the turnaround.
- **Eye clearance:** the Y position follows the tank profile plus a safety
  gap. It uses the largest radius under the full width of the eye, so the eye
  cannot hit the tank at the domes.
- **Surface-speed control:** the speed limit is a surface speed in mm/s,
  converted to an A-axis rate for the current diameter. Every feed rate is
  computed from the coordinates exactly as written to the file, and rounded
  down, so no move ever turns the mandrel faster than this limit (each still
  runs within a fraction of a percent of it).
- **Responsive pause (Max Move Time):** Klipper can't interrupt a move it has
  already queued. It keeps about 2 s of motion queued, and each queued move
  always runs to its end. So after a pause, the machine keeps going for up to
  about 2 s plus the length of the move in progress. Any move that would take
  longer than *Max Move Time* (default 0.5 s) is split into equal straight
  pieces. The path, speed, time and pattern stay exactly the same, and a pause
  takes effect within about 2.5 s. Near-hoop layups and turnarounds at low
  rotation speeds benefit most; otherwise their single moves can take several
  seconds each.
- **Wind-angle validation:** angles that the tank size and band width cannot
  produce are corrected automatically. At angles that are too steep, each wrap
  would lie on top of the previous one. At angles that are too shallow, the
  strand barely moves around the tank.
- **Optimize Trajectory (experimental):** moves part of each dwell rotation
  into the X steps just before and after it. The motion planner then does not
  have to slow almost to a stop at the turnaround, and the winding pattern
  stays exactly the same. It has no effect while a turnaround zone is set,
  because the zone already spreads out the whole turnaround.
- **Live Settings Preview:** a pseudo-3D tank view that you can rotate, an
  end-on view showing the eye reach, and live estimates. The estimates come in
  two groups:
  - **Program:** total time, required tow, total cycles, rotation speed and eye
    reach for the whole wind.
  - **Selected layup:** its time, time per cycle, required tow, X speed, cycles
    needed for full coverage, coverage (shown in orange while gaps would
    remain) and extra turnaround rotation.

  Click the rotation or X speed to change units. The preview shows the first
  cycle of the selected layup. Check **Show All Layups** to overlay every
  layup in its own color, with a legend underneath; click a legend entry to
  select that layup. The simulation runs on a background thread, so the
  interface stays responsive. Switching layups redraws instantly from the
  cached result without re-simulating.
- **G-Code Preview:** load any generated file and play or scrub through it.
  You can jump to a line or cycle and see the coordinates, elapsed time and
  current layup. Each layup is drawn in its own color. There is an optional 3D
  mode that spins with the mandrel. Loading a file also restores its settings,
  including every layup, into the settings panel. Files from before
  multi-layup support load as a single layup.
- Light and dark themes (View menu).

## Generated G-code

```gcode
; --- WINDER SETTINGS ---
; chuck_offset: 50.0          <- every setting is saved as a comment, so a
; ...                             file can be reopened and its settings restored
; start_x: 146.82458365518542
; start_x_auto: True
; ...
; max_move_time: 0.5
; turnaround_zone: 80.0
; optimize_trajectory: False
; layups: 2
; layup_1: passes=30 pattern_number=3 wind_angle=45.0 turnaround_angle=270.0 auto_cycles=True
; layup_2: passes=4 pattern_number=5 wind_angle=75.0 turnaround_angle=270.0 auto_cycles=False
; ld: 96.825                  <- computed dome length
; -----------------------
G28                           <- or just "G92 A0" if "Home Axes Before Winding" is off
; --- MOVE TO WIND START ---
G1 Y0.000 F...                <- pull the eye fully back first
G1 X48.942 F...               <- travel to Start Wind at X (in pieces, see Max Move Time)
...
G1 Y70.000 F...               <- then move in to winding distance
PAUSE                         <- attach the fiber, resume on the machine
G92 A0                        <- the mandrel's angle at resume is the wind's zero
G1 X146.825 Y70.000 A0.000 F...   <- the start position
; LAYUP_START:1
G1 X151.825 Y... A... F...    <- traversal in 5 mm X steps, toward +X
...
G1 X... Y... A... F...        <- turnaround: extra rotation on the steps of the zone
...                              (or one A-only move at the end without a zone)
; CYCLE_COMPLETE:1            <- cycles are numbered across the whole program
G92 A0                        <- reset A after each cycle so the value never grows too large
...
; LAYUP_START:2
...
```

`test_winding.py` pins the exact output of several reference programs, so an
unintended change to the motion shows up in the tests.

The suggested file name records the time of generation, the end-cap type and
the estimated duration, for example `17_09_14_32-RND-00_07_26.gcode`.

## Pattern alignment

A layup with pattern number *p* has *p* evenly spaced slots around the tank,
360°/*p* apart. Each circuit (out to the far end and back) turns the mandrel
by the helix rotation of both traverses, plus at least the dwell angle at each
turnaround. The app then adds the smallest **extra rotation** that makes the
circuit's total a whole number of slots (the **skip**). So the next circuit
always starts on a slot, but not necessarily the neighbouring one. The skip is
chosen coprime with *p*, so every cycle visits all *p* slots.

After each cycle, the pattern moves on by (360°/*p*) ÷ *number of cycles*. With
at least *Cycles for Full Coverage* cycles, the bands overlap evenly all the
way around. The last cycle then meets the first exactly, and a following
identical layup lands exactly on top of this one.

The far-end turnaround is identical on every circuit. The chuck-end turnaround
matches it to within one cycle shift, which is a fraction of a band. Over a
layup, both ends get the same total rotation.

### Relation to the old Excel generator

The earlier spreadsheet generator ("Winding G-Code … PN0003.xlsx") used the
same skip: its *degrees per cycle* is the next multiple of the *cycle width*
360°/*p* above the minimum circuit rotation. It also used a turnaround zone:
its two *bulkhead height compensation* moves run 80 mm out over the bulkhead
and back, each with a quarter of the circuit's turnaround rotation. The app
adopts both. It differs in two ways:

- **The cycle shift is applied once per cycle.** The spreadsheet spread it as
  a small constant subtraction from every circuit. That gives each pattern slot
  a different sub-band offset, and leaves uncovered strips at some slot seams:
  on the spreadsheet's own layers about 1.0 mm (12°), 2.6 mm (20°) and 7.3 mm
  (54°) wide. Applying the shift per cycle tiles the bands exactly, at the cost
  of the chuck-end turnaround being a few degrees longer once per cycle.
- **The skip is always coprime with the pattern number.** The spreadsheet
  didn't check this. It worked because its pattern numbers (5 and 7) are prime.

The spreadsheet's minimum turnaround rule, 2 × (90° − winding angle) per end,
is a good starting value for a layup's dwell angle.

## Installation

Requires Python 3 with Tkinter (developed and tested on 3.13).

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

`ttkbootstrap` also installs Pillow, which the app uses to draw its window icon.

## Usage

```powershell
python main.py
```

1. Fill in **Machine**, **Tank** and **Winding Settings** in the left panel.
   The Settings Preview updates as you type.
2. Set up the first layup in the **Layup** section. To add another, click
   **+** in the section header. The new layup copies every setting from the
   last layup, so you usually only change what differs. Its *Number of
   Cycles* starts on auto (grey, tagged **AUTO**), so it covers the tank
   completely. Type a number only if you want a different count. Use **‹ ›**
   to switch between layups and **−** to remove the selected one. The colored
   swatch next to the title matches that layup's color in the previews.
3. Check the estimates. *Cycles for Full Coverage* shows how many cycles the
   selected layup needs to cover the tank completely, and *Coverage* shows how
   much its current number of cycles covers. Check **Show All Layups** to see
   every layup on the tank at once.
4. Click **Generate G-Code** and choose where to save the file. The file
   opens in the G-Code Preview automatically.
5. Run it on the machine. With homing on, it homes, moves the eye to the wind
   start and pauses: attach the fiber there, then resume on the machine.
6. Use **Open G-Code** to inspect a file generated earlier.

## Tests

The winding math has headless tests (no GUI needed):

```powershell
python -m unittest -v test_winding
```

## Project structure

| File | Purpose |
|------|---------|
| `main.py` | App entry point and `CFRPWinderApp`: window layout and menus, the settings model (global settings plus one set of variables per layup), and G-code file output. |
| `winding.py` | All winding math, with no GUI dependency: the `WindingJob`/`Layup` settings, validation, geometry, dwell/pattern alignment, trajectory blending, the motion generator (`iter_program`), the preview simulation (`simulate`), and the G-code writer and header parser. |
| `plan_tab.py` | `PlanTab`: settings form with the layup switcher, live Settings Preview (3D tank, end view, estimates, Show All Layups legend), wind-angle validation and the background calculation thread. |
| `view_tab.py` | `ViewTab`: G-code parser and playback viewer. |
| `theme.py` | ttkbootstrap theme setup, canvas color palettes (including the layup colors) and the generated app icon. |
| `test_winding.py` | Headless tests for `winding.py`. |

## Notes for development

- **Pattern and turnaround math:** `plan_layup()` derives each layup's skip,
  cycle shift and extra rotations (`compute_pattern_skip`,
  `compute_dwell_extras`), and `compute_turnaround_balance_offset` splits the
  extra between the two ends. `compute_dwell_blend` decides where a turnaround's
  rotation happens: at the end, eased by Optimize Trajectory, or spread over
  the turnaround zone. The tests in `PatternAlignment` check the tiling and the
  balance between the two ends directly from the generated motion.
- **One motion generator:** `winding.iter_program()` produces every move of
  the program. The G-code writer and the preview simulation both use it, so a
  change to the motion only has to be made there, and the preview always
  matches the file.
- **Adding a setting:** add a field to `WindingJob` (global) or `Layup` (per
  layup) in `winding.py`, then add its entry field to the matching field list
  at the top of `plan_tab.py`. The Tk variables, the settings header and
  restoring settings from a file all follow from those two definitions.
- **Custom G-code macros:** `START_GCODE` and `END_GCODE` at the top of
  `CFRPWinderApp` in `main.py` are inserted into every file.
  `ONE_WIND_COMPLETE_GCODE` is defined but **not yet used** anywhere.
- **Machine constants:** `MAX_X`, the 550 mm Y reference (`Y_REFERENCE`), the
  0–180 mm Y range (`Y_TRAVEL`), the 5 mm traversal step (`STEP_SIZE`) and the
  lowest allowed Max Move Time (`MIN_MOVE_TIME`) are named constants at the top
  of `winding.py`.
- **Move splitting and feed rates:** `iter_program()` splits long moves (its
  inner `moves()` helper), so the preview, estimates and G-code all see the
  same pieces. `write_gcode()` computes each F from the written coordinates via
  `_rotation_limited_feed()`. The `MoveSplitting` and `MaxRotationSpeed` tests
  check both on the written file.
- **Homing position:** the move to the wind start assumes `G28` leaves the
  carriage at X0 Y0, with Y0 the eye's fully retracted end (farthest from the
  tank), as in the app's machine model. It is written by `_write_move_to_start()`
  in `winding.py`; adjust it there if the machine homes elsewhere.
- **Auto/custom fields:** `AutoEntry` in `plan_tab.py` gives *Number of Cycles*
  and *Start Wind at X* their shared auto/custom behavior. A new auto setting
  needs a flag in the model (see `winding.AUTO_FLAGS`) and one `AutoEntry`.
- **Feed rate and Klipper:** feed rates assume the controller applies F to the
  combined X/Y/A move length, with A in degrees, as the original generator did.
  If Klipper's `max_velocity` or `max_accel` are lower than what the file asks
  for, moves run slower than estimated: the wind takes longer than the
  app's *Time* estimate, and pieces can outlast Max Move Time. Keep the
  printer.cfg limits above the rates the app uses.
- **Layup colors:** the palettes in `theme.py` were checked for contrast
  against the mint tank and between every pair of colors, including for
  red-green color-blind viewers. From the 7th layup on, the colors repeat with
  a dashed line.
- The G-code viewer only reads `G0`/`G1`/`G92` commands and `;` comments.
  Other commands are skipped.
