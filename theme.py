"""Visual theming for the CFRP Winder app: ttkbootstrap setup, the app icon, and
the color palette used by the hand-drawn Tkinter Canvas visualizations (which
ttkbootstrap doesn't reach, since they're not ttk widgets).

Kept separate from main.py/plan_tab.py/view_tab.py so the winding-generation
logic in those files never has to be touched to change how the app looks.
"""
import os
import sys
import tempfile
import tkinter
from functools import lru_cache

import ttkbootstrap as tb

LIGHT_THEME = "minty-light"
DARK_THEME = "minty-dark"

# The ASTRA student club's logo (black on white): the mountain-and-rocket mark
# above the ASTRA lettering. The app shows it in the club's purple -- the mark
# as the window icon (white on a purple tile), the mark and the name side by
# side in the status bar's corner -- colored from this one file at startup.
LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "astra_logo.png")
ASTRA_PURPLE = {"light": "#5b2a86", "dark": "#b48ce6"}  # the club's purple; lightened to read on the dark theme

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
        "auto_text": "#a2aaa7",  # an entry field's value while the app sets it (vs. #5a5a5a typed)
        # Machine-position markers drawn over the tank and strands (the wind
        # start): ink-dark on a light halo, so they read on any strand color.
        "marker": "#1f2d2a",
        "marker_halo": "#ffffff",
        # The LIMIT tag on the speed limit that sets the selected layup's pace:
        # the theme's mint, darkened to read at tag size on a white field (5:1).
        "limit_tag": "#2b7d66",
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
        "auto_text": "#7f8a8c",
        "marker": "#eef3f1",
        "marker_halo": "#22282a",
        "limit_tag": "#93cebd",  # the theme's own mint (7.5:1 on the dark field)
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
    # Fields a little shorter than the theme's roomy default (6 px padding all
    # round), in keeping with the smaller font -- the panel's many rows then
    # fit a maximized window with room to spare. The combobox matches them; the
    # spinbox (partial-export page) gets the same padding, though its arrow
    # images keep it a couple of pixels taller.
    style.configure("Settings.TEntry", font=("TkDefaultFont", body_size), padding=(6, 4))
    style.configure("Settings.TCombobox", padding=(6, 5, 9, 3))
    style.configure("Settings.TSpinbox", padding=(13, 4))
    style.configure("Settings.TLabelframe.Label", font=("TkDefaultFont", header_size, "bold"))
    # The layup switcher's title ("Layup 2 of 3", sized like the other section
    # headers) and its small square previous/next/add/remove buttons. The buttons
    # derive from the plain TButton on purpose: configuring a bootstyle-derived
    # name (e.g. "Nav.secondary.Outline.TButton") makes ttkbootstrap build that
    # color style app-wide, which restyles other buttons that reference it.
    style.configure("SettingsHeader.TLabel", font=("TkDefaultFont", header_size, "bold"))
    # A page's title in the settings panel (the partial-export page).
    style.configure("PageTitle.TLabel", font=("TkDefaultFont", header_size + 2, "bold"))
    style.configure("Nav.TButton", font=("TkDefaultFont", header_size + 1, "bold"), padding=(2, 0))
    # The settings panel's action buttons: Generate filled, Open outlined, both
    # in the theme's primary color. ttkbootstrap only builds a color style once
    # it's configured (otherwise not until the first theme switch, leaving them
    # plain grey until then), so build exactly these two up front.
    style.configure("primary.TButton")
    style.configure("primary.Outline.TButton")
    # Likewise for the other color styles in use -- the Eye Reach readout
    # (orange, like the reach lines it goes with) and the viewer's Play
    # button -- so they look the same from startup, not just after a switch.
    style.configure("warning.TLabel")
    style.configure("success.TButton")


