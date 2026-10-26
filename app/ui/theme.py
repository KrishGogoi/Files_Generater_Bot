"""Visual identity for Krishna's Super Bot: colour tokens, QSS and QPalette.

Everything cosmetic lives here so the screens stay about behaviour. Four things
are worth knowing before editing:

* **No fractional ``rgba()`` in the stylesheet.** Qt's CSS parser reads the alpha
  channel of ``rgba(r, g, b, a)`` as an integer, so ``0.14`` silently becomes
  fully transparent. Every "translucent" token is therefore pre-blended against
  its own background by `_mix` and shipped as a solid hex -- except the handful of
  deliberate glass surfaces in `_QSS_WALLPAPER`, which use *integer* alphas
  (``224``, not ``0.88``) because they have a photograph behind them.
* **The dark theme is one picture.** Its tokens are sampled from
  `wallpaper_dark.jpg` and the same image is painted behind the window, so
  changing one without the other pulls them apart. The light theme has no
  wallpaper at all: a photograph behind a white working surface fights every
  panel on screen.
* **Never style bare ``QFrame``.** ``QLabel`` and every ``QAbstractScrollArea``
  inherit from it, so a bare ``QFrame { border: ... }`` rule boxes in half the
  app. Cards are selected by ``QFrame[card="true"]`` or by object name.
* Dynamic-property selectors (``[variant="primary"]``, ``[dragActive="true"]``)
  only re-evaluate after a style re-polish -- use `repolish` after every
  ``setProperty``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPalette, QPixmap
from PySide6.QtWidgets import QApplication, QStyleFactory, QWidget

_LOG = logging.getLogger(__name__)

#: Small style assets shipped beside this module (see the check-box tick).
_ASSETS = Path(__file__).resolve().parent / "assets"

#: The app's own logo -- the one on screen: window icon, dashboard header, lock
#: screen. Deliberately NOT `BrandConfig.logo_path`, which is the academy logo
#: that goes on the PowerPoint title slide. The two are different marks for
#: different audiences and must not be shared.
APP_LOGO_PATH = _ASSETS / "app_logo.jpg"

#: Window background for the dark theme only. The light theme is a paper-white
#: working surface and a photograph behind it would fight every card on screen.
WALLPAPER_PATH = _ASSETS / "wallpaper_dark.jpg"

#: How much black is laid over the wallpaper before anything is drawn on it.
#: Chosen so the brightest part of the image -- the moon, at 85% lightness --
#: still sits far enough below the body text colour to read cleanly; without it,
#: a label that happens to land on the moon is illegible.
WALLPAPER_SCRIM = 0.62

# --------------------------------------------------------------------------- #
# Fonts
# --------------------------------------------------------------------------- #

#: UI font fallback chain. Segoe UI Variable is the Windows 11 face; Segoe UI
#: covers Windows 10; the rest keeps the app legible on a non-Windows box.
FONT_FAMILIES: tuple[str, ...] = (
    "Segoe UI Variable",
    "Segoe UI",
    "Inter",
    "Noto Sans",
    "DejaVu Sans",
)

#: Monochrome glyph faces, used for the icon labels. Segoe UI Symbol comes first
#: on purpose: Segoe UI Emoji would paint the same code points in colour, which
#: looks accidental next to a flat UI.
GLYPH_FAMILIES: tuple[str, ...] = (
    "Segoe UI Symbol",
    "Segoe UI",
    "DejaVu Sans",
)

_FONT_CSS = ", ".join(f'"{f}"' for f in FONT_FAMILIES) + ", sans-serif"
_GLYPH_CSS = ", ".join(f'"{f}"' for f in GLYPH_FAMILIES) + ", sans-serif"

#: Base UI size in px. Qt style sheets win over ``QApplication.setFont``, so this
#: is the single source of truth for body text.
BASE_FONT_PX = 13


# --------------------------------------------------------------------------- #
# Colour tokens
# --------------------------------------------------------------------------- #

_BRAND_BLUE = "#141E52"      # Cerebellum deep blue, from the PPT builders
_BRAND_BLUE_SOFT = "#26356F"
_ACCENT = "#1AB7EA"          # Cerebellum cyan rule colour
_QUESTION_RED = "#E23A3A"    # the red used for question text in the decks
_AMBER = "#F2B33D"

#: One hue per feature tile, used *only* for the icon square on a card -- never
#: for text, a border or a whole card. Two rules decide this set:
#:
#: * **All cool, none warm.** Amber means "a human needs to look at this" and red
#:   means "this is wrong" everywhere else in the app, so a warm tile would read
#:   as a status rather than as decoration.
#: * **Low saturation.** Six full-strength hues turn the home page into a toy.
#:   These are mixed down further still by `tile_colors` before they are painted.
TINTS: dict[str, str] = {
    "extract": "#5B87C7",    # slate blue
    "answers": "#57A07A",    # sage
    "pptx": "#8A7BB8",       # muted violet
    "merge": "#4F9AA0",      # teal
    "bulk": "#6C7DC4",       # indigo
    "settings": "#7D8C99",   # grey -- Settings is plumbing, not a feature
}


def _rgb(value: str) -> tuple[int, int, int]:
    h = value.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _mix(top: str, bottom: str, alpha: float) -> str:
    """Flatten `top` at `alpha` over `bottom` and return a solid ``#rrggbb``.

    Used instead of ``rgba()`` because Qt's stylesheet parser does not accept a
    fractional alpha (see the module docstring).
    """
    a = max(0.0, min(1.0, alpha))
    tr, tg, tb = _rgb(top)
    br, bg_, bb = _rgb(bottom)
    r = round(tr * a + br * (1.0 - a))
    g = round(tg * a + bg_ * (1.0 - a))
    b = round(tb * a + bb * (1.0 - a))
    return f"#{r:02X}{g:02X}{b:02X}"


def tile_colors(name: str, dark: bool = False, surface: str | None = None) -> tuple[str, str]:
    """``(background, glyph)`` for the icon square of feature `name`.

    The hue from `TINTS` is mixed down hard against the card it sits on -- a
    sixth of it for the square, and lightened (dark theme) or darkened (light
    theme) for the glyph so the mark keeps its contrast without shouting.
    Unknown names fall back to the grey used by Settings.
    """
    hue = TINTS.get(name, TINTS["settings"])
    under = surface or (DARK["surface"] if dark else LIGHT["surface"])
    if dark:
        # A shade stronger than the light theme's: the dark cards are glass over
        # the wallpaper, which washes a tint out again on its way to the screen.
        return _mix(hue, under, 0.21), _mix("#FFFFFF", hue, 0.38)
    return _mix(hue, under, 0.13), _mix("#000000", hue, 0.30)


#: Brand constants, identical in both themes.
BRAND: dict[str, str] = {
    "brand": _BRAND_BLUE,
    "brand_soft": _BRAND_BLUE_SOFT,
    "accent": _ACCENT,
    "red": _QUESTION_RED,
    "amber": _AMBER,
    "green": "#3ECF8E",
}


#: Dark-theme key colours, sampled from `wallpaper_dark.jpg` so the chrome and
#: the wallpaper are one picture rather than two. The image is a single hue
#: family -- everything in it sits at h≈200-208°, with saturation falling from
#: 66% in the deepest part of the suit to 20% in the lit cloud, and lightness
#: running 5% (night sky, stone) to 85% (moon).
_BAT_NIGHT = "#070C13"       # night sky top-left / stone plinth, l≈6%
_BAT_SUIT = "#0C1721"        # chest armour, the darkest large area
_BAT_CAPE = "#111E29"        # cape mid-tone
_BAT_CAPE_LIT = "#264354"    # the lit edge of the cape -- strongest line colour
_BAT_MOON = "#C6E3EB"        # moon core
_BAT_MOONLIGHT = "#8FC0D6"   # the moonlit rim on the cowl and shoulders


def _dark_tokens() -> dict[str, str]:
    """The Batman palette: gunmetal blue-black surfaces, moonlight for accent.

    The accent is the moonlight blue off the suit, **not** bat-signal gold. Gold
    would read as more obviously Batman, but amber already means "this question
    needs a human" everywhere in the review table -- and an accent that looks
    like a warning is a worse app, however good it looks in a screenshot.
    """
    bg = _BAT_NIGHT
    surface = _BAT_SUIT
    sunken = "#060B11"
    accent = _BAT_MOONLIGHT
    sidebar = "#09111A"
    return {
        # surfaces
        "bg": bg,
        "bg_alt": "#0A121A",
        "surface": surface,
        # the nav rail: one step up from the window so it reads as a panel
        "sidebar_bg": sidebar,
        "nav_active_bg": _mix(accent, sidebar, 0.16),
        "surface_alt": _BAT_CAPE,
        "surface_sunken": sunken,
        "hover_bg": "#162734",
        "press_bg": "#0A1620",
        "chip_bg": _BAT_CAPE,
        "chip_border": "#274050",
        "row_alt": "#0A141C",
        "header_bg": "#101C26",
        "tooltip_bg": "#04080C",
        "tooltip_text": "#E4F1F7",
        # lines
        "border": "#1B2E3B",
        "border_strong": _BAT_CAPE_LIT,
        "disabled_border": "#152430",
        # text
        "text": "#E4F1F7",
        "text_dim": "#A3BCC9",
        "text_faint": "#6F8B9A",
        "disabled_bg": "#0B141B",
        "disabled_text": "#546C7A",
        # accent -- moonlight
        "accent": accent,
        "accent_hover": _BAT_MOON,
        "accent_press": "#6DA2BA",
        "accent_fg": "#05131B",
        "accent_soft": _mix(accent, surface, 0.16),
        "accent_sunk": _mix(accent, sunken, 0.12),
        "focus": _BAT_MOON,
        "focus_ring": _mix(accent, bg, 0.45),
        "selection": _mix(accent, surface, 0.30),
        # status -- semantic, deliberately NOT from the wallpaper
        "red": "#E4574E",
        "red_soft": _mix(_QUESTION_RED, surface, 0.20),
        "red_text": "#FF9E95",
        "amber": _AMBER,
        "amber_text": "#F7D48F",
        "amber_soft": _mix(_AMBER, surface, 0.14),
        "amber_border": _mix(_AMBER, surface, 0.45),
        "green": "#3ECF8E",
        "green_soft": _mix("#3ECF8E", surface, 0.16),
    }


def _light_tokens() -> dict[str, str]:
    bg = "#F3F6FC"
    surface = "#FFFFFF"
    sunken = "#F7F9FE"
    accent = "#0E9BCB"  # darkened cyan: #1AB7EA on white fails contrast for text
    return {
        "bg": bg,
        "bg_alt": "#EBF0F9",
        "surface": surface,
        "sidebar_bg": surface,
        "nav_active_bg": _mix(_ACCENT, surface, 0.13),
        "surface_alt": "#F1F5FD",
        "surface_sunken": sunken,
        "hover_bg": "#E7EDF9",
        "press_bg": "#DDE5F4",
        "chip_bg": "#EEF3FC",
        "chip_border": "#CFD9EC",
        "row_alt": "#F7F9FE",
        "header_bg": "#EDF2FB",
        "tooltip_bg": _BRAND_BLUE,
        "tooltip_text": "#F4F7FF",
        "border": "#D6DEEE",
        "border_strong": "#B4C0D8",
        "disabled_border": "#E2E8F3",
        "text": _BRAND_BLUE,
        "text_dim": "#4B5677",
        "text_faint": "#7C87A3",
        "disabled_bg": "#EEF1F8",
        "disabled_text": "#A2AAC0",
        "accent": accent,
        "accent_hover": _ACCENT,
        "accent_press": "#0B7CA3",
        "accent_fg": "#FFFFFF",
        "accent_soft": _mix(_ACCENT, surface, 0.14),
        "accent_sunk": _mix(_ACCENT, sunken, 0.10),
        "focus": accent,
        "focus_ring": _mix(_ACCENT, bg, 0.40),
        "selection": _mix(_ACCENT, surface, 0.26),
        "red": "#D93636",
        "red_soft": _mix(_QUESTION_RED, surface, 0.12),
        "red_text": "#B02525",
        "amber": _AMBER,
        "amber_text": "#8A5A00",
        "amber_soft": _mix(_AMBER, surface, 0.20),
        "amber_border": _mix("#C98A0E", surface, 0.55),
        "green": "#1E8E5A",
        "green_soft": _mix("#1E8E5A", surface, 0.12),
    }


DARK: dict[str, str] = _dark_tokens()
LIGHT: dict[str, str] = _light_tokens()

#: Flat token map for the default (light) theme, plus the two per-theme maps
#: under ``COLORS["dark"]`` / ``COLORS["light"]``. `palette` is the tidier
#: accessor. Light is the default: this is a daytime desk tool whose output is
#: black-on-white documents, so a bright UI matches the work.
COLORS: dict[str, Any] = {**BRAND, **LIGHT, "dark": DARK, "light": LIGHT}


def palette(dark: bool = False) -> dict[str, str]:
    """The flat token map for one theme (brand keys first, theme keys win)."""
    return {**BRAND, **(DARK if dark else LIGHT)}


# --------------------------------------------------------------------------- #
# Style sheet
# --------------------------------------------------------------------------- #

# %(token)s interpolation is used rather than an f-string so the QSS braces stay
# readable. Any literal percent sign in here would have to be doubled.
_QSS = """
/* ---------------------------------------------------------------- base ---- */
QWidget {
    color: %(text)s;
    font-family: %(font)s;
    font-size: %(base_px)dpx;
}
QMainWindow, QDialog, QStackedWidget,
QWidget[screen="true"] {
    background-color: %(bg)s;
}
QLabel {
    background: transparent;
}
QLabel:disabled, QCheckBox:disabled, QRadioButton:disabled {
    color: %(disabled_text)s;
}
QScrollArea {
    background: transparent;
    border: none;
}
QWidget#screenInner, QScrollArea > QWidget > QWidget {
    background: transparent;
}
QFrame#separator {
    background-color: %(border)s;
    border: none;
    max-height: 1px;
    min-height: 1px;
}

