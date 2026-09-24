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
#
# "layups" are the per-layup strand identity colors, in fixed order (layup 1
# always gets slot 1, etc.). Slot 1 is the original single-strand color, so a
# single-layup wind looks exactly as it always did. The rest were picked to
# stand apart from the mint tank they're drawn on and from EVERY other slot
# (layups overlap on the same tank, so all pairs matter, not just neighbors) --
# under normal vision and simulated protan/deutan color blindness alike. Worst
# case (OKLab dE x100) in either mode: 17.8 normal / 10.5 CVD between any two
# slots, 21.0 normal / 14.7 CVD against the tank (the dataviz targets are >= 15
# normal, >= 8 CVD). Reds, oranges and magentas were deliberately left out: for red-green
# color-blind viewers they collapse into the mint tank itself. The palette tops
# out at six colors before pairs become hard to tell apart; past that,
# layup_style() repeats the colors with a dash pattern, so identity is never
# carried by color alone.
PALETTES = {
    "light": {
        "canvas_bg": "#ffffff",
        "shaft": "#c9cfcd",
        "shaft_outline": "#8a9490",
        "tank": "#5f9b8a",
        "tank_outline": "#33564c",
        "highlight": "#a9d6c9",
        "shadow": "#33564c",
        "layups": ["#f0a202", "#1a237e", "#ffffff", "#1565c0", "#ffee58", "#4e342e"],
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
        "layups": ["#ffce67", "#212121", "#536dfe", "#311b92", "#ffffff", "#880e4f"],
        "reach": "#ffb454",
        "warn_text": "#e2836a",
        "muted": "#7a8486",
    },
}

# Fixed dark "viewport" palette for the G-Code Preview canvas -- does not change
# with the light/dark toggle (see module docstring). Its layup colors are
# validated the same way as above, against the viewport's dark tank (worst
# 15.2 normal / 10.2 CVD between slots).
VIEWPORT_PALETTE = {
    "bg": "#1a1d21",
    "tank": "#2b2f36",
    "tank_outline": "#5a6068",
    "layups": ["#ffd700", "#b39cff", "#ff4d4d", "#f0f0f0", "#ffab91", "#80d8ff"],
    "grid": "#4a5058",
    "eye_line": "#f0a202",
    "eye_marker": "#e5533d",
}

# Dash patterns for layups beyond the palette's color count: the 7th layup
# reuses color 1 dashed, the 13th dash-dotted, and so on. Only patterns that
# Tk's Windows canvas renders reliably on thick lines are used.
LAYUP_DASHES = [None, (8, 4), (12, 4, 3, 4)]


def layup_style(palette, index):
    """(color, dash) identifying the layup at 0-based `index`; dash is None for
    a solid line."""
    colors = palette["layups"]
    cycle, slot = divmod(index, len(colors))
    return colors[slot], LAYUP_DASHES[cycle % len(LAYUP_DASHES)]


def draw_layup_swatch(canvas, x, y, palette, index, width=24, height=12, tags=()):
    """Draws a legend key for a layup at (x, y) (top-left): a small chip of
    tank color with a band of the layup's strand color across it, so the key
    shows exactly how the strand looks on the tank -- including the slots
    (white, near-black) that would vanish against a plain background."""
    color, dash = layup_style(palette, index)
    canvas.create_rectangle(x, y, x + width, y + height, fill=palette["tank"],
                            outline=palette["tank_outline"], tags=tags)
    band = max(3, height // 3)
    canvas.create_line(x + 2, y + height / 2, x + width - 1, y + height / 2, fill=color,
                       width=band, dash=dash or "", capstyle="butt", tags=tags)


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
    # The layup switcher's title ("Layup 2 of 3", sized like the other section
    # headers) and its small square previous/next/add/remove buttons. The buttons
    # derive from the plain TButton on purpose: configuring a bootstyle-derived
    # name (e.g. "Nav.secondary.Outline.TButton") makes ttkbootstrap build that
    # color style app-wide, which restyles other buttons that reference it.
    style.configure("SettingsHeader.TLabel", font=("TkDefaultFont", header_size, "bold"))
    style.configure("Nav.TButton", font=("TkDefaultFont", header_size + 1, "bold"), padding=(2, 0))


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
