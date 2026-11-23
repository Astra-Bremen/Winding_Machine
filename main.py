import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import dataclasses
import datetime
import os
import ttkbootstrap as tb
import machine_link
import strength
import theme
import winding
from plan_tab import PlanTab
from partial_page import PartialPage
from progress_dialog import Cancelled, ProgressTask
from view_tab import ViewTab, read_gcode

# Of a "Generate G-Code" run's progress bar: the share for writing the file;
# the rest is for loading it into the G-Code Preview, which takes about as long.
WRITE_SHARE = 0.5


def _estimate_header(job, material, result):
    # The material estimate's inputs (a strength.Material, or None while they
    # don't hold numbers) and its results for `job`, recorded in the file.
    if material is None:
        return []
    if strength.errors(material):
        return strength.header_items(material)
    return strength.header_items(material, strength.estimate(job, result, material))


def _is_set(var):
    try:
        return bool(var.get())
    except tk.TclError:
        return False


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
        # The window icon (every window's): the ASTRA mark, in a few sizes.
        self._icon_images = theme.apply_icon(self.root, style.colors.primary)

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
        # The Control tab's tank calibration: (start, end), machine X of the
        # straight section as measured, while Chuck X-Offset and Straight
        # Length still hold what it worked out (see apply_calibration).
        self.calibration = None
        self._calibrated_values = {}
        self._writing_calibration = False
        theme.style_calibrated(style, self.theme_mode)

        # Status bar first, packed to the bottom, so it claims a slim strip there
        # before the main content below claims everything else.
        status_bar = ttk.Frame(root, padding=(10, 3))
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Separator(root, orient=tk.HORIZONTAL).pack(side=tk.BOTTOM, fill=tk.X)
        self.status_var = tk.StringVar(value="Ready")
        # The ASTRA logo in the window's bottom-right corner, in the club's
        # purple (lighter on the dark theme; see _draw_logo).
        self._logo_label = ttk.Label(status_bar)
        self._logo_label.pack(side=tk.RIGHT)
        self._logo_image = None
        self._draw_logo()
        ttk.Label(status_bar, textvariable=self.status_var).pack(side=tk.LEFT)

        # The two tabs, Plan and Control, in a strip along the top; each is a
        # page of its own, and only the selected one is shown.
        self._build_tab_bar()
        self.plan_page = ttk.Frame(root)
        self.control_page = ttk.Frame(root, padding="10")
        self.plan_page.pack(fill=tk.BOTH, expand=True)
        self.current_tab = "plan"

        # --- PLAN: settings on the left, split previews on the right ---
        main_frame = ttk.Frame(self.plan_page, padding="10")
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
        self._left_scrollbar = left_scrollbar
        # The scrollbar only shows while the page doesn't fit (a small window).
        settings_inner.bind("<Configure>", lambda e: self._fit_left_scrollbar(), add="+")
        left_canvas.bind("<Configure>", lambda e: self._fit_left_scrollbar(), add="+")
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
        self.partial_page.frame.bind("<Configure>", lambda e: self._fit_left_scrollbar(), add="+")
        self._partial_shown = False

        # --- CONTROL: the machine, live (see control_tab) ---
        from control_tab import ControlTab
        self.control_tab = ControlTab(self.control_page, self)

        # A calibrated Chuck X-Offset / Straight Length stays marked until
        # it's changed by anything but the calibration (typing, opening a
        # file); a new tank diameter or end cap moves the tank's start
        # against the measured straight section, so the offset follows.
        for key in ("chuck_offset", "straight_length"):
            self.params[key].trace_add("write", self._on_calibrated_edited)
        for key in ("tank_diameter", "end_cap_diameter", "end_cap_type"):
            self.params[key].trace_add("write", self._on_dome_changed)

        root.bind("<Control-Key-1>", lambda e: self.show_tab("plan"))
        root.bind("<Control-Key-2>", lambda e: self.show_tab("control"))
        root.update_idletasks()
        left_canvas.configure(width=settings_inner.winfo_reqwidth())

    def _fit_left_scrollbar(self):
        if not hasattr(self, "partial_page"):
            return
        page = self.partial_page.frame if self._partial_shown else self._settings_page
        needed = page.winfo_reqheight() > self._left_canvas.winfo_height() > 1
        shown = bool(self._left_scrollbar.winfo_manager())
        if needed and not shown:
            self._left_scrollbar.pack(side=tk.RIGHT, fill=tk.Y, before=self._left_canvas)
        elif not needed and shown:
            self._left_scrollbar.pack_forget()
            self._left_canvas.yview_moveto(0)

    # --- Tabs ---

    TABS = (("plan", "Plan", "Plan the wind: settings, previews, G-code (Ctrl+1)."),
            ("control", "Control", "The machine, live: connection, jogging, calibration and the wind running "
                                   "on it (Ctrl+2)."))

    def _build_tab_bar(self):
        # Plain Tk labels with directly set colors (restyled with the theme in
        # _style_tab_bar): the selected tab in the theme's mint, underlined.
        from ttkbootstrap.widgets.tooltip import ToolTip
        bar = tk.Frame(self.root)
        bar.pack(side=tk.TOP, fill=tk.X)
        ttk.Separator(self.root, orient=tk.HORIZONTAL).pack(side=tk.TOP, fill=tk.X)
        self._tab_bar, self._tabs = bar, {}
        for i, (key, text, tip) in enumerate(self.TABS):
            cell = tk.Frame(bar)
            cell.pack(side=tk.LEFT, padx=(8 if i == 0 else 0, 0))
            label = tk.Label(cell, text=text, font=("TkDefaultFont", 10, "bold"), padx=14, pady=3, cursor="hand2")
            label.grid(row=0, column=0)
            badge = tk.Label(cell, text="", font=("TkDefaultFont", 8, "bold"), padx=0, pady=0, cursor="hand2")
            badge.grid(row=0, column=1, padx=(0, 10))
            badge.grid_remove()
            line = tk.Frame(cell, height=3)
            line.grid(row=1, column=0, columnspan=2, sticky="ew")
            for widget in (label, badge):
                widget.bind("<Button-1>", lambda e, k=key: self.show_tab(k))
                widget.bind("<Enter>", lambda e, k=key: self._style_tab_bar(hover=k))
                widget.bind("<Leave>", lambda e: self._style_tab_bar())
            ToolTip(label, text=tip, wraplength=300, delay=400)
            self._tabs[key] = (cell, label, badge, line)
        self._badge_color = None
        self._theme_btn = tk.Label(bar, font=("TkDefaultFont", 9), padx=10, pady=5, cursor="hand2")
        self._theme_btn.pack(side=tk.RIGHT, padx=(0, 8))
        self._theme_btn.bind("<Button-1>", lambda e: self.set_theme_mode("dark" if self.theme_mode == "light" else "light"))
        ToolTip(self._theme_btn, text="Switches between the light and the dark look.", wraplength=300, delay=400)
        about = tk.Label(bar, text="About", font=("TkDefaultFont", 9), padx=10, pady=3, cursor="hand2")
        about.pack(side=tk.RIGHT)
        about.bind("<Button-1>", lambda e: self._show_about())
        self._bar_links = (self._theme_btn, about)
        self._style_tab_bar()

    def _style_tab_bar(self, hover=None):
        colors = self.root.style.colors
        self._tab_bar.configure(bg=colors.bg)
        for key, (cell, label, badge, line) in self._tabs.items():
            selected = key == getattr(self, "current_tab", "plan")
            fg = colors.primary if selected else (colors.fg if key == hover else self.canvas_palette["muted"])
            for widget in (cell, label, badge):
                widget.configure(bg=colors.bg)
            label.configure(fg=fg)
            badge.configure(fg=self._badge_color or fg)
            line.configure(bg=colors.primary if selected else colors.bg)
        self._theme_btn.configure(text="☾  Dark" if self.theme_mode == "light" else "☀  Light")
        for link in self._bar_links:
            link.configure(bg=colors.bg, fg=self.canvas_palette["muted"])

    def show_tab(self, key):
        if key == self.current_tab:
            return
        self.current_tab = key
        if key == "control":
            self.plan_page.pack_forget()
            self.control_page.pack(fill=tk.BOTH, expand=True)
            self.control_tab.on_show()
        else:
            self.control_tab.on_hide()
            self.control_page.pack_forget()
            self.plan_page.pack(fill=tk.BOTH, expand=True)
        self._style_tab_bar()

    def set_control_badge(self, color, text):
        """The machine's state next to the Control tab's name: a dot in
        `color` (none while offline) and a few words."""
        _, _, badge, _ = self._tabs["control"]
        self._badge_color = color
        if color is None:
            badge.grid_remove()
        else:
            badge.configure(text=f"●  {text}" if text else "●", fg=color)
            badge.grid()

    def _show_about(self):
        messagebox.showinfo("About CFRP Winder",
                             "CFRP Winder\n\n"
                             "Klipper G-code generator, visualizer and machine control\n"
                             "for composite filament winding on a 3-axis (X, Y, A) winder.")

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
        theme.style_calibrated(style, mode)
        if hasattr(self, "control_tab"): self.control_tab.on_theme_changed()
        if hasattr(self, "_tab_bar"): self._style_tab_bar()
        self._draw_logo()

    def _draw_logo(self):
        # The status bar's ASTRA logo, in the purple that reads on this theme
        # (nothing, if the logo's file is missing).
        from PIL import ImageTk
        image = theme.logo_lockup(18, theme.ASTRA_PURPLE[self.theme_mode])
        self._logo_image = ImageTk.PhotoImage(image) if image is not None else None
        self._logo_label.configure(image=self._logo_image or "")

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

    # --- Tank calibration (from the Control tab) ---

    def apply_calibration(self, start, end):
        """Enters the Chuck X-Offset and Straight Length that put the straight
        section from machine X `start` to `end`, marked as calibrated."""
        self.calibration = (start, end)
        self._write_calibration()
        self.plan_tab.set_calibrated(True)

    def _write_calibration(self):
        try:
            # Only the tank's shape matters here (its domes): not the whole job,
            # so a half-typed layup field doesn't hold it up.
            tank = winding.WindingJob(**{k: self.params[k].get() for k in ("end_cap_type", "tank_diameter",
                                                                               "end_cap_diameter")})
            values = machine_link.calibrated_settings(tank, *self.calibration)
        except (tk.TclError, ValueError, TypeError):
            return
        self._calibrated_values = {key: round(value, 2) for key, value in values.items()}
        self._writing_calibration = True
        try:
            for key, value in self._calibrated_values.items():
                self.params[key].set(value)
        finally:
            self._writing_calibration = False

    def _on_calibrated_edited(self, *args):
        # Only a different value ends it: generating reloads the file just
        # written into the G-Code Preview, which sets the same values again.
        if self.calibration is None or self._writing_calibration:
            return
        for key, value in self._calibrated_values.items():
            try:
                same = abs(float(self.params[key].get()) - value) < 1e-6
            except (tk.TclError, ValueError):
                same = False
            if not same:
                self.calibration = None
                self.plan_tab.set_calibrated(False)
                return

    def _on_dome_changed(self, *args):
        if self.calibration is not None and not self._writing_calibration:
            self._write_calibration()

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
        # -- except its Number of Cycles and Bulkhead Angle, which always start
        # on auto.
        self.layups.append(self._copy_layup_vars(self.layups[-1], auto_cycles=True, bulkhead_auto=True))
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
            # A 90° wind doesn't use its helical settings, so a half-typed one
            # doesn't hold it up either.
            ignored = winding.HOOP_IGNORED if layup_index is not None and _is_set(variables["hoop"]) else ()
            for key, var in variables.items():
                try:
                    values[key] = var.get()
                except tk.TclError:
                    # An auto setting's field may be empty (e.g. just cleared to
                    # return it to auto); the WindingJob computes the value.
                    flag = winding.AUTO_FLAGS.get(key)
                    if flag and bool(variables[flag].get()): values[key] = 1
                    elif key in ignored: values[key] = getattr(winding.Layup(), key)
                    else: raise winding.SettingsError(key, layup_index) from None
            return values

        layups = tuple(winding.Layup(**read(layup_vars, index)) for index, layup_vars in enumerate(self.layups))
        return winding.WindingJob(**read(self.params), layups=layups)

    # --- G-code generation ---


    def send_and_wind(self):
        """Generates the program like Generate does, then sends it to the
        machine connected on the Control tab and (once confirmed) starts it."""
        blocker = self.control_tab.send_blocker()
        if blocker:
            messagebox.showwarning("Send & Wind", blocker)
            if self.control_tab.status() is None:
                self.show_tab("control")
            return
        self.generate(send=True)

    def generate(self, resume=None, send=False):
        """Writes the program to a file the user picks. With a winding.Resume,
        writes a partial program that continues an interrupted wind instead.
        `send`: then hands the file to the machine (see send_and_wind)."""
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
                # The partial page has usually worked it out already.
                location = self.plan_tab.location_for(job, resume.point) or winding.locate(job, resume.point)
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
        # A long program takes a while to write (and as long again to load into
        # the G-Code Preview): that runs on a worker thread behind a progress
        # window. Everything it needs from the app is gathered here, on the GUI
        # thread -- Tk variables and the preview's cache aren't the worker's to read.
        try:
            material = self.build_material()
        except winding.SettingsError:
            material = None
        cached = self.plan_tab.simulation_for(job)
        skipped = location.elapsed if resume is not None else 0.0  # wound before a partial program begins
        start_gcode, end_gcode = self.START_GCODE, self.END_GCODE
        kind = "Partial G-Code" if resume is not None else "G-Code"

        def work(progress):
            result = cached
            if result is None:
                progress(None, "Working out the wind…")
                result = winding.simulate(job)
            # Progress is the winding time written so far, out of the total.
            total = max(1e-9, result.total_time - skipped)
            progress(0.0, "Writing the G-code…")
            # Written to a temporary file next to the target and only moved
            # into place once complete: the machine must never be handed a
            # truncated program (e.g. one missing its END_GCODE) because writing
            # failed -- or was cancelled -- halfway.
            tmp_path = filepath + ".part"
            try:
                with open(tmp_path, "w") as f:
                    winding.write_gcode(f, job, start_gcode, end_gcode, resume=resume,
                                        extra_header=_estimate_header(job, material, result),
                                        on_progress=lambda written: progress(WRITE_SHARE * min(1.0, written / total)))
                os.replace(tmp_path, filepath)
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            # Saved. Cancelling (or failing) from here on only skips the preview.
            try:
                return read_gcode(filepath, lambda f: progress(WRITE_SHARE + (1 - WRITE_SHARE) * f,
                                                               "Loading it into the G-Code Preview…"))
            except (Cancelled, OSError, UnicodeDecodeError, ValueError):
                return None

        def done(data):
            if data is not None:
                self.view_tab.show_file(filepath, data)
            if resume is not None:
                p = resume.point
                self.set_status(f"Partial G-code generated (continues from Layup {p.layup + 1}, Cycle {p.cycle + 1}, "
                                f"A {p.angle:.1f}°): {filepath}")
            else:
                layup_note = f" ({len(job.layups)} layups, {job.total_cycles} cycles)" if len(job.layups) > 1 else ""
                self.set_status(f"G-code generated{layup_note}: {filepath}")
            if send:
                # Straight on to the upload: no "saved" window in between.
                self.root.after(10, lambda: self.control_tab.send_file(filepath))
                return None
            note = "" if data is not None else "\n(Not loaded into the G-Code Preview -- use Open G-Code.)"
            # The file's name, then its folder: a path has no spaces to wrap at.
            return f"{kind} saved", f"{os.path.basename(filepath)}\nin {os.path.dirname(filepath)}{note}"

        ProgressTask(self, f"Generating {kind}", work, on_done=done, finishing="Drawing the preview…",
                     on_error=lambda e: messagebox.showerror("Error", f"G-code generation failed:\n{e}"),
                     on_cancel=lambda: self.set_status("G-code generation cancelled. Nothing was saved."))


if __name__ == "__main__":
    # (No ttkbootstrap default icon: the app sets its own, see theme.apply_icon.)
    root = tb.Window(themename=theme.LIGHT_THEME, iconphoto=None)
    app = CFRPWinderApp(root)
    root.mainloop()