/* --------------------------------------------------------- typography ---- */
QLabel#h1 {
    font-size: 24px;
    font-weight: bold;
    color: %(text)s;
}
QLabel#h2 {
    font-size: 17px;
    font-weight: bold;
    color: %(text)s;
}
QLabel#subtitle {
    font-size: 13px;
    color: %(text_dim)s;
}
QLabel#sectionLabel {
    font-size: 11px;
    font-weight: bold;
    color: %(text_faint)s;
}
QLabel#fieldLabel {
    font-size: 12px;
    color: %(text_dim)s;
}
QLabel#footer, QLabel#hint {
    font-size: 12px;
    color: %(text_faint)s;
}

/* -------------------------------------------------------------- cards ---- */
QFrame[card="true"] {
    background-color: %(surface)s;
    border: 1px solid %(border)s;
    border-radius: 8px;
}

/* ------------------------------------------------------------ buttons ---- */
QPushButton {
    background-color: %(surface_alt)s;
    color: %(text)s;
    border: 1px solid %(border_strong)s;
    border-radius: 7px;
    padding: 6px 13px;
    min-height: 16px;
    font-size: 12px;
    font-weight: bold;
}
QPushButton:hover {
    background-color: %(hover_bg)s;
    border-color: %(accent)s;
}
QPushButton:pressed {
    background-color: %(press_bg)s;
}
QPushButton:focus {
    border-color: %(focus)s;
    outline: 2px solid %(focus_ring)s;
    outline-offset: 1px;
}
QPushButton:disabled {
    background-color: %(disabled_bg)s;
    color: %(disabled_text)s;
    border-color: %(disabled_border)s;
}
QPushButton[variant="primary"] {
    background-color: %(accent)s;
    color: %(accent_fg)s;
    border: 1px solid %(accent_press)s;
    padding: 7px 18px;
    font-size: 13px;
}
QPushButton[variant="primary"]:hover {
    background-color: %(accent_hover)s;
}
QPushButton[variant="primary"]:pressed {
    background-color: %(accent_press)s;
}
QPushButton[variant="primary"]:focus {
    border-color: %(text)s;
    outline: 2px solid %(focus_ring)s;
    outline-offset: 2px;
}
QPushButton[variant="primary"]:disabled {
    background-color: %(disabled_bg)s;
    color: %(disabled_text)s;
    border-color: %(disabled_border)s;
}
QPushButton[variant="ghost"] {
    background: transparent;
    border: 1px solid transparent;
    color: %(text_dim)s;
    font-weight: normal;
}
QPushButton[variant="ghost"]:hover {
    background-color: %(surface_alt)s;
    color: %(text)s;
    border-color: %(border)s;
}
QPushButton[variant="ghost"]:pressed {
    background-color: %(press_bg)s;
}
QPushButton[variant="link"] {
    background: transparent;
    border: none;
    color: %(accent)s;
    padding: 2px 4px;
    font-weight: normal;
    text-decoration: underline;
}
QPushButton[variant="link"]:hover {
    color: %(accent_hover)s;
}
QPushButton[variant="link"]:focus {
    outline: 2px solid %(focus_ring)s;
    outline-offset: 1px;
}
QPushButton[variant="danger"] {
    background-color: %(red_soft)s;
    color: %(red_text)s;
    border: 1px solid %(red)s;
}
QPushButton[variant="danger"]:hover {
    background-color: %(red)s;
    color: #FFFFFF;
}
QPushButton#iconButton {
    min-width: 30px;
    max-width: 30px;
    min-height: 30px;
    max-height: 30px;
    padding: 0px;
    border-radius: 15px;
    font-family: %(glyph_font)s;
    font-size: 16px;
    font-weight: normal;
    color: %(text_dim)s;
}
QPushButton#iconButton:hover {
    color: %(text)s;
}
QPushButton#chipClose {
    min-width: 18px;
    max-width: 18px;
    min-height: 18px;
    max-height: 18px;
    padding: 0px;
    border: none;
    border-radius: 9px;
    background: transparent;
    color: %(text_faint)s;
    font-size: 12px;
    font-weight: bold;
}
QPushButton#chipClose:hover {
    background-color: %(red)s;
    color: #FFFFFF;
}
QPushButton#chipClose:focus {
    outline: 2px solid %(focus_ring)s;
    outline-offset: 0px;
}

