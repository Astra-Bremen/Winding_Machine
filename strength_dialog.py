"""The Strength & Materials window: the material/strength estimate's inputs
next to their live results (see strength.py for the method)."""
import tkinter as tk
from tkinter import ttk
import ttkbootstrap as tb
from ttkbootstrap.widgets.tooltip import ToolTip
import strength
from plan_tab import FIELD_FONT

# (key, label, unit, explanation) for every input, as they appear in the window.
MATERIAL_FIELDS = [
    ("tow_density", "Tow Linear Density", "g/m",
     "Mass of one meter of the dry tow (fiber only). Excel sheet: \"fiber mass per meter\"."),
    ("fiber_fraction", "Fiber Mass Fraction", "0–1",
     "Fiber share of the cured composite's mass; the rest is resin. 0.55 means 55 % fiber, 45 % resin. "
     "Excel sheet: \"fiber to resin ratio\"."),
    ("composite_density", "Composite Density", "kg/m³",
     "Density of the cured fiber/resin composite; turns the wound mass into a wall thickness. "
     "Excel sheet: \"Material density\"."),
    ("fiber_strength", "Fiber Tensile Strength", "MPa",
     "Ultimate tensile strength of the fiber, from its datasheet. Excel sheet: \"Fiber Ultimate Tensile Strength\"."),
    ("translation", "Strength Translation", "0–1",
     "Share of the fiber's strength the wound laminate actually realizes. The Excel sheet's 0.85."),
    ("laminate_factor", "Laminate Factor", "0–1",
     "Share of that strength the winding carries around the tank (hoop direction). The Excel sheet's 0.3, "
     "assuming a 54° equivalent winding."),
]
LOAD_FIELDS = [
    ("pressure", "Operating Pressure", "bar", "The tank's working pressure. Excel sheet: \"Operational Pressure\"."),
]
LABELS = {key: label for key, label, _, _ in MATERIAL_FIELDS + LOAD_FIELDS}


