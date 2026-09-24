# Winding_Machine

**CFRP Winder** is a desktop tool that generates and previews G-code for a
3-axis composite filament winding machine running Klipper. You enter the
machine, tank and winding parameters. The app shows a live preview of the
winding pattern with time and material estimates, writes a `.gcode` file, and
can play that file back as a simulation.

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
  end-on view showing the eye reach, and live estimates: total time, time per
  cycle, required tow length, rotation and X speeds (click to change units),
  cycles needed for full coverage and extra turnaround rotation. The
  simulation runs on a background thread, so the interface stays responsive.
- **G-Code Preview:** load any generated file and play or scrub through it.
  You can jump to a line or cycle and see the coordinates and elapsed time.
  There is an optional 3D mode that spins with the mandrel. Loading a file
  also restores its settings into the settings panel.
- Light and dark themes (View menu).

## Generated G-code

```gcode
; --- WINDER SETTINGS ---
; chuck_offset: 50.0          <- every parameter is saved as a comment, so a
; ...                             file can be reopened and its settings restored
; ld: 96.825                  <- computed dome length
; -----------------------
G28                           <- or "G92 A0" if "Home Axes Before Winding" is off
G1 X50.000 Y... A0.000 F...   <- move to the start position
G1 X55.000 Y... A... F...     <- traversal in 5 mm X steps
...
G1 X... Y... A... F...        <- dwell at the turnaround (A only)
; CYCLE_COMPLETE:1
G92 A0                        <- reset A after each cycle so the value never grows too large
...
```

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
2. Check the **Estimates** panel. *Cycles for Full Coverage* shows how many
   cycles are needed to cover the tank completely.
3. Click **Generate G-Code** and choose where to save the file. The file
   opens in the G-Code Preview automatically.
4. Use **Open G-Code** to inspect a file generated earlier.

## Project structure

| File | Purpose |
|------|---------|
| `main.py` | App entry point and `CFRPWinderApp`: parameters, window layout and menus, all winding math (geometry, dwell/pattern alignment, trajectory blending), the preview simulation (`simulate_winding`) and G-code output (`generate`). |
| `plan_tab.py` | `PlanTab`: settings form, live Settings Preview (3D tank, end view, estimates), wind-angle validation and the background calculation thread. |
| `view_tab.py` | `ViewTab`: G-code parser and playback viewer. |
| `theme.py` | ttkbootstrap theme setup, canvas color palettes and the generated app icon. |

## Notes for development

- **Custom G-code macros:** `START_GCODE` and `END_GCODE` at the top of
  `CFRPWinderApp` in `main.py` are inserted into every file.
  `ONE_WIND_COMPLETE_GCODE` is defined but **not yet used** anywhere.
- **The winding loop exists twice:** `simulate_winding()` (preview and
  estimates) and `generate()` (file output) run the same circuit/dwell loop
  separately. A change to the motion must be made in both places, or the
  preview will no longer match the G-code.
- **Hard-coded machine constants:** the 550 mm Y reference, the 0–180 mm Y
  range and the 5 mm traversal step are written out directly in `main.py`,
  `plan_tab.py` and `view_tab.py`. Only `MAX_X` is a named constant.
- The G-code viewer only reads `G0`/`G1`/`G92` commands and `;` comments.
  Other commands are skipped.
