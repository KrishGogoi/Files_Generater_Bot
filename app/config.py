"""Settings persistence for Krishna's Super Bot.

Everything the user can configure lives in one JSON file under
``%APPDATA%/KrishnasSuperBot``. Two constraints shape this module:

* ``load_settings()`` must never raise. The app has to launch when the file is
  missing, was half-written by a crash, was hand-edited into nonsense, or was
  written by a future version with extra keys. So: unknown keys are ignored and
  any field that fails to coerce silently falls back to its default.
* The nested export configs are the real dataclasses from ``app.models``, not
  dicts. ``json.load`` hands back plain dicts, so they are rehydrated field by
  field -- and ``PptxExportConfig`` colours must come back as ``tuple``, not
  ``list``, or python-pptx rejects them.

The API key is stored here as well. It is never logged, and the file is
restricted to the owner where the OS honours mode bits.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import MISSING, Field, asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, TypeVar

from app.models import BrandConfig, PptxExportConfig, WordExportConfig

_LOG = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Identity / defaults
# --------------------------------------------------------------------------- #

APP_NAME = "Krishna's Super Bot"
APP_VERSION = "1.0.0"

#: Folder name used under %APPDATA% (or ~/.config on a POSIX box).
APP_DIR_NAME = "KrishnasSuperBot"

#: Lets a test run point at a throwaway settings file instead of the real one.
ENV_SETTINGS_PATH = "KRISHNAS_SUPERBOT_SETTINGS"

DEFAULT_PROVIDER = "gemini"
#: Verified live 2026-07-25: the whole `gemini-2.5-*` family now returns
#: 404 "no longer available to new users", so it must not be a default. Of the
#: models a fresh free-tier key can reach, flash-lite scored identically to
#: 3.5-flash on real extraction (zero verbatim mismatches, correct handling of
#: numbered sub-items) at roughly 4x the speed -- 22.6s vs 91.9s on a
#: 60-question file -- which is why it leads.
DEFAULT_MODEL = "gemini-3.1-flash-lite"

#: Providers `app.parse.providers` knows how to build. "none" = offline/rules-only.
PROVIDERS: tuple[str, ...] = ("gemini", "openai", "anthropic", "groq", "none")

THEMES: tuple[str, ...] = ("light", "dark")
DEFAULT_THEME = "light"

#: A sane default model per provider, used when the stored model is blank.
DEFAULT_MODELS: dict[str, str] = {
    "gemini": DEFAULT_MODEL,
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-haiku-latest",
    "groq": "llama-3.3-70b-versatile",
    "none": "",
}

#: Env vars consulted by `resolve_api_key`, in order, per provider.
ENV_VARS: dict[str, tuple[str, ...]] = {
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "none": (),
}

#: Filename of the Cerebellum Academy brand logo that ships with the app -- the
#: one that goes on the deck's title and thank-you slides. It lives in
#: `app/ui/assets` alongside `app_logo.jpg`, which is a DIFFERENT logo (the
#: window and lock-screen icon). Same folder, different jobs; do not swap them.
LOGO_FILENAME = "Cerebellum_Logo.png"

#: Where to look for it, nearest first, always RELATIVE to this file. This used
#: to be one hard-coded absolute path, and renaming the project folder silently
#: cost every deck its logo -- the file was still there, the string was not. The
#: older locations are kept so an existing checkout does not lose its logo the
#: day it updates.
_LOGO_DIRS: tuple[Path, ...] = (
    Path(__file__).resolve().parent / "ui" / "assets",   # where it ships
    Path(__file__).resolve().parent.parent,              # KrishnasSuperBot/
    Path(__file__).resolve().parent.parent.parent,       # the folder holding it
)

#: Upper bound for `Settings.max_concurrency` -- the Gemini free tier rate-limits
#: hard, and more threads past this only buys 429s.
MAX_CONCURRENCY_LIMIT = 16

#: How many entries `recent_files` keeps.
RECENT_FILES_LIMIT = 20

_T = TypeVar("_T")


def default_logo_path() -> str:
    """Path to the bundled brand logo, or "" when it is not on this machine."""
    for folder in _LOGO_DIRS:
        try:
            candidate = folder / LOGO_FILENAME
            if candidate.is_file():
                return str(candidate)
        except OSError as exc:  # weird path / permission trouble: skip the logo
            _LOG.debug("logo probe failed for %s: %s", folder, exc)
    return ""


def _default_brand() -> BrandConfig:
    return BrandConfig(logo_path=default_logo_path())


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Settings:
    """Everything the user can change, persisted as one JSON object."""

    api_key: str = ""
    #: One of `PROVIDERS`. "none" disables AI entirely (rules-only parsing).
    provider: str = DEFAULT_PROVIDER
    model: str = DEFAULT_MODEL
    use_ai: bool = True
    #: Threads used for AI chunk calls; clamped to 1..MAX_CONCURRENCY_LIMIT.
    max_concurrency: int = 4
    brand: BrandConfig = field(default_factory=_default_brand)
    word: WordExportConfig = field(default_factory=WordExportConfig)
    pptx: PptxExportConfig = field(default_factory=PptxExportConfig)
    #: Where exports go; "" means "next to the input file".
    output_dir: str = ""
    recent_files: list[str] = field(default_factory=list)
    #: "light" or "dark". Light by default: this is a daytime desk tool, and the
    #: exports it produces are black-on-white, so a bright UI matches the work.
    theme: str = DEFAULT_THEME

    # -- convenience -------------------------------------------------------- #

    @property
    def ai_enabled(self) -> bool:
        """True when an AI pass should even be attempted (key checked later)."""
        return bool(self.use_ai) and self.provider.strip().lower() != "none"

    def remember_files(self, paths: list[str]) -> None:
        """Push `paths` onto the front of `recent_files`, newest first."""
        merged = [str(p) for p in paths if str(p).strip()]
        merged.extend(self.recent_files)
        self.recent_files = list(dict.fromkeys(merged))[:RECENT_FILES_LIMIT]


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #


def config_dir() -> Path:
    """The folder holding settings.json (created lazily by `save_settings`)."""
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME") or ""
    root = Path(base) if base.strip() else Path.home() / ".config"
    return root / APP_DIR_NAME


def settings_path() -> Path:
    """%APPDATA%/KrishnasSuperBot/settings.json, or the env override."""
    override = os.environ.get(ENV_SETTINGS_PATH, "").strip()
    if override:
        return Path(override).expanduser()
    return config_dir() / "settings.json"


# --------------------------------------------------------------------------- #
# JSON <-> dataclass round-tripping
# --------------------------------------------------------------------------- #


def _field_default(f: Field[Any]) -> Any:
    if f.default is not MISSING:
        return f.default
    if f.default_factory is not MISSING:  # type: ignore[misc]
        return f.default_factory()  # type: ignore[misc]
    return None


def _coerce(default: Any, raw: Any) -> Any:
    """Bend `raw` (straight out of JSON) into the shape of `default`.

    Driven by the *runtime default value* rather than the annotation, because
    ``from __future__ import annotations`` makes every annotation a string and
    the defaults are exactly as authoritative.
    """
    if is_dataclass(default) and not isinstance(default, type):
        if isinstance(raw, Mapping):
            return _rehydrate(type(default), raw)
        raise TypeError(f"expected an object, got {type(raw).__name__}")
    if isinstance(default, tuple):
        # RGB colour triples: python-pptx needs a tuple, JSON gives a list.
        if isinstance(raw, (list, tuple)):
            return tuple(raw)
        raise TypeError(f"expected a list, got {type(raw).__name__}")
    if isinstance(default, bool):
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        return bool(raw)
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    if isinstance(default, str):
        return raw if isinstance(raw, str) else str(raw)
    if isinstance(default, list):
        if isinstance(raw, (list, tuple)):
            return list(raw)
        raise TypeError(f"expected a list, got {type(raw).__name__}")
    return raw


def _rehydrate(cls: type[_T], data: Mapping[str, Any]) -> _T:
    """Build `cls` from a JSON dict, defaulting anything missing or malformed."""
    kwargs: dict[str, Any] = {}
    for f in fields(cls):  # type: ignore[arg-type]
        default = _field_default(f)
        if f.name not in data:
            kwargs[f.name] = default
            continue
        try:
            kwargs[f.name] = _coerce(default, data[f.name])
        except (TypeError, ValueError) as exc:
            # Never mention the value itself: this dict can hold the API key.
            _LOG.debug("settings: field %r rejected (%s); using default", f.name, exc)
            kwargs[f.name] = default
    return cls(**kwargs)


def _clamp_int(value: Any, low: int, high: int, fallback: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, n))


def _finalise(s: Settings) -> Settings:
    """Normalise a freshly-loaded `Settings` so the rest of the app can trust it."""
    s.api_key = str(s.api_key or "").strip()
    s.provider = str(s.provider or DEFAULT_PROVIDER).strip().lower()
    if s.provider not in PROVIDERS:
        _LOG.warning(
            "unknown provider %r in settings; falling back to %r",
            s.provider, DEFAULT_PROVIDER,
        )
        s.provider = DEFAULT_PROVIDER
    s.model = str(s.model or "").strip() or DEFAULT_MODELS.get(s.provider, DEFAULT_MODEL)
    s.use_ai = bool(s.use_ai)
    s.max_concurrency = _clamp_int(s.max_concurrency, 1, MAX_CONCURRENCY_LIMIT, 4)
    s.theme = str(getattr(s, "theme", "") or "").strip().lower()
    if s.theme not in THEMES:
        s.theme = DEFAULT_THEME
    s.output_dir = str(s.output_dir or "").strip()
    s.recent_files = [
        p for p in dict.fromkeys(str(p) for p in s.recent_files) if p.strip()
    ][:RECENT_FILES_LIMIT]
    s.brand.logo_path = _resolve_logo(str(s.brand.logo_path or "").strip())
    return s


def _resolve_logo(stored: str) -> str:
    """The logo path to actually use, healing a house logo that has moved.

    Three cases:

    * nothing stored -- a brand-new profile still gets the house logo;
    * the stored path resolves -- leave it exactly as it is;
    * it does not resolve. If it is the *house* logo, the project folder was
      renamed or moved, so re-point it: the alternative is what happened before,
      which is every title and thank-you slide quietly losing its logo while the
      file sat there the whole time. A *custom* logo is left alone -- it may be
      on a drive that is merely offline right now, and silently replacing
      someone's own logo with ours would be worse than skipping it this once.
    """
    if not stored:
        return default_logo_path()
    try:
        if Path(stored).expanduser().is_file():
            return stored
    except OSError as exc:
        _LOG.debug("logo probe failed for %s: %s", stored, exc)
        return stored
    if Path(stored).name.lower() != LOGO_FILENAME.lower():
        _LOG.warning("brand logo %s is not reachable; slides will have no logo", stored)
        return stored
    found = default_logo_path()
    if found:
        _LOG.info("house logo moved: %s -> %s", stored, found)
    else:
        _LOG.warning("house logo %s no longer exists and no copy was found", stored)
    return found


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def load_settings() -> Settings:
    """Read settings.json. Never raises -- any problem yields defaults."""
    path = settings_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        _LOG.debug("no settings file at %s; using defaults", path)
        return _finalise(Settings())
    except (OSError, UnicodeDecodeError) as exc:
        _LOG.warning("could not read %s (%s); using defaults", path, exc)
        return _finalise(Settings())

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        _LOG.warning("settings file is corrupt (%s); using defaults", exc)
        return _finalise(Settings())

    if not isinstance(data, Mapping):
        _LOG.warning("settings file is not a JSON object; using defaults")
        return _finalise(Settings())

    try:
        s = _rehydrate(Settings, data)
    except Exception as exc:  # noqa: BLE001 - the contract is "never raises"
        _LOG.warning("settings could not be applied (%s); using defaults", exc)
        s = Settings()
    return _finalise(s)


def save_settings(s: Settings) -> None:
    """Write settings.json atomically. Logs and returns on failure."""
    path = settings_path()
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(s), indent=2, ensure_ascii=False)
        # Temp file + replace: a crash mid-write must not destroy a good file.
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError) as exc:
        _LOG.error("could not save settings to %s: %s", path, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return
    _harden(path)


def _harden(path: Path) -> None:
    """Owner-only permissions where the OS honours mode bits.

    On Windows this is close to a no-op (ACLs rule there), which is why the
    failure is only logged at debug level.
    """
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        _LOG.debug("could not tighten permissions on %s: %s", path, exc)


def resolve_api_key(s: Settings) -> str:
    """The key to use: the stored one, else the provider's env var. "" if none."""
    stored = str(s.api_key or "").strip()
    if stored:
        return stored
    provider = str(s.provider or "").strip().lower()
    for var in ENV_VARS.get(provider, ()):
        value = os.environ.get(var, "").strip()
        if value:
            _LOG.debug("using API key from %s", var)  # name only, never the value
            return value
    return ""


def has_api_key(s: Settings) -> bool:
    """Convenience for the UI banner: is any usable key configured?"""
    return bool(resolve_api_key(s))