/* ---------------------------------------------------------- drop zone ---- */
QFrame#dropZone {
    background-color: %(surface_sunken)s;
    border: 2px dashed %(border_strong)s;
    border-radius: 10px;
}
QFrame#dropZone:hover {
    border-color: %(accent)s;
    background-color: %(accent_sunk)s;
}
QFrame#dropZone:focus {
    border-color: %(focus)s;
    background-color: %(accent_sunk)s;
}
QFrame#dropZone[dragActive="true"] {
    border: 2px solid %(accent)s;
    background-color: %(accent_soft)s;
}
QFrame#dropZone[dragActive="reject"] {
    border: 2px solid %(red)s;
    background-color: %(red_soft)s;
}
QLabel#dropGlyph {
    font-family: %(glyph_font)s;
    font-size: 30px;
    color: %(accent)s;
}
QLabel#dropTitle {
    font-size: 15px;
    font-weight: bold;
    color: %(text)s;
}
QLabel#dropHint {
    font-size: 12px;
    color: %(text_faint)s;
}

/* --------------------------------------------------------- file chips ---- */
QFrame#chip {
    background-color: %(chip_bg)s;
    border: 1px solid %(chip_border)s;
    border-radius: 8px;
}
QFrame#chip[bad="true"] {
    border-color: %(red)s;
    background-color: %(red_soft)s;
}
/* A chip that is itself a click target (the History rows). */
QFrame#chip[clickable="true"]:hover {
    background-color: %(hover_bg)s;
    border-color: %(accent)s;
}
QFrame#chip[clickable="true"]:focus {
    border-color: %(focus)s;
}
QFrame#chip:disabled {
    background-color: %(disabled_bg)s;
    border-color: %(disabled_border)s;
}
QLabel#chipName {
    font-size: 13px;
    color: %(text)s;
}
QLabel#chipMeta {
    font-size: 11px;
    color: %(text_faint)s;
}

