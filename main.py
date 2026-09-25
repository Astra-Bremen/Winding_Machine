import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import dataclasses
import datetime
import os
import ttkbootstrap as tb
import strength
import theme
import winding
from plan_tab import PlanTab
from partial_page import PartialPage
from view_tab import ViewTab


def _make_var(default, value=None):
    # Picks the Tk variable type from the type of the setting's default value.
    value = default if value is None else value
    if isinstance(default, bool): return tk.BooleanVar(value=value)
    if isinstance(default, int): return tk.IntVar(value=value)
    if isinstance(default, float): return tk.DoubleVar(value=value)
    return tk.StringVar(value=value)


class CFRPWinderApp:
    # --- HARDCODED G-CODE MACROS ---
    # Homing (G28) is controlled by the "Home Axes Before Winding" checkbox, not by
    # this macro. Use START_GCODE for any additional manual startup G-code (e.g. a
    # pause, or turning on an indicator light) -- it always runs after homing/zeroing.
    START_GCODE = ""
    ONE_WIND_COMPLETE_GCODE = ""  # Execute after every L-R-L cycle
    END_GCODE = ""                # Execute at the very end

    def __init__(self, root):
        self.root = root
        self.root.title("CFRP Winder")
        self.root.geometry("1280x720")
        # Open maximized. "zoomed" is the Windows/most-Tk-builds way to do this;
        # fall back to the X11 equivalent attribute if that's not supported.
        try:
            self.root.state("zoomed")
        except tk.TclError:
            try: self.root.attributes("-zoomed", True)
            except tk.TclError: pass

        self.theme_mode = "light"
        style = theme.apply(self.root, self.theme_mode)
        theme.setup_compact_styles(style)
        self.canvas_palette = theme.PALETTES[self.theme_mode]
        self._icon_image = theme.build_icon(style.colors.primary)
        self.root.iconphoto(True, self._icon_image)

        # Settings are split in two: `params` holds everything shared by the
        # whole winding program (machine, tank, tow width, ...), while each entry
        # of `layups` holds one switchable layup's own settings (cycles, pattern
        # number, winding angle, dwell). Defaults come from winding.WindingJob,
        # the single source of truth for them.
        defaults = winding.WindingJob()
        self.params = {key: self._watched(_make_var(getattr(defaults, key))) for key in winding.GLOBAL_KEYS}
        # Layups made in the app start with their Number of Cycles on auto (full
        # coverage); only an explicit entry in the field makes it custom.
        self.layups = [self._make_layup_vars({**dataclasses.asdict(defaults.layups[0]), "auto_cycles": True})]
        self.active_layup = 0
        # The partial-export page's values (1-based layup/cycle, mandrel angle,
        # rehome): kept here so they survive leaving and reopening the page.
        self.partial_vars = {"layup": tk.IntVar(value=1), "cycle": tk.IntVar(value=1),
                             "angle": tk.DoubleVar(value=0.0), "rehome": tk.BooleanVar(value=False)}
        # The material/strength estimate's inputs (see strength.Material): not
        # winding settings -- they never change the G-code's motion, only the
        # estimates, so they don't trigger a re-simulation.
        material = strength.Material()
        self.material_vars = {key: tk.DoubleVar(value=getattr(material, key)) for key in strength.KEYS}
        for var in self.material_vars.values():
            var.trace_add("write", self._on_material_changed)
        self.strength_dialog = None

        self._build_menu()

        # Status bar first, packed to the bottom, so it claims a slim strip there
        # before the main content below claims everything else.
        status_bar = ttk.Frame(root, padding=(10, 3))
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Separator(root, orient=tk.HORIZONTAL).pack(side=tk.BOTTOM, fill=tk.X)
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(status_bar, textvariable=self.status_var).pack(side=tk.LEFT)

        # --- MAIN LAYOUT: settings on the left, split previews on the right ---
        main_frame = ttk.Frame(root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Left settings panel: wrapped in a canvas + scrollbar so all controls stay
        # reachable even when the window is too short to show them all at once.
        left_frame = ttk.Frame(main_frame)
        left_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))

        left_canvas = tk.Canvas(left_frame, borderwidth=0, highlightthickness=0, bg=style.colors.bg)
        left_scrollbar = ttk.Scrollbar(left_frame, orient=tk.VERTICAL, command=left_canvas.yview)
        left_canvas.configure(yscrollcommand=left_scrollbar.set)
        left_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        left_canvas.pack(side=tk.LEFT, fill=tk.Y, expand=True)
        self._left_canvas = left_canvas

        # The panel shows one page at a time: the settings, or the partial-export
        # page that temporarily takes their place (see show_partial_page).
        settings_inner = ttk.Frame(left_canvas)
        left_canvas_window = left_canvas.create_window((0, 0), window=settings_inner, anchor="nw")
        settings_inner.bind("<Configure>", lambda e: left_canvas.configure(scrollregion=left_canvas.bbox("all")))
        left_canvas.bind("<Configure>", lambda e: left_canvas.itemconfig(left_canvas_window, width=e.width))
        self._settings_page, self._left_window = settings_inner, left_canvas_window

        def _on_mousewheel(event):
            left_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        left_canvas.bind("<Enter>", lambda e: left_canvas.bind_all("<MouseWheel>", _on_mousewheel))
        left_canvas.bind("<Leave>", lambda e: left_canvas.unbind_all("<MouseWheel>"))

        right_paned = ttk.Panedwindow(main_frame, orient=tk.VERTICAL)
        right_paned.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        settings_preview_frame = ttk.LabelFrame(right_paned, text="Settings Preview", padding="10")
        gcode_preview_frame = ttk.LabelFrame(right_paned, text="G-Code Preview", padding="10")
        right_paned.add(settings_preview_frame, weight=1)
        right_paned.add(gcode_preview_frame, weight=1)

        self.plan_tab = PlanTab(settings_inner, settings_preview_frame, self)
        self.view_tab = ViewTab(gcode_preview_frame, self)
        self.partial_page = PartialPage(left_canvas, self)
        self.partial_page.frame.bind("<Configure>", lambda e: left_canvas.configure(scrollregion=left_canvas.bbox("all")),
                                     add="+")
        self._partial_shown = False

        root.update_idletasks()
        left_canvas.configure(width=settings_inner.winfo_reqwidth())

    def _build_menu(self):
        menubar = tk.Menu(self.root, tearoff=False)
        self.root.config(menu=menubar)

        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="Generate G-Code...", command=self.generate)
        file_menu.add_command(label="Open G-Code...", command=lambda: self.view_tab.open_gcode_view())
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.root.destroy)
        menubar.add_cascade(label="File", menu=file_menu)

        self.theme_mode_var = tk.StringVar(value=self.theme_mode)
        view_menu = tk.Menu(menubar, tearoff=False)
        view_menu.add_radiobutton(label="Light Mode", value="light", variable=self.theme_mode_var, command=lambda: self.set_theme_mode("light"))
        view_menu.add_radiobutton(label="Dark Mode", value="dark", variable=self.theme_mode_var, command=lambda: self.set_theme_mode("dark"))
        menubar.add_cascade(label="View", menu=view_menu)

        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

    def _show_about(self):
        messagebox.showinfo("About CFRP Winder",
                             "CFRP Winder\n\n"
                             "Klipper G-code generator and visualizer for composite\n"
                             "filament winding on a 3-axis (X, Y, A) winder.")

    def set_theme_mode(self, mode):
        if mode not in theme.PALETTES or mode == self.theme_mode:
            return
        self.theme_mode = mode
        style = theme.apply(self.root, mode)
        self.canvas_palette = theme.PALETTES[mode]
        if hasattr(self, "_left_canvas"): self._left_canvas.configure(bg=style.colors.bg)
        # Only the Settings Preview's custom-drawn canvases follow the app theme;
        # the G-Code Preview's viewport is intentionally a fixed dark surface in
        # both modes (see theme.py). ttkbootstrap retroactively recolors every
        # plain tk.Canvas on a theme switch though (not just ttk widgets), so the
        # viewport's fixed color has to be explicitly re-applied afterward or it
        # gets swept into the light/dark toggle too.
        if hasattr(self, "plan_tab"): self.plan_tab.on_theme_changed()
        if hasattr(self, "view_tab"): self.view_tab.reapply_viewport_colors()
        if hasattr(self, "partial_page"): self.partial_page.on_theme_changed()
        if self.strength_dialog is not None and self.strength_dialog.exists():
            self.strength_dialog.on_theme_changed()
            self.strength_dialog.refresh()

    def set_status(self, text):
        if hasattr(self, "status_var"): self.status_var.set(text)

    # --- Settings model ---

    def _watched(self, var):
        # Every setting change refreshes the Settings Preview (debounced there).
        var.trace_add("write", self._on_setting_changed)
        return var

    def _on_setting_changed(self, *args):
        if hasattr(self, "plan_tab"): self.plan_tab.schedule_redraw()
        if getattr(self, "_partial_shown", False): self.partial_page.refresh()

    def _make_layup_vars(self, values):
        # `values` maps every layup key to its initial value.
        defaults = winding.Layup()
        return {key: self._watched(_make_var(getattr(defaults, key), values[key])) for key in winding.LAYUP_KEYS}

    def _copy_layup_vars(self, source, **overrides):
        # Copies the fields' raw contents rather than their parsed values, so a
        # half-typed value is inherited as-is instead of failing the copy.
        return self._make_layup_vars({**{key: self.root.getvar(str(var)) for key, var in source.items()}, **overrides})

    def _layups_changed(self):
        if hasattr(self, "plan_tab"): self.plan_tab.on_layups_changed()
        if getattr(self, "_partial_shown", False): self.partial_page.refresh()

    # --- Material / strength estimate ---

    def build_material(self):
        """The estimate's inputs as a strength.Material. Raises SettingsError if
        a field doesn't hold a valid number right now."""
        values = {}
        for key, var in self.material_vars.items():
            try: values[key] = var.get()
            except tk.TclError: raise winding.SettingsError(key) from None
        return strength.Material(**values)

    def load_material(self, material):
        """Restores estimate inputs (e.g. the ones a reopened G-code file was
        made with)."""
        for key, var in self.material_vars.items():
            var.set(getattr(material, key))

    def _on_material_changed(self, *args):
        if hasattr(self, "plan_tab"): self.plan_tab.refresh_estimate()

    def show_strength_dialog(self):
        from strength_dialog import StrengthDialog
        if self.strength_dialog is None or not self.strength_dialog.exists():
            self.strength_dialog = StrengthDialog(self)
        self.strength_dialog.show()

    # --- Settings panel pages ---

    def show_partial_page(self):
        """Swap the settings for the partial-export page (see partial_page)."""
        if self._partial_shown:
            return
        self._partial_shown = True
        self._left_canvas.itemconfigure(self._left_window, window=self.partial_page.frame)
        self._left_canvas.yview_moveto(0)
        self.partial_page.on_show()

    def show_settings_page(self):
        if not self._partial_shown:
            return
        self._partial_shown = False
        self.partial_page.on_hide()
        self._left_canvas.itemconfigure(self._left_window, window=self._settings_page)
        self._left_canvas.yview_moveto(0)

    def load_partial(self, resume):
        """Restores a partial program's continue point (from a reopened file)
        and shows it on the partial-export page."""
        p = resume.point
        self.partial_vars["layup"].set(p.layup + 1)
        self.partial_vars["cycle"].set(p.cycle + 1)
        self.partial_vars["angle"].set(round(p.angle, 3))
        self.partial_vars["rehome"].set(resume.rehome)
        self.show_partial_page()

    def add_layup(self):
        # A new layup inherits every setting from the last one, so building up a
        # program usually only means changing the one or two values that differ
        # -- except its Number of Cycles, which always starts on auto.
        self.layups.append(self._copy_layup_vars(self.layups[-1], auto_cycles=True))
        self.active_layup = len(self.layups) - 1
        self._layups_changed()

    def remove_layup(self, index):
        if len(self.layups) <= 1 or not 0 <= index < len(self.layups):
            return
        # Keep a reference to the removed layup's variables until the editor has
        # been re-bound to a surviving layup: a Tk variable is unset the moment
        # its last Python reference goes away, and unsetting one that's still
        # bound to an entry field makes Tk silently re-create it.
        removed = self.layups.pop(index)
        if self.active_layup >= index and self.active_layup > 0:
            self.active_layup -= 1
        self._layups_changed()
        del removed

    def select_layup(self, index):
        if 0 <= index < len(self.layups) and index != self.active_layup:
            self.active_layup = index
            self._layups_changed()

    def load_layups(self, layups):
        """Replaces every layup (e.g. with the ones restored from a G-code file)."""
        if not layups:
            return
        old = self.layups  # kept alive until the editor is re-bound, see remove_layup
        self.layups = [self._make_layup_vars(dataclasses.asdict(layup)) for layup in layups]
        self.active_layup = 0
        self._layups_changed()
        del old

    def build_job(self):
        """An immutable snapshot of every current setting. Raises SettingsError
        if an entry field doesn't hold a valid number right now."""
        def read(variables, layup_index=None):
            values = {}
            for key, var in variables.items():
                try:
                    values[key] = var.get()
                except tk.TclError:
                    # An auto setting's field may be empty (e.g. just cleared to
                    # return it to auto); the WindingJob computes the value.
                    flag = winding.AUTO_FLAGS.get(key)
                    if flag and bool(variables[flag].get()): values[key] = 1
                    else: raise winding.SettingsError(key, layup_index) from None
            return values

        layups = tuple(winding.Layup(**read(layup_vars, index)) for index, layup_vars in enumerate(self.layups))
        return winding.WindingJob(**read(self.params), layups=layups)

    # --- G-code generation ---

    def _estimate_header(self, job):
        # The material estimate's inputs and results, recorded in the file.
        try:
            material = self.build_material()
        except winding.SettingsError:
            return []
        if strength.errors(material):
            return strength.header_items(material)
        result = self.plan_tab.simulation_for(job) or winding.simulate(job)
        return strength.header_items(material, strength.estimate(job, result, material))

    def generate(self, resume=None):
        """Writes the program to a file the user picks. With a winding.Resume,
        writes a partial program that continues an interrupted wind instead."""
        try:
            job = self.build_job()
        except winding.SettingsError as e:
            messagebox.showerror("Error", self.plan_tab.describe_settings_error(e))
            return
        errors = winding.validate(job)
        if errors:
            messagebox.showerror("Error", "\n\n".join(errors))
            return
        # Suggest a name like "17_09_14_32-RND-00_07_26.gcode": date/time
        # generated, end-cap type, and estimated wind duration. Windows
        # filenames can't contain ":", so the HH:MM:SS estimate is written
        # with underscores instead, matching the date's separator style. The
        # duration is taken from the Settings Preview's own live estimate
        # (already kept in sync with these same settings) rather than
        # recomputed here, so clicking Generate never blocks on a second full
        # simulation pass over a potentially long wind. A partial program is
        # marked "PARTIAL-L<layup>C<cycle>" and shows the time it has left.
        cap_code = "RND" if job.end_cap_type == "Round" else "FLT"
        seconds = self.plan_tab.estimated_total_time() or 0
        partial_code = ""
        if resume is not None:
            try:
                location = winding.locate(job, resume.point)
            except winding.PointError as e:
                messagebox.showerror("Error", f"Can't continue from there:\n{e}")
                return
            seconds = max(0.0, seconds - location.elapsed)
            partial_code = f"PARTIAL-L{resume.point.layup + 1}C{resume.point.cycle + 1}-"
        h, m = divmod(int(seconds), 3600); m, s = divmod(m, 60)
        suggested_name = (f"{datetime.datetime.now().strftime('%d_%m_%H_%M')}-{cap_code}-{partial_code}"
                          f"{h:02d}_{m:02d}_{s:02d}.gcode")
        filepath = filedialog.asksaveasfilename(defaultextension=".gcode", filetypes=[("G-Code Files", "*.gcode")], initialfile=suggested_name)
        if not filepath: return
        # Written to a temporary file next to the target and only moved into
        # place once complete: the machine must never be handed a truncated
        # program (e.g. one missing its END_GCODE) because writing failed halfway.
        tmp_path = filepath + ".part"
        try:
            with open(tmp_path, "w") as f:
                winding.write_gcode(f, job, self.START_GCODE, self.END_GCODE, resume=resume,
                                    extra_header=self._estimate_header(job))
            os.replace(tmp_path, filepath)
        except Exception as e:
            messagebox.showerror("Error", f"G-code generation failed:\n{e}")
            return
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        if resume is not None:
            p = resume.point
            self.set_status(f"Partial G-code generated (continues from Layup {p.layup + 1}, Cycle {p.cycle + 1}, "
                            f"A {p.angle:.1f}°): {filepath}")
        else:
            layup_note = f" ({len(job.layups)} layups, {job.total_cycles} cycles)" if len(job.layups) > 1 else ""
            self.set_status(f"G-code generated{layup_note}: {filepath}")
        kind = "Partial G-Code" if resume is not None else "G-Code"
        messagebox.showinfo("Success", f"{kind} generated!\nSaved to: {filepath}")
        self.view_tab.open_gcode_view(filepath)


if __name__ == "__main__":
    root = tb.Window(themename=theme.LIGHT_THEME)
    app = CFRPWinderApp(root)
    root.mainloop()
