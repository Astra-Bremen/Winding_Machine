"""The "Export Partial G-Code" page: continue an interrupted wind.

Shown in place of the settings panel. The user says where the wind stopped --
layup, cycle, and the mandrel angle A the machine shows -- sees the tank as it
should look at that point in the Settings Preview, and exports a partial
program that continues from exactly there (see winding.Resume). Its values
live in the app (app.partial_vars), so they're still there when the page is
opened again later in the session.
"""
import tkinter as tk
from tkinter import ttk
import winding
from plan_tab import FIELD_FONT, format_hms


class PartialPage:
    def __init__(self, parent, app):
        self.app = app
        self.vars = app.partial_vars
        self.frame = ttk.Frame(parent)
        self.point = None     # the winding.ProgramPoint entered, while valid
        self._location = None  # its winding.Location, once calculated
        self._timer = None
        self._hints = []      # muted helper texts, recolored with the theme
        self._build()
        for var in self.vars.values():
            var.trace_add("write", self._schedule_update)

    # --- Layout ---

    def _build(self):
        LBL, FRM, PAD = "Settings.TLabel", "Settings.TLabelframe", (8, 4)
        f = self.frame

        header = ttk.Frame(f)
        header.pack(fill=tk.X, pady=(0, 2))
        ttk.Button(header, text="‹", width=2, style="Nav.TButton", takefocus=False,
                   command=self.app.show_settings_page).pack(side=tk.LEFT)
        ttk.Label(header, text="Export Partial G-Code", style="PageTitle.TLabel").pack(side=tk.LEFT, padx=(8, 0))
        self._hint(f, "Continue an interrupted wind from where it stopped. Settings changed on the settings "
                      "page (‹) apply to the rest of the wind.").pack(fill=tk.X, pady=(0, 8))

        # Where the machine stopped.
        where = ttk.LabelFrame(f, text="Continue From", padding=PAD, style=FRM)
        where.pack(fill=tk.X, pady=(0, 6))
        where.columnconfigure(0, weight=1)
        ttk.Label(where, text="Layup", style=LBL).grid(row=0, column=0, sticky="w", pady=1)
        self.layup_combo = ttk.Combobox(where, state="readonly", width=21, font=FIELD_FONT, style="Settings.TCombobox")
        self.layup_combo.grid(row=0, column=1, sticky="e", pady=1)
        self.layup_combo.bind("<<ComboboxSelected>>", lambda e: self.vars["layup"].set(self.layup_combo.current() + 1))

        ttk.Label(where, text="Cycle", style=LBL).grid(row=1, column=0, sticky="w", pady=1)
        cycle_row = ttk.Frame(where)
        cycle_row.grid(row=1, column=1, sticky="e", pady=1)
        self.cycles_of = ttk.Label(cycle_row, text="of 1", style=LBL, width=7, anchor="w")
        self.cycles_of.pack(side=tk.RIGHT, padx=(6, 0))
        self.cycle_spin = ttk.Spinbox(cycle_row, from_=1, to=1, increment=1, width=5, font=FIELD_FONT,
                                      textvariable=self.vars["cycle"], style="Settings.TSpinbox")
        self.cycle_spin.pack(side=tk.RIGHT)
        # The wheel steps the cycle (instead of scrolling the panel).
        self.cycle_spin.bind("<MouseWheel>", lambda e: (self._step_cycle(1 if e.delta > 0 else -1), "break")[1])

        ttk.Label(where, text="Mandrel Angle A (°)", style=LBL).grid(row=2, column=0, sticky="w", pady=1)
        ttk.Entry(where, textvariable=self.vars["angle"], width=12, font=FIELD_FONT,
                  style="Settings.TEntry").grid(row=2, column=1, sticky="e", pady=1)
        self._hint(where, "The A the machine shows; it restarts at 0 with every cycle. "
                          "0 continues from the start of the cycle.").grid(row=3, column=0, columnspan=2, sticky="we",
                                                                             pady=(0, 4))
        self.from_viewer_btn = ttk.Button(where, text="Use G-Code Preview Position", style="primary.Outline.TButton",
                                          command=self._take_viewer_position)
        self.from_viewer_btn.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        ttk.Label(where, text="Eye Position", style=LBL).grid(row=5, column=0, sticky="w", pady=1)
        self.eye_value = ttk.Label(where, text="…", style=LBL)
        self.eye_value.grid(row=5, column=1, sticky="e", pady=1)

        # What's done and what's left at that point.
        state = ttk.LabelFrame(f, text="At This Point", padding=PAD, style=FRM)
        state.pack(fill=tk.X, pady=(0, 6))
        state.columnconfigure(0, weight=1)
        self.readouts = {}
        for row, (key, name) in enumerate((("wound", "Wound"), ("elapsed", "Elapsed"), ("remaining", "Remaining"),
                                           ("tow", "Remaining Tow"))):
            ttk.Label(state, text=name, style=LBL).grid(row=row, column=0, sticky="w", pady=1)
            self.readouts[key] = ttk.Label(state, text="…", style=LBL)
            self.readouts[key].grid(row=row, column=1, sticky="e", pady=1)

        options = ttk.LabelFrame(f, text="Options", padding=PAD, style=FRM)
        options.pack(fill=tk.X, pady=(0, 6))
        ttk.Checkbutton(options, text="Move to Starting Position", variable=self.vars["rehome"],
                        style="Settings.TCheckbutton").pack(anchor="w")
        self._hint(options, "Only for a severed fiber: homes X and Y (the mandrel stays put), moves the eye to "
                            "the continue point and pauses there to reattach the fiber. Leave it off while the "
                            "fiber is still attached and the eye is at the point.").pack(fill=tk.X, pady=(2, 0))

        self.message = ttk.Label(f, text="", style=LBL, justify="left")
        self.message.pack(fill=tk.X)
        self.export_btn = ttk.Button(f, text="Export Partial G-Code", style="primary.TButton", command=self._export)
        self.export_btn.pack(fill=tk.X, pady=(4, 2), ipady=4)
        # Wrap the texts to the panel's width, whatever it is.
        f.bind("<Configure>", lambda e: [w.configure(wraplength=max(120, e.width - 24))
                                         for w in self._hints + [self.message]])

    def _hint(self, parent, text):
        label = ttk.Label(parent, text=text, style="Settings.TLabel", justify="left")
        self._hints.append(label)
        return label

    # --- Showing ---

    def on_show(self):
        """The page was opened: refresh the layup list (the layups may have
        changed on the settings page) and show the point in the preview."""
        self.app.plan_tab.on_progress = self._on_progress
        self.on_theme_changed()
        self.from_viewer_btn.state(["!disabled"] if self.app.view_tab.gcode_commands else ["disabled"])
        self._update()

    def refresh(self):
        """Settings changed underneath the page (e.g. a G-code file was
        opened): re-check the point against them."""
        self._schedule_update()

    def on_hide(self):
        if self._timer:
            self.app.root.after_cancel(self._timer)
            self._timer = None
        self.app.plan_tab.on_progress = None
        self.app.plan_tab.set_partial_point(None)

    def on_theme_changed(self):
        # ttkbootstrap leaves a spinbox's field uncolored (plain white, even in
        # dark mode), so it takes the theme's entry-field colors explicitly.
        colors = self.app.root.style.colors
        self.app.root.style.configure("Settings.TSpinbox", fieldbackground=colors.inputbg, foreground=colors.inputfg,
                                      arrowcolor=colors.inputfg)
        muted = self.app.canvas_palette["muted"]
        for label in self._hints:
            label.configure(foreground=muted)
        self.message.configure(foreground=self.app.canvas_palette["warn_text"])

    # --- Input ---

    def _step_cycle(self, delta):
        try:
            value = int(self.vars["cycle"].get()) + delta
        except (tk.TclError, ValueError):
            value = 1
        self.vars["cycle"].set(max(1, min(int(self.cycle_spin.cget("to")), value)))

    def _take_viewer_position(self):
        point = self.app.view_tab.current_point()
        if point is None:
            self.app.set_status("Open the wound G-code in the G-Code Preview and move to where the machine stopped first.")
            return
        self.vars["layup"].set(point.layup + 1)
        self.vars["cycle"].set(point.cycle + 1)
        self.vars["angle"].set(round(point.angle, 3))
        self.app.set_status(f"Continue point taken from the G-Code Preview: Layup {point.layup + 1}, "
                            f"Cycle {point.cycle + 1}, A {point.angle:.1f}°.")

    def _schedule_update(self, *args):
        # Debounced like the preview, so typing an angle stays smooth.
        if self._timer:
            self.app.root.after_cancel(self._timer)
        self._timer = self.app.root.after(80, self._update)

    def _update(self):
        self._timer = None
        self.point = self._location = None
        self._set_readouts(None, None)
        self.export_btn.state(["disabled"])
        try:
            job = self.app.build_job()
        except winding.SettingsError as e:
            self._show_message(f"{self.app.plan_tab.describe_settings_error(e)} Fix it on the settings page (‹).")
            self.app.plan_tab.set_partial_point(None)
            return
        self.layup_combo.configure(values=[f"Layup {i + 1} · {l.wind_angle:g}° · {l.passes} cycles"
                                           for i, l in enumerate(job.layups)])
        layup = max(1, min(len(job.layups), self._int(self.vars["layup"], 1)))
        passes = job.layups[layup - 1].passes
        self.layup_combo.current(layup - 1)
        self.cycle_spin.configure(to=passes)
        self.cycles_of.configure(text=f"of {passes}")
        cycle, angle = self._int(self.vars["cycle"], None), self._float(self.vars["angle"])
        if cycle is None or not 1 <= cycle <= passes:
            self._show_message(f"Enter a cycle from 1 to {passes}.")
            return
        if angle is None or angle < 0:
            self._show_message("Enter the mandrel angle A as shown on the machine (0 or more).")
            return
        self._show_message("")
        self.point = winding.ProgramPoint(layup - 1, cycle - 1, angle)
        # The Settings Preview and its estimates follow the chosen layup.
        self.app.select_layup(layup - 1)
        self.app.plan_tab.set_partial_point(self.point)
        self.app.plan_tab.report_progress()

    @staticmethod
    def _int(var, default):
        try:
            return int(var.get())
        except (tk.TclError, ValueError):
            return default

    @staticmethod
    def _float(var):
        try:
            return float(var.get())
        except (tk.TclError, ValueError):
            return None

    # --- Results from the Settings Preview ---

    def _on_progress(self, location, total, error):
        # The preview calculated where the entered point is (or why it isn't):
        # fill in the eye position and progress, and allow exporting.
        if error is not None:
            self._location = None
            self._set_readouts(None, None)
            self.export_btn.state(["disabled"])
            self._show_message(str(error))
            return
        self._location = location
        self._set_readouts(location, total)
        if location is None:
            return
        errors = winding.validate(self.app.plan_tab.job) if self.app.plan_tab.job else []
        self._show_message(errors[0] if errors else "")
        self.export_btn.state(["disabled"] if errors else ["!disabled"])

    def _set_readouts(self, location, total):
        if location is None:
            self.eye_value.configure(text="…")
            for label in self.readouts.values():
                label.configure(text="…")
            return
        self.eye_value.configure(text=f"X {location.x:.1f} · Y {location.y:.1f} mm")
        self.readouts["elapsed"].configure(text=format_hms(location.elapsed))
        if total is None:
            for key in ("wound", "remaining", "tow"):
                self.readouts[key].configure(text="…")
            return
        self.readouts["wound"].configure(text=f"{100 * location.elapsed / max(total.total_time, 1e-9):.0f} %")
        self.readouts["remaining"].configure(text=format_hms(total.total_time - location.elapsed))
        self.readouts["tow"].configure(text=f"{max(0.0, total.total_tow - location.tow) / 1000:.2f} m")

    def _show_message(self, text):
        self.message.configure(text=text)

    def _export(self):
        if self.point is None:
            return
        self.app.generate(resume=winding.Resume(self.point, bool(self.vars["rehome"].get())))