/* ------------------------------------------------------- notices/banner -- */
/* The app-level "running without AI" strip across the top of the window. Amber
   because it is a warning, but the theme's amber rather than a hard-coded gold:
   it sits above every screen and must not be the loudest thing on any of them. */
QWidget#aiBanner {
    background-color: %(amber_soft)s;
    border-bottom: 1px solid %(amber_border)s;
}
QWidget#aiBanner QLabel {
    color: %(amber_text)s;
    font-weight: bold;
}
QFrame#banner {
    background-color: %(amber_soft)s;
    border: 1px solid %(amber_border)s;
    border-left: 3px solid %(amber)s;
    border-radius: 8px;
}
QLabel#bannerGlyph {
    font-family: %(glyph_font)s;
    font-size: 16px;
    color: %(amber)s;
}
QLabel#bannerText {
    font-size: 13px;
    color: %(amber_text)s;
}
QLabel#notice {
    font-size: 12px;
    color: %(amber_text)s;
    background-color: %(amber_soft)s;
    border: 1px solid %(amber_border)s;
    border-radius: 6px;
    padding: 6px 10px;
}
QLabel#notice[level="error"] {
    color: %(red_text)s;
    background-color: %(red_soft)s;
    border-color: %(red)s;
}
QLabel#notice[level="info"] {
    color: %(text_dim)s;
    background-color: %(surface_alt)s;
    border-color: %(border)s;
}

/* ------------------------------------------------------- action cards ---- */
QFrame#actionCard {
    background-color: %(surface)s;
    border: 1px solid %(border)s;
    border-radius: 8px;
}
QFrame#actionCard:hover {
    background-color: %(surface_alt)s;
    border-color: %(accent)s;
}
QFrame#actionCard:focus {
    border: 1px solid %(focus)s;
    background-color: %(surface_alt)s;
    outline: 2px solid %(focus_ring)s;
    outline-offset: 1px;
}
QFrame#actionCard:disabled {
    background-color: %(disabled_bg)s;
    border: 1px dashed %(disabled_border)s;
}
QLabel#cardGlyph {
    font-family: %(glyph_font)s;
    font-size: 21px;
    color: %(accent)s;
}
QLabel#cardTitle {
    font-size: 14px;
    font-weight: bold;
    color: %(text)s;
}
QLabel#cardDesc {
    font-size: 12px;
    color: %(text_dim)s;
}
QLabel#cardReason {
    font-size: 11px;
    font-weight: bold;
    color: %(amber_text)s;
}
/* The icon square. Its two colours come from the generated [tint="..."] rules
   appended by `stylesheet`; this is only the shape. */