@lru_cache(maxsize=1)
def _logo_parts():
    """(mark, name): the logo's two parts as alpha masks (Pillow "L" images,
    ink opaque, cropped to it), or None if the file can't be read. The parts
    are told apart by the widest band of empty rows between them."""
    from PIL import Image, ImageOps
    try:
        ink = ImageOps.invert(Image.open(LOGO_PATH).convert("L"))
    except OSError:
        return None
    w, h = ink.size
    profile = ink.resize((1, h), Image.Resampling.BOX).tobytes()  # each row's mean ink
    rows = [y for y, v in enumerate(profile) if v > 0]
    if len(rows) < 2:
        return None
    _, split = max((b - a, (a + b) // 2) for a, b in zip(rows, rows[1:]))
    parts = []
    for box in ((0, 0, w, split), (0, split, w, h)):
        part = ink.crop(box)
        bbox = part.getbbox()
        if bbox is None:
            return None
        parts.append(part.crop(bbox))
    return tuple(parts)


def _ink(mask, color):
    # The mask's shape filled with `color`, on transparent.
    from PIL import Image
    img = Image.new("RGBA", mask.size, color)
    img.putalpha(mask)
    return img


def logo_lockup(height, color):
    """The logo for small spaces: the mark with the ASTRA name beside it,
    `height` px tall, in `color` -- a Pillow image, or None without the file."""
    from PIL import Image
    parts = _logo_parts()
    if parts is None:
        return None
    mark, name = parts
    lanczos = Image.Resampling.LANCZOS
    m = mark.resize((max(1, round(mark.width * height / mark.height)), height), lanczos)
    name_h = max(1, round(height * 0.44))
    n = name.resize((max(1, round(name.width * name_h / name.height)), name_h), lanczos)
    gap = max(2, round(height * 0.3))
    mask = Image.new("L", (m.width + gap + n.width, height), 0)
    mask.paste(m, (0, 0))
    mask.paste(n, (m.width + gap, (height - name_h) // 2))
    return _ink(mask, color)


def _logo_icon(size, tile, ink="#ffffff"):
    # The window icon: the mark in `ink` on a rounded square of `tile`, drawn
    # four times as large and scaled down, so its edges come out smooth.
    from PIL import Image, ImageDraw
    big = size * 4
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle((0, 0, big - 1, big - 1), radius=round(big * 0.22), fill=tile)
    mark = _logo_parts()[0]
    fit = big * 0.8 / max(mark.width, mark.height)
    m = mark.resize((round(mark.width * fit), round(mark.height * fit)), Image.Resampling.LANCZOS)
    img.alpha_composite(_ink(m, ink), ((big - m.width) // 2, (big - m.height) // 2))
    return img.resize((size, size), Image.Resampling.LANCZOS)


def build_icons(primary_hex):
    """The window icon, as PhotoImages in a few sizes for root.iconphoto(): the
    ASTRA mark on a purple tile -- or, without the logo file, a small "wound
    coil" badge in the theme's primary color."""
    from PIL import ImageTk
    if _logo_parts() is not None:
        return [ImageTk.PhotoImage(_logo_icon(size, ASTRA_PURPLE["light"])) for size in (16, 32, 48, 64)]
    return [build_icon(primary_hex)]


def apply_icon(root, primary_hex):
    """Gives every window of the app its icon (see build_icons). Returns the
    PhotoImages to keep referenced (none where they aren't used). On Windows
    the icon goes over as a multi-size .ico written to the temp folder, the way
    ttkbootstrap sets its own default: there, a photo icon doesn't take (the
    title bar shows a blank one), and the .ico also gives the taskbar a large
    one. It's set for the main window and as every other window's default."""
    if sys.platform == "win32" and _logo_parts() is not None:
        path = os.path.join(tempfile.gettempdir(), "cfrp_winder_astra.ico")
        try:
            _logo_icon(256, ASTRA_PURPLE["light"]).save(
                path, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
            root.wm_iconbitmap(path)
            root.wm_iconbitmap(default=path)
            return []
        except (OSError, tkinter.TclError):
            pass  # fall back to the photo icon
    images = build_icons(primary_hex)
    root.iconphoto(True, *images)
    return images


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
