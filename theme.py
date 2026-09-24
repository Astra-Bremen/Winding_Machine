"""Visual theming for the CFRP Winder app: ttkbootstrap setup, the app icon, and
the color palette used by the hand-drawn Tkinter Canvas visualizations (which
ttkbootstrap doesn't reach, since they're not ttk widgets).

Kept separate from main.py/plan_tab.py/view_tab.py so the winding-generation
logic in those files never has to be touched to change how the app looks.
"""
import ttkbootstrap as tb

LIGHT_THEME = "minty-light"
DARK_THEME = "minty-dark"

# Colors for the custom-drawn Settings Preview canvases (the 3D tank view and the
# thin end-view). These react to the light/dark toggle. The G-Code Preview's
# viewport intentionally does NOT use this palette -- it stays a fixed dark
# "viewport" in both modes, the same way a CAD/slicer render view commonly stays
# dark regardless of the surrounding UI theme.
PALETTES = {
    "light": {
        "canvas_bg": "#ffffff",
        "shaft": "#c9cfcd",
        "shaft_outline": "#8a9490",
        "tank": "#5f9b8a",
        "tank_outline": "#33564c",
        "highlight": "#a9d6c9",
        "shadow": "#33564c",
        "strand": "#f0a202",
        "reach": "#df8c1f",
        "warn_text": "#cc6041",
        "muted": "#8a9490",
    },
    "dark": {
        "canvas_bg": "#22282a",
        "shaft": "#3a4244",
        "shaft_outline": "#586164",
        "tank": "#5fb39c",
        "tank_outline": "#2e3d3a",
        "highlight": "#8fd6bf",
        "shadow": "#12211d",
        "strand": "#ffce67",
        "reach": "#ffb454",
        "warn_text": "#e2836a",
        "muted": "#7a8486",
    },
}

# Fixed dark "viewport" palette for the G-Code Preview canvas -- does not change
# with the light/dark toggle (see module docstring).
VIEWPORT_PALETTE = {
    "bg": "#1a1d21",
    "tank": "#2b2f36",
    "tank_outline": "#5a6068",
    "strand": "#ffd700",
    "grid": "#4a5058",
    "eye_line": "#f0a202",
    "eye_marker": "#e5533d",
}


def apply(root, mode="light"):
    """Switch `root` (a ttkbootstrap Window, which always carries a `.style`) to
    the given mode ("light"/"dark") and return its Style object. Safe to call
    repeatedly to switch modes at runtime."""
    theme_name = DARK_THEME if mode == "dark" else LIGHT_THEME
    root.style.theme_use(theme_name)
    return root.style


def setup_compact_styles(style, body_size=8, header_size=9):
    """Defines the "Settings.*" style variants used by the left settings panel:
    a slightly smaller body font than the rest of the app so all three settings
    groups comfortably fit on screen without scrolling on a typical display,
    with LabelFrame section headers kept a notch larger (and bold) for
    hierarchy. Custom style configuration survives theme_use() switches (unlike
    plain tk widget options), so this only needs to run once at startup.
    """
    for name in ("Settings.TLabel", "Settings.TCheckbutton", "Settings.TCombobox"):
        style.configure(name, font=("TkDefaultFont", body_size))
    style.configure("Settings.TEntry", font=("TkDefaultFont", body_size))
    style.configure("Settings.TLabelframe.Label", font=("TkDefaultFont", header_size, "bold"))


def build_icon(primary_hex, size=64):
    """Procedurally draws a small abstract "wound coil" badge icon (a few
    concentric rings in the theme's primary color) and returns a PhotoImage
    suitable for root.iconphoto(). Uses Pillow, which ttkbootstrap already pulls
    in as a dependency, so this adds no extra install footprint.
    """
    from PIL import Image, ImageDraw
    from PIL import ImageTk

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx = cy = size / 2.0
    rings = [size * 0.46, size * 0.33, size * 0.20]
    widths = [max(2, size // 14), max(2, size // 16), max(3, size // 10)]
    for r, w in zip(rings, widths):
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=primary_hex, width=w)
    return ImageTk.PhotoImage(img)