QLabel#cardTile {
    background-color: %(chip_bg)s;
    color: %(accent)s;
    border-radius: 12px;
    font-family: %(glyph_font)s;
    font-size: 19px;
}
QLabel#cardCta {
    font-size: 12px;
    font-weight: bold;
    color: %(accent)s;
}
QLabel#cardArrow {
    background-color: %(surface_sunken)s;
    color: %(text_dim)s;
    border: 1px solid %(border)s;
    border-radius: 13px;
    font-family: %(glyph_font)s;
    font-size: 13px;
}
QLabel#cardCta:disabled, QLabel#cardArrow:disabled {
    color: %(disabled_text)s;
}

/* ------------------------------------------------------------ sidebar ---- */
QWidget#sidebar {
    background-color: %(sidebar_bg)s;
    border-right: 1px solid %(border)s;
}
QLabel#sidebarTitle {
    font-size: 14px;
    font-weight: bold;
    color: %(text)s;
}
QLabel#sidebarTagline, QLabel#sidebarVersion {
    font-size: 11px;
    color: %(text_faint)s;
}
QFrame#navItem {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 8px;
}
QFrame#navItem:hover {
    background-color: %(hover_bg)s;
}
QFrame#navItem:focus {
    border-color: %(focus)s;
}
QFrame#navItem[active="true"] {
    background-color: %(nav_active_bg)s;
}
QLabel#navGlyph {
    font-family: %(glyph_font)s;
    font-size: 14px;
    color: %(text_faint)s;
}
QLabel#navText {
    font-size: 13px;
    color: %(text_dim)s;
}
QLabel#navGlyph[active="true"] {
    color: %(accent)s;
}
QLabel#navText[active="true"] {
    color: %(text)s;
    font-weight: bold;
}
/* The quote in the bottom-left corner. */
QFrame#quoteCard {
    background-color: %(surface_alt)s;
    border: 1px solid %(border)s;
    border-left: 2px solid %(accent)s;
    border-radius: 8px;
}
QLabel#quoteText {
    font-size: 11px;
    font-style: italic;
    color: %(text_dim)s;
}

/* ----------------------------------------------------- dashboard head ---- */
QLabel#eyebrow {
    font-size: 11px;
    font-weight: bold;
    color: %(text_faint)s;
}
QLabel#greeting {
    font-size: 27px;
    font-weight: bold;
    color: %(text)s;
}

/* ------------------------------------------------------- status strip ---- */
QFrame#statusStrip {
    background-color: %(surface)s;
    border: 1px solid %(border)s;
    border-radius: 10px;
}
QLabel#statusDot {
    font-family: %(glyph_font)s;
    font-size: 11px;
    color: %(green)s;
}
QLabel#statusDot[state="off"] {
    color: %(amber)s;
}
QLabel#statusText {
    font-size: 13px;
    color: %(text)s;
}
QLabel#statusHint {
    font-size: 11px;
    color: %(text_faint)s;
}
QPushButton#pill {
    background-color: %(chip_bg)s;
    color: %(text_dim)s;
    border: 1px solid %(chip_border)s;
    border-radius: 13px;
    padding: 5px 14px;
    font-size: 12px;
    font-weight: bold;
}
QPushButton#pill:hover {
    background-color: %(hover_bg)s;
    border-color: %(accent)s;
    color: %(text)s;
}
QPushButton#pill:pressed {
    background-color: %(press_bg)s;
}

/* ------------------------------------------------------------- inputs ---- */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QDateEdit {
    background-color: %(surface_sunken)s;
    color: %(text)s;
    border: 1px solid %(border)s;
    border-radius: 7px;
    padding: 5px 9px;
    selection-background-color: %(selection)s;
    selection-color: %(text)s;
}
QLineEdit:hover, QPlainTextEdit:hover, QTextEdit:hover,
QSpinBox:hover, QDoubleSpinBox:hover, QDateEdit:hover {
    border-color: %(border_strong)s;
}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus, QDateEdit:focus {
    border: 1px solid %(accent)s;
    background-color: %(surface)s;
}
QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled,
QSpinBox:disabled, QDoubleSpinBox:disabled, QDateEdit:disabled {
    background-color: %(disabled_bg)s;
    color: %(disabled_text)s;
    border-color: %(disabled_border)s;
}
QLineEdit[invalid="true"] {
    border-color: %(red)s;
}
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {
    width: 16px;
    border: none;
    background: transparent;
}
QComboBox {
    background-color: %(surface_sunken)s;
    color: %(text)s;
    border: 1px solid %(border)s;
    border-radius: 7px;
    padding: 5px 9px;
    min-height: 16px;
}
QComboBox:hover {
    border-color: %(border_strong)s;
}
QComboBox:focus, QComboBox:on {
    border: 1px solid %(accent)s;
}
QComboBox:disabled {
    background-color: %(disabled_bg)s;
    color: %(disabled_text)s;
}
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 24px;
    border: none;
}
QComboBox::down-arrow {
    /* Pure-CSS caret: avoids shipping an icon resource. */
    image: none;
    width: 0px;
    height: 0px;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid %(text_dim)s;
    margin-right: 9px;
}
QComboBox QAbstractItemView {
    background-color: %(surface)s;
    color: %(text)s;
    border: 1px solid %(border_strong)s;
    border-radius: 8px;
    padding: 4px;
    outline: none;
    selection-background-color: %(accent_soft)s;
    selection-color: %(text)s;
}
QCheckBox, QRadioButton {
    spacing: 8px;
    background: transparent;
}
QCheckBox::indicator, QRadioButton::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid %(border_strong)s;
    background-color: %(surface_sunken)s;
}
QCheckBox::indicator {
    border-radius: 4px;
}
QRadioButton::indicator {
    border-radius: 8px;
}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {
    border-color: %(accent)s;
}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {
    background-color: %(accent)s;
    border-color: %(accent_press)s;
}
QCheckBox::indicator:checked {
    /* A real tick, not just a filled square: these boxes decide what goes into
       an export, so "on" has to be unmistakable at a glance. */
    image: url("%(check_icon)s");
}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {
    background-color: %(disabled_bg)s;
    border-color: %(disabled_border)s;
}

