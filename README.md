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
  spaced circuits around the circumference. After each cycle, the pattern
  moves on by one effective band width, so repeated cycles cover the surface.
- **Multi-layup programs:** a wind is a sequence of layups, wound one after the
  other on the same tank. Each layup has its own number of cycles, pattern
  number, winding angle and turnaround dwell angle, so one program can combine,
  for example, a 45° helical layup with a near-hoop layup. The tow width, the
  Optimize Trajectory setting and all machine and tank settings apply to the
  whole program. Each layup keeps its own pattern alignment.
- **Pattern-correct turnarounds:** each turnaround has a minimum dwell angle.
  The app adds extra rotation so that every circuit starts exactly where the
  pattern needs it. This extra rotation is split evenly between the two tank
  ends.
- **Eye clearance:** the Y position follows the tank profile plus a safety
  gap. It uses the largest radius under the full width of the eye, so the eye
  cannot hit the tank at the domes.
- **Surface-speed control:** the speed limit is a surface speed in mm/s,
  converted to an A-axis rate for the current diameter. Every G-code feed rate
  is chosen so that the A axis turns at exactly this rate.
- **Wind-angle validation:** angles that the tank size and band width cannot
  produce are corrected automatically. At angles that are too steep, each wrap
  would lie on top of the previous one. At angles that are too shallow, the
  strand barely moves around the tank.
- **Optimize Trajectory (experimental):** moves part of each dwell rotation
  into the X steps just before and after it. The motion planner then does not
  have to slow almost to a stop at the turnaround, and the winding pattern
  stays exactly the same.
- **Live Settings Preview:** a pseudo-3D tank view that you can rotate, an
  end-on view showing the eye reach, and live estimates. The estimates come in
  two groups:
  - **Program:** total time, required tow, total cycles, rotation speed and eye
    reach for the whole wind.
  - **Selected layup:** its time, time per cycle, required tow, X speed, cycles
    needed for full coverage and extra turnaround rotation.

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
; optimize_trajectory: False
; layups: 2
; layup_1: passes=10 pattern_number=3 wind_angle=45.0 turnaround_angle=270.0
; layup_2: passes=4 pattern_number=5 wind_angle=75.0 turnaround_angle=270.0
; ld: 96.825                  <- computed dome length
; -----------------------
G28                           <- or "G92 A0" if "Home Axes Before Winding" is off
G1 X50.000 Y... A0.000 F...   <- move to the start position
; LAYUP_START:1
G1 X55.000 Y... A... F...     <- traversal in 5 mm X steps
...
G1 X... Y... A... F...        <- dwell at the turnaround (A only)
; CYCLE_COMPLETE:1            <- cycles are numbered across the whole program
G92 A0                        <- reset A after each cycle so the value never grows too large
...
; LAYUP_START:2
...
```

A single-layup program produces exactly the same moves as the app did before
layups existed. `test_winding.py` checks this against the original output.

The suggested file name records the time of generation, the end-cap type and
the estimated duration, for example `17_09_14_32-RND-00_07_26.gcode`.

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
   last layup, so you usually only change what differs. Use **‹ ›** to switch
   between layups and **−** to remove the selected one. The colored swatch
   next to the title matches that layup's color in the previews.
3. Check the estimates. *Cycles for Full Coverage* shows how many cycles the
   selected layup needs to cover the tank completely. Check **Show All
   Layups** to see every layup on the tank at once.
4. Click **Generate G-Code** and choose where to save the file. The file
   opens in the G-Code Preview automatically.
5. Use **Open G-Code** to inspect a file generated earlier.

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
  0–180 mm Y range (`Y_TRAVEL`) and the 5 mm traversal step (`STEP_SIZE`) are
  named constants at the top of `winding.py`.
- **Layup colors:** the palettes in `theme.py` were checked for contrast
  against the mint tank and between every pair of colors, including for
  red-green color-blind viewers. From the 7th layup on, the colors repeat with
  a dashed line.
- The G-code viewer only reads `G0`/`G1`/`G92` commands and `;` comments.
  Other commands are skipped.