class StrengthDialog:
    def __init__(self, app):
        self.app = app
        self.win = tb.Toplevel(title="Strength & Materials", resizable=(False, False), iconphoto=None)
        self.win.transient(app.root)
        self.win.protocol("WM_DELETE_WINDOW", self.win.destroy)
        self.win.bind("<Escape>", lambda e: self.win.destroy())
        self._muted = []  # labels drawn in the muted text color
        self._build()
        self.refresh()

    def exists(self):
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def show(self):
        # Bottom-right of the main window, over the G-Code Preview: the tank
        # and the Estimates it relates to stay in view while it's open.
        self.win.deiconify()
        self.win.update_idletasks()
        root = self.app.root
        x = root.winfo_rootx() + root.winfo_width() - self.win.winfo_reqwidth() - 30
        y = root.winfo_rooty() + root.winfo_height() - self.win.winfo_reqheight() - 40
        self.win.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.win.lift()
        self.win.focus_set()

    # --- Layout ---

    def _build(self):
        f = ttk.Frame(self.win, padding=16)
        f.pack(fill=tk.BOTH, expand=True)
        ttk.Label(f, text="Strength & Materials", style="PageTitle.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        self._label(f, "A rough estimate after the original Excel sheet — not a substitute for a design "
                       "analysis or a burst test. Hover over an input for what it means.",
                    muted=True, wraplength=620).grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 12))

        inputs = ttk.Frame(f)
        inputs.grid(row=2, column=0, sticky="n")
        material = ttk.LabelFrame(inputs, text="Material", padding=(10, 6), style="Settings.TLabelframe")
        material.pack(fill=tk.X)
        for key, label, unit, tip in MATERIAL_FIELDS:
            self._input_row(material, key, label, unit, tip)
        load = ttk.LabelFrame(inputs, text="Load", padding=(10, 6), style="Settings.TLabelframe")
        load.pack(fill=tk.X, pady=(8, 0))
        for key, label, unit, tip in LOAD_FIELDS:
            self._input_row(load, key, label, unit, tip)
        ttk.Button(inputs, text="Reset to Excel Values", style="primary.Outline.TButton",
                   command=self._reset).pack(fill=tk.X, pady=(10, 0))

        ttk.Separator(f, orient=tk.VERTICAL).grid(row=2, column=1, sticky="ns", padx=18)

        out = ttk.Frame(f)
        out.grid(row=2, column=2, sticky="n")
        results = ttk.LabelFrame(out, text="Estimate for the Current Wind", padding=(10, 6), style="Settings.TLabelframe")
        results.pack(fill=tk.X)
        results.columnconfigure(1, weight=1)
        self.values = {}
        rows = [("fiber", "Fiber Mass"), ("resin", "Resin Mass"), ("total", "Total Mass"), None,
                ("thickness", "Wall Thickness (straight section)"), ("hoop", "Hoop Stress at Operating Pressure"),
                ("allowable", "Allowable Stress"), None]
        for row, item in enumerate(rows):
            if item is None:
                ttk.Separator(results).grid(row=row, column=0, columnspan=2, sticky="ew", pady=6)
                continue
            key, name = item
            ttk.Label(results, text=name).grid(row=row, column=0, sticky="w", pady=1, padx=(0, 16))
            self.values[key] = ttk.Label(results, text="…", anchor="e")
            self.values[key].grid(row=row, column=1, sticky="e", pady=1)
        row = len(rows)
        ttk.Label(results, text="Safety Factor", font=("TkHeadingFont", 10, "bold")).grid(row=row, column=0, sticky="w")
        self.values["sf"] = ttk.Label(results, text="…", font=("TkHeadingFont", 16, "bold"), anchor="e")
        self.values["sf"].grid(row=row, column=1, sticky="e")
        ttk.Label(results, text="Estimated Burst Pressure").grid(row=row + 1, column=0, sticky="w", pady=1)
        self.values["burst"] = ttk.Label(results, text="…", anchor="e")
        self.values["burst"].grid(row=row + 1, column=1, sticky="e", pady=1)
        self.sheet_line = self._label(out, "", muted=True, wraplength=330)
        self.sheet_line.pack(fill=tk.X, pady=(8, 0))

        self.message = ttk.Label(f, text="", wraplength=620)
        self.message.grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self._label(f, "Wall thickness = mass wound on the straight section ÷ (its surface × composite density)  ·  "
                       "Hoop stress = pressure × radius ÷ thickness  ·  Allowable stress = fiber strength × fiber "
                       "fraction × strength translation × laminate factor  ·  Safety factor = allowable ÷ hoop "
                       "stress  ·  Burst pressure = operating pressure × safety factor.",
                    muted=True, wraplength=620).grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Button(f, text="Close", style="primary.TButton", command=self.win.destroy).grid(
            row=5, column=2, sticky="e", pady=(14, 0), ipadx=18)
        self.on_theme_changed()

    def _input_row(self, parent, key, label, unit, tip):
        row = parent.grid_size()[1]
        name = ttk.Label(parent, text=label, style="Settings.TLabel", cursor="question_arrow")
        name.grid(row=row, column=0, sticky="w", pady=1, padx=(0, 12))
        entry = ttk.Entry(parent, textvariable=self.app.material_vars[key], width=9, font=FIELD_FONT,
                          style="Settings.TEntry", justify="right")
        entry.grid(row=row, column=1, sticky="e", pady=1)
        self._label(parent, unit, muted=True, width=6).grid(row=row, column=2, sticky="w", padx=(6, 0))
        for widget in (name, entry):
            ToolTip(widget, text=tip, wraplength=280, delay=400)

    def _label(self, parent, text, muted=False, **options):
        label = ttk.Label(parent, text=text, style="Settings.TLabel", justify="left", **options)
        if muted:
            self._muted.append(label)
        return label

    def on_theme_changed(self):
        for label in self._muted:
            label.configure(foreground=self.app.canvas_palette["muted"])
        self.message.configure(foreground=self.app.canvas_palette["warn_text"])

    # --- Behavior ---

    def _reset(self):
        self.app.load_material(strength.Material())
        self.app.set_status("Strength & Materials inputs reset to the Excel sheet's values.")

    def refresh(self):
        """Show the estimate the Settings Preview last worked out (it updates
        this whenever the wind or an input changes)."""
        if not self.exists():
            return
        try:
            material = self.app.build_material()
        except Exception as e:
            self._blank(f"'{LABELS.get(getattr(e, 'key', ''), 'A value')}' must be a number.")
            return
        problems = strength.errors(material)
        if problems:
            self._blank(problems[0])
            return
        estimate = self.app.plan_tab.estimate
        if estimate is None or estimate[1] != material:
            self._blank("Waiting for the wind's simulation…" if self.app.plan_tab.calculating()
                        else "The current settings can't be wound — see the Settings Preview.")
            return
        e = estimate[2]
        self.message.configure(text="")
        v = self.values
        v["fiber"].configure(text=f"{e.fiber_mass:.3f} kg")
        v["resin"].configure(text=f"{e.resin_mass:.3f} kg")
        v["total"].configure(text=f"{e.total_mass:.3f} kg")
        if e.safety_factor is None:
            for key in ("thickness", "hoop", "sf", "burst"):
                v[key].configure(text="--")
            v["allowable"].configure(text=f"{e.allowable_stress:.0f} MPa")
            self.message.configure(text="No fiber lies on a straight section, so there's no wall to rate.")
        else:
            v["thickness"].configure(text=f"{e.thickness:.2f} mm")
            v["hoop"].configure(text=f"{e.hoop_stress:.0f} MPa")
            v["allowable"].configure(text=f"{e.allowable_stress:.0f} MPa")
            v["sf"].configure(text=f"{e.safety_factor:.2f}",
                              foreground=self.app.canvas_palette["warn_text"] if e.safety_factor < 1 else "")
            v["burst"].configure(text=f"{e.burst_pressure:.0f} bar")
            if e.safety_factor < 1:
                self.message.configure(text="Below 1: the wall would fail at the operating pressure.")
        if e.sheet_safety_factor is not None:
            self.sheet_line.configure(
                text=f"Excel sheet method: {e.sheet_thickness:.2f} mm, safety factor {e.sheet_safety_factor:.2f}. "
                     "It spreads the whole tank's mass over the straight section, dome and turnaround material "
                     "included, so it reads higher.")
        else:
            self.sheet_line.configure(text="")

    def _blank(self, message):
        for label in self.values.values():
            label.configure(text="…", foreground="")
        self.sheet_line.configure(text="")
        self.message.configure(text=message)