/* -------------------------------------------------------------- table ---- */
QTableView, QTreeView, QListView {
    background-color: %(surface)s;
    alternate-background-color: %(row_alt)s;
    color: %(text)s;
    border: 1px solid %(border)s;
    border-radius: 8px;
    gridline-color: %(border)s;
    outline: none;
    selection-background-color: %(selection)s;
    selection-color: %(text)s;
}
QTableView::item, QTreeView::item, QListView::item {
    padding: 3px 7px;
    border: none;
}
QTableView::item:selected, QTreeView::item:selected, QListView::item:selected {
    background-color: %(selection)s;
    color: %(text)s;
}
QTableView::item:focus {
    border: none;
}
QHeaderView {
    background: transparent;
    border: none;
}
QHeaderView::section {
    background-color: %(header_bg)s;
    color: %(text_dim)s;
    padding: 5px 9px;
    border: none;
    border-right: 1px solid %(border)s;
    border-bottom: 1px solid %(border)s;
    font-size: 12px;
    font-weight: bold;
}
QHeaderView::section:last {
    border-right: none;
}
QHeaderView::section:hover {
    color: %(text)s;
}
QTableCornerButton::section {
    background-color: %(header_bg)s;
    border: none;
    border-bottom: 1px solid %(border)s;
}

/* --------------------------------------------------------- scrollbars ---- */
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 2px 2px 2px 0px;
}
QScrollBar:horizontal {
    background: transparent;
    height: 10px;
    margin: 0px 2px 2px 2px;
}
QScrollBar::handle:vertical {
    background-color: %(border_strong)s;
    min-height: 32px;
    border-radius: 5px;
    margin: 0px 2px;
}
QScrollBar::handle:horizontal {
    background-color: %(border_strong)s;
    min-width: 32px;
    border-radius: 5px;
    margin: 2px 0px;
}
QScrollBar::handle:hover {
    background-color: %(accent)s;
}
QScrollBar::add-line, QScrollBar::sub-line {
    width: 0px;
    height: 0px;
    border: none;
    background: transparent;
}
QScrollBar::add-page, QScrollBar::sub-page {
    background: transparent;
}

/* ------------------------------------------------------- misc chrome ----- */
QToolTip {
    background-color: %(tooltip_bg)s;
    color: %(tooltip_text)s;
    border: 1px solid %(border_strong)s;
    border-radius: 6px;
    padding: 6px 9px;
    font-size: 12px;
}
QStatusBar {
    background-color: %(sidebar_bg)s;
    border-top: 1px solid %(border)s;
    color: %(text_dim)s;
    font-size: 12px;
}
QStatusBar::item {
    border: none;
}
QProgressBar {
    background-color: %(surface_sunken)s;
    border: 1px solid %(border)s;
    border-radius: 6px;
    height: 10px;
    text-align: center;
    color: %(text_dim)s;
    font-size: 11px;
}
QProgressBar::chunk {
    background-color: %(accent)s;
    border-radius: 6px;
}
QMenu {
    background-color: %(surface)s;
    border: 1px solid %(border_strong)s;
    border-radius: 8px;
    padding: 5px;
}
QMenu::item {
    padding: 5px 19px 5px 12px;
    border-radius: 6px;
}
QMenu::item:selected {
    background-color: %(accent_soft)s;
}
QMenu::separator {
    height: 1px;
    background-color: %(border)s;
    margin: 4px 8px;
}
QTabWidget::pane {
    background-color: %(surface)s;
    border: 1px solid %(border)s;
    border-radius: 8px;
}
QTabBar::tab {
    background: transparent;
    color: %(text_dim)s;
    padding: 6px 13px;
    margin-right: 2px;
    border: 1px solid transparent;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
}
QTabBar::tab:selected {
    background-color: %(surface)s;
    color: %(text)s;
    border-color: %(border)s;
    border-bottom-color: %(surface)s;
}
QTabBar::tab:hover:!selected {
    color: %(text)s;
}
QGroupBox {
    background-color: %(surface)s;
    border: 1px solid %(border)s;
    border-radius: 8px;
    margin-top: 9px;
    padding: 11px 10px 9px 10px;
    font-weight: bold;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0px 6px;
    color: %(text_dim)s;
}
QSplitter::handle {
    background-color: %(border)s;
}
QSplitter::handle:horizontal {
    width: 1px;
}
QSplitter::handle:vertical {
    height: 1px;
}
QDialogButtonBox QPushButton {
    min-width: 84px;
}
"""

# Appended only in the dark theme, and only when the wallpaper file is present.
# Everything between the window and the content stops painting a background so
# the picture drawn by `MainWindow.paintEvent` is visible, and the panels that
# sit on top of it go translucent enough to feel like glass over the image while
# staying dark enough to read text on.
#
# NOTE the integer alphas: Qt's parser reads the fourth argument of rgba() as
# 0-255, so `rgba(12, 23, 33, 224)` is right and `rgba(12, 23, 33, 0.88)` would
# silently come out fully transparent (see the module docstring).
_QSS_WALLPAPER = """
/* ----------------------------------------------------------- wallpaper ---- */
QMainWindow, QStackedWidget, QWidget[screen="true"] {
    background: transparent;
}
QFrame[card="true"], QFrame#actionCard, QFrame#chip, QFrame#statusStrip,
QGroupBox {
    background-color: %(glass)s;
}
QFrame#actionCard:hover, QFrame#actionCard:focus {
    background-color: %(glass_lift)s;
}
/* The rail and the status bar frame the picture rather than sitting in it, so
   they are darker glass than the cards -- enough to read nav labels against a
   moonlit cloud. */
QWidget#sidebar, QStatusBar {
    background-color: %(glass_rail)s;
}
QFrame#quoteCard {
    background-color: %(glass_sunken)s;
}
QTableView, QTreeView, QListView {
    background-color: %(glass_solid)s;
}
QHeaderView::section {
    background-color: %(glass_header)s;
}
QFrame#dropZone {
    background-color: %(glass_sunken)s;
}
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QDateEdit,
QComboBox {
    background-color: %(glass_sunken)s;
}
/* Dialogs are their own windows with no wallpaper behind them, so they stay
   opaque -- a translucent dialog over the desktop would read as a glitch. */
QDialog {
    background-color: %(bg)s;
}
QDialog QFrame[card="true"], QDialog QGroupBox {
    background-color: %(surface)s;
}
"""


def _rgba(value: str, alpha: int) -> str:
    """``#rrggbb`` -> ``rgba(r, g, b, a)`` with `alpha` as Qt wants it: 0-255."""
    r, g, b = _rgb(value)
    return f"rgba({r}, {g}, {b}, {max(0, min(255, int(alpha)))})"


def _tile_rules(dark: bool, tokens: dict[str, Any]) -> str:
    """``QLabel#cardTile[tint="..."]`` rules, one per entry in `TINTS`.

    Generated rather than written out because the two colours are derived from
    one hue and the card surface underneath -- see `tile_colors`.
    """
    lines = []
    for name in TINTS:
        bg, fg = tile_colors(name, dark, str(tokens["surface"]))
        lines.append(
            f'QLabel#cardTile[tint="{name}"] '
            f"{{ background-color: {bg}; color: {fg}; }}"
        )
    # Last, so it beats the tint rules: a disabled card must not keep a live
    # colour on its icon. Equal specificity, so order is what decides it.
    lines.append(
        "QLabel#cardTile:disabled { "
        f"background-color: {tokens['disabled_bg']}; "
        f"color: {tokens['disabled_text']}; }}"
    )
    return (
        "\n/* ------------------------------------------------- icon tints ---- */\n"
        + "\n".join(lines)
        + "\n"
    )


def stylesheet(dark: bool = False) -> str:
    """The complete application style sheet for one theme."""
    tokens: dict[str, Any] = dict(palette(dark))
    tokens["font"] = _FONT_CSS
    tokens["glyph_font"] = _GLYPH_CSS
    tokens["base_px"] = BASE_FONT_PX
    # QSS url() wants forward slashes even on Windows, and an absolute path so
    # the sheet works whatever the process's working directory is.
    tokens["check_icon"] = (_ASSETS / "check.svg").as_posix()
    sheet = _QSS % tokens
    sheet += _tile_rules(dark, tokens)
    if dark and WALLPAPER_PATH.is_file():
        # Panels are glass; anything holding editable text is near-opaque, because
        # a cape edge running behind a line of option text is the one place the
        # picture actively costs the user something.
        tokens["glass"] = _rgba(tokens["surface"], 224)
        tokens["glass_lift"] = _rgba(tokens["surface_alt"], 238)
        tokens["glass_solid"] = _rgba(tokens["surface"], 244)
        tokens["glass_header"] = _rgba(tokens["header_bg"], 244)
        tokens["glass_sunken"] = _rgba(tokens["surface_sunken"], 242)
        tokens["glass_rail"] = _rgba(tokens["sidebar_bg"], 236)
        sheet += _QSS_WALLPAPER % tokens
    return sheet


# --------------------------------------------------------------------------- #
# Application-level wiring
# --------------------------------------------------------------------------- #

#: (QPalette role, token) pairs applied to the enabled colour group.
_PALETTE_ROLES: tuple[tuple[str, str], ...] = (
    ("Window", "bg"),
    ("WindowText", "text"),
    ("Base", "surface_sunken"),
    ("AlternateBase", "row_alt"),
    ("Text", "text"),
    ("PlaceholderText", "text_faint"),
    ("Button", "surface_alt"),
    ("ButtonText", "text"),
    ("BrightText", "red"),
    ("Light", "surface_alt"),
    ("Midlight", "border"),
    ("Mid", "border"),
    ("Dark", "bg_alt"),
    ("Shadow", "bg"),
    ("Highlight", "accent"),
    ("HighlightedText", "accent_fg"),
    ("ToolTipBase", "tooltip_bg"),
    ("ToolTipText", "tooltip_text"),
    ("Link", "accent"),
    ("LinkVisited", "brand_soft"),
)

#: Roles that must be dimmed for the Disabled colour group.
_DISABLED_ROLES: tuple[tuple[str, str], ...] = (
    ("WindowText", "disabled_text"),
    ("Text", "disabled_text"),
    ("ButtonText", "disabled_text"),
    ("Base", "disabled_bg"),
    ("Button", "disabled_bg"),
    ("Highlight", "disabled_bg"),
    ("HighlightedText", "disabled_text"),
)


def build_palette(dark: bool = False) -> QPalette:
    """A QPalette matching `stylesheet`, for the bits QSS cannot reach."""
    c = palette(dark)
    pal = QPalette()
    for role_name, token in _PALETTE_ROLES:
        role = getattr(QPalette.ColorRole, role_name, None)
        if role is not None:
            pal.setColor(role, QColor(c[token]))
    for role_name, token in _DISABLED_ROLES:
        role = getattr(QPalette.ColorRole, role_name, None)
        if role is not None:
            pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(c[token]))
    return pal


def apply_theme(app: QApplication | None = None, dark: bool = False) -> None:
    """Apply font, palette and style sheet to `app` (default: the running one).

    Fusion is forced first: the native Windows 11 style ignores several box-model
    properties, so the same QSS would render inconsistently between machines.
    """
    global _wallpaper_on

    target = app if app is not None else QApplication.instance()
    if target is None:
        _LOG.warning("apply_theme called with no QApplication; nothing to style")
        return

    # Wallpaper is a dark-theme-only feature; flipping this before the sheet is
    # built keeps `wallpaper_enabled()` and the style sheet from disagreeing.
    _wallpaper_on = bool(dark)
    _wallpaper_cache.clear()

    if "Fusion" in QStyleFactory.keys():
        target.setStyle(QStyleFactory.create("Fusion"))

    font = QFont()
    font.setFamilies(list(FONT_FAMILIES))
    font.setPointSizeF(10.0)  # QSS px sizes win; this is for native chrome only
    target.setFont(font)

    target.setPalette(build_palette(dark))
    target.setStyleSheet(stylesheet(dark))
    _LOG.debug("theme applied (dark=%s)", dark)


# --------------------------------------------------------------------------- #
# The app logo
# --------------------------------------------------------------------------- #


def app_logo(
    height: int = 48, *, square: bool = False, radius: int | None = None
) -> QPixmap:
    """The app logo as a pixmap `height` px tall, with rounded corners.

    `square` centre-crops first, which is what a header badge or a window icon
    wants: scaling a portrait into a square box would squash the face. Pass
    ``radius=0`` for hard corners.

    Returns a null pixmap when the asset is missing or unreadable, so every
    caller can simply check `isNull()` and fall back rather than handle an
    exception.
    """
    if height <= 0 or not APP_LOGO_PATH.is_file():
        return QPixmap()
    source = QPixmap(str(APP_LOGO_PATH))
    if source.isNull():
        _LOG.warning("app logo at %s could not be read", APP_LOGO_PATH)
        return QPixmap()

    if square:
        side = min(source.width(), source.height())
        source = source.copy(
            (source.width() - side) // 2, (source.height() - side) // 2, side, side
        )
    scaled = source.scaledToHeight(height, Qt.TransformationMode.SmoothTransformation)

    corner = height // 6 if radius is None else radius
    if corner <= 0:
        return scaled

    rounded = QPixmap(scaled.size())
    rounded.fill(Qt.GlobalColor.transparent)
    painter = QPainter(rounded)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(scaled.rect()), corner, corner)
        painter.setClipPath(clip)
        painter.drawPixmap(0, 0, scaled)
    finally:
        # An unfinished QPainter on a QPixmap warns on destruction and can leave
        # the pixmap unusable, so end it even if drawing raised.
        painter.end()
    return rounded


#: Set by `apply_theme`. Read by `wallpaper`, so a screen never has to work out
#: for itself whether the current theme wants a picture behind it.
_wallpaper_on = False

#: One entry, keyed by (width, height): cover-scaling a 2000x1124 JPEG costs a
#: few milliseconds, which is fine once per resize and far too much per repaint.
_wallpaper_cache: dict[tuple[int, int], QPixmap] = {}


def wallpaper_enabled() -> bool:
    """True when the active theme wants the wallpaper drawn behind the window."""
    return _wallpaper_on and WALLPAPER_PATH.is_file()


def wallpaper(width: int, height: int) -> QPixmap:
    """The wallpaper, cover-cropped to ``width x height`` and dimmed.

    Cover, not stretch: the image is scaled until it fills both axes and the
    overflow is cropped from the centre, so the composition survives any window
    shape. Returns a null pixmap in the light theme, which is how callers know to
    draw nothing.
    """
    if width <= 0 or height <= 0 or not wallpaper_enabled():
        return QPixmap()
    key = (int(width), int(height))
    cached = _wallpaper_cache.get(key)
    if cached is not None:
        return cached

    source = QPixmap(str(WALLPAPER_PATH))
    if source.isNull():
        _LOG.warning("wallpaper at %s could not be read", WALLPAPER_PATH)
        return QPixmap()

    scaled = source.scaled(
        width,
        height,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    out = QPixmap(width, height)
    painter = QPainter(out)
    try:
        painter.drawPixmap(
            -(scaled.width() - width) // 2, -(scaled.height() - height) // 2, scaled
        )
        scrim = QColor(0, 0, 0)
        scrim.setAlphaF(WALLPAPER_SCRIM)
        painter.fillRect(out.rect(), scrim)
    finally:
        painter.end()

    # A window gets resized a lot; keep the cache to the last few shapes.
    if len(_wallpaper_cache) > 6:
        _wallpaper_cache.clear()
    _wallpaper_cache[key] = out
    return out


def app_icon() -> QIcon:
    """The logo as a window/taskbar icon, at the sizes Windows asks for."""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pixmap = app_logo(size, square=True, radius=0)
        if not pixmap.isNull():
            icon.addPixmap(pixmap)
    return icon


def repolish(widget: QWidget) -> None:
    """Re-evaluate style sheet rules after a dynamic property changed.

    Qt caches the resolved rules per widget, so ``setProperty("variant", ...)``
    alone has no visible effect -- the style has to unpolish/polish the widget.
    """
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def set_variant(widget: QWidget, variant: str) -> None:
    """Tag `widget` with a QSS variant (``primary``/``ghost``/``link``/...)."""
    widget.setProperty("variant", variant)
    repolish(widget)
