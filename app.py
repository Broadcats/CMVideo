"""CMVideo - drag-and-drop / URL profanity censor and downloader."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from censor.version import APP_VERSION


def _bootstrap_bundle_paths() -> None:
    """Prepend bundled tool dirs to PATH when running a PyInstaller build.

    Linux AppImage drops ffmpeg/ffprobe/espeak next to the real executable;
    Windows one-file extracts them under ``sys._MEIPASS``. ``shutil.which``
    only sees what's on PATH, so wire that up before any worker imports
    modules that shell out.
    """
    if not getattr(sys, "frozen", False):
        return
    roots: list[Path] = []
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        roots.append(Path(mei))
    roots.append(Path(sys.executable).resolve().parent)
    seen: set[Path] = set()
    prefix: list[str] = []
    for r in roots:
        r = r.resolve()
        if r in seen:
            continue
        seen.add(r)
        prefix.append(str(r))
    os.environ["PATH"] = os.pathsep.join(prefix + [os.environ.get("PATH", "")])
    for r in roots:
        data = r / "espeak-ng-data"
        if data.is_dir():
            os.environ.setdefault("ESPEAK_DATA_PATH", str(data))
            break


_bootstrap_bundle_paths()

if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    HERE = Path(sys._MEIPASS)
else:
    HERE = Path(__file__).resolve().parent


import queue
import subprocess
import threading
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from tkinter import filedialog, messagebox, ttk

# CustomTkinter is the website-matched dark/rounded UI toolkit. It's a
# soft dependency: when missing we silently fall back to the legacy
# ttk-only widgets so the app still launches in editor / dev shells
# that haven't installed it yet.
try:
    import customtkinter as ctk  # type: ignore[import-not-found]
    _CTK_AVAILABLE = True
except Exception:  # noqa: BLE001
    ctk = None  # type: ignore[assignment]
    _CTK_AVAILABLE = False

from censor import cancel as censor_cancel, funtts, pipeline
from censor.audio import SUPPORTED_INPUT_EXTS
from censor.config_store import load_config, save_config
from censor.download import (
    AUDIO_QUALITY_LABELS,
    SUPPORTED_DOWNLOAD_FORMATS,
    VIDEO_QUALITY_LABELS,
    default_download_dir,
    is_url,
)
from censor.plugins import ensure_user_plugin_dir

# tkinterdnd2 is optional - if missing we still work via the Browse button.
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _DND_AVAILABLE = True
except Exception:  # noqa: BLE001
    TkinterDnD = None  # type: ignore[assignment]
    DND_FILES = None  # type: ignore[assignment]
    _DND_AVAILABLE = False


APP_TITLE = "CMVideo"           # window / taskbar title (short)
APP_BRAND = "Clean My Video"    # full wordmark shown inside the UI
APP_TAGLINE = "Automatic profanity removal for video and audio"
COPYRIGHT = f"\u00a9 Daniel Brown 2026 \u00b7 v{APP_VERSION}"

# Downsize preset labels shown in the UI -> pipeline keys.
_DOWNSIZE_CHOICES: tuple[tuple[str, str], ...] = (
    ("Original", "none"),
    ("Small", "small"),
    ("Medium", "medium"),
    ("Large", "large"),
)

# Whitelist of recognised faster-whisper model sizes. Defence in depth:
# a hand-edited `whisper_model_size` in config.json could otherwise be
# fed straight to `WhisperModel(...)` which also accepts arbitrary local
# paths. We accept only known-good named sizes; anything else falls
# back to "small".
_VALID_WHISPER_SIZES: frozenset[str] = frozenset({
    "tiny", "tiny.en",
    "base", "base.en",
    "small", "small.en",
    "medium", "medium.en",
    "large", "large-v1", "large-v2", "large-v3",
    "distil-small.en", "distil-medium.en",
    "distil-large-v2", "distil-large-v3",
})


def _safe_whisper_model_size(value: object) -> str:
    if isinstance(value, str) and value in _VALID_WHISPER_SIZES:
        return value
    return "small"

SUPPORTED_FILETYPES = [
    (
        "All supported",
        "*.mp4 *.mov *.mkv *.webm *.avi *.flv "
        "*.mp3 *.m4a *.aac *.ogg *.opus *.wav *.flac",
    ),
    ("Video (MP4, MOV, MKV, WebM, AVI, FLV)", "*.mp4 *.mov *.mkv *.webm *.avi *.flv"),
    (
        "Audio (MP3, M4A/AAC, OGG, Opus, WAV, FLAC)",
        "*.mp3 *.m4a *.aac *.ogg *.opus *.wav *.flac",
    ),
    ("All files", "*.*"),
]


# Cool-shade dark palette. Indigo body, violet accents, cyan side
# notes; tuned so the cards read as raised against a slightly bluer
# background.
class Theme:
    BG = "#0d1018"           # window background
    BG_DEEP = "#090b12"      # darker fall-back / footer
    SURFACE = "#171a26"      # card / surface
    SURFACE_HI = "#222637"   # hover / drop-zone
    SURFACE_DEEP = "#0e1119" # inset wells
    BORDER = "#262a3b"
    BORDER_HI = "#3a4159"
    BORDER_GLOW = "#3b3a8a"  # accent border / focus halo

    ACCENT = "#6366f1"       # indigo-500 (primary)
    ACCENT_HI = "#818cf8"    # indigo-400 (hover)
    ACCENT_LO = "#4f46e5"    # indigo-600 (pressed)
    ACCENT_GLOW = "#a78bfa"  # violet-400 (highlight halo)
    COOL = "#22d3ee"         # cyan-400 (secondary accent)
    COOL_LO = "#06b6d4"      # cyan-500

    TEXT = "#eef0f7"
    TEXT_MUTED = "#9aa0b4"
    TEXT_DIM = "#5b6076"

    SUCCESS = "#34d399"
    DANGER = "#f87171"


def _short(text: str, n: int) -> str:
    """Compact a long string for label display."""
    text = text.strip()
    return text if len(text) <= n else (text[: n - 1] + "\u2026")


# Browser-extension store URLs for exporting cookies.txt. Updated 2026-05;
# both extensions have been around for years and are the de-facto picks.
COOKIES_EXTENSION_LINKS: tuple[tuple[str, str], ...] = (
    (
        "Chrome / Edge / Brave - 'Get cookies.txt LOCALLY'",
        "https://chromewebstore.google.com/detail/get-cookiestxt-locally/"
        "cclelndahbckbenkjhflpdbgdldlbecc",
    ),
    (
        "Firefox - 'cookies.txt'",
        "https://addons.mozilla.org/en-US/firefox/addon/cookies-txt/",
    ),
)


# Config dir + load/save helpers live in censor.config_store so the
# `censor.plugins` module can find the same on-disk location without
# importing the GUI. Imported above.


def _parse_drop_data(data: str) -> list[str]:
    """Parse tkinterdnd2 drop event data into a list of filesystem paths.

    The Tk DnD payload separates paths with whitespace, wrapping any
    path that contains spaces in `{...}`. Multi-file drops on Linux
    look like `{/a/b c.mp4} /d/e.mp3 {/f/g.wav}`.
    """
    if not data:
        return []
    paths: list[str] = []
    i = 0
    n = len(data)
    while i < n:
        if data[i].isspace():
            i += 1
            continue
        if data[i] == "{":
            end = data.find("}", i + 1)
            if end == -1:
                break
            paths.append(data[i + 1 : end])
            i = end + 1
        else:
            j = i
            while j < n and not data[j].isspace():
                j += 1
            paths.append(data[i:j])
            i = j
    return paths


def _pick_font(families: list[str], size: int, weight: str = "normal") -> tkfont.Font:
    """Return a tkinter Font using the first family that's installed."""
    available = set(tkfont.families())
    for fam in families:
        if fam in available:
            return tkfont.Font(family=fam, size=size, weight=weight)
    return tkfont.Font(size=size, weight=weight)


if _CTK_AVAILABLE:
    # Apply website-matched palette + dark mode globally before any CTk
    # widget gets instantiated.
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")  # we override via fg_color/hover_color


def _ctk_font(f):  # type: ignore[no-untyped-def]
    """Convert a tk Font to a CTk-friendly tuple. Pass-through if already
    tuple/None/CTkFont. CTk rejects tkinter.font.Font instances, so we
    extract `family`, `size`, `weight` and hand them across as a tuple."""
    if f is None:
        return None
    if isinstance(f, tuple):
        return f
    try:
        family = f.cget("family")
        size = int(f.cget("size"))
        weight = f.cget("weight") or "normal"
        return (family, size, weight)
    except Exception:  # noqa: BLE001
        return None


# --- CTk widget shims --------------------------------------------------
# CustomTkinter widgets don't share the ttk API. These tiny helpers let
# the rest of CensorApp stay agnostic: they emit a CTk widget when CTk
# is present (smooth dark dropdown, rounded checkbox, progress bar with
# a soft bar, etc.) and a ttk widget otherwise.

class _CTkComboBoxShim:
    """Wraps `ctk.CTkComboBox` to look like `ttk.Combobox` to the rest
    of the app: supports `.bind('<<ComboboxSelected>>', cb)` and
    `.current(idx)`, plus the usual `set/get/configure`."""

    def __init__(self, parent, *, textvariable=None, values, state="readonly",
                 width=12, font=None):  # type: ignore[no-untyped-def]
        self._values: list[str] = list(values)
        self._cb: list = []
        self._var = textvariable
        char_w = 8  # approx average px per char at 11pt Inter
        ctk_kwargs = dict(
            values=self._values,
            state=state,
            width=max(60, width * char_w + 28),
            height=30,
            corner_radius=8,
            border_width=1,
            border_color=Theme.BORDER,
            fg_color=Theme.SURFACE,
            button_color=Theme.SURFACE_HI,
            button_hover_color=Theme.ACCENT_LO,
            text_color=Theme.TEXT,
            dropdown_fg_color=Theme.SURFACE,
            dropdown_hover_color=Theme.ACCENT,
            dropdown_text_color=Theme.TEXT,
            font=_ctk_font(font),
            command=self._on_command,
        )
        if textvariable is not None:
            ctk_kwargs["variable"] = textvariable
        self._widget = ctk.CTkComboBox(parent, **ctk_kwargs)  # type: ignore[union-attr]

    def _on_command(self, choice: str) -> None:
        for cb in self._cb:
            try:
                cb(None)
            except Exception:  # noqa: BLE001
                pass

    # ttk-compatible surface ------------------------------------------------
    def bind(self, event: str, callback) -> None:  # type: ignore[no-untyped-def]
        if event == "<<ComboboxSelected>>":
            self._cb.append(callback)
            return
        self._widget.bind(event, callback)

    def current(self, idx: int | None = None):  # type: ignore[no-untyped-def]
        if idx is None:
            try:
                return self._values.index(self._widget.get())
            except ValueError:
                return -1
        if 0 <= idx < len(self._values):
            self._widget.set(self._values[idx])

    def set(self, value: str) -> None:
        self._widget.set(value)

    def get(self) -> str:
        return self._widget.get()

    def pack(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return self._widget.pack(*args, **kwargs)

    def grid(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return self._widget.grid(*args, **kwargs)

    def configure(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        if "values" in kwargs:
            self._values = list(kwargs["values"])
        self._widget.configure(**kwargs)

    def state(self, _states):  # type: ignore[no-untyped-def]
        # ttk.state(["disabled"]) / state(["!disabled"]) compatibility
        for s in _states:
            if s == "disabled":
                self._widget.configure(state="disabled")
            elif s in ("!disabled", "readonly"):
                self._widget.configure(state="readonly")


def _make_combobox(parent, *, textvariable=None, values, state="readonly",
                   width=12, font=None):  # type: ignore[no-untyped-def]
    if _CTK_AVAILABLE:
        return _CTkComboBoxShim(
            parent,
            textvariable=textvariable,
            values=values,
            state=state,
            width=width,
            font=font,
        )
    kwargs = dict(
        values=list(values),
        state=state,
        width=width,
        style="Dark.TCombobox",
    )
    if textvariable is not None:
        kwargs["textvariable"] = textvariable
    return ttk.Combobox(parent, **kwargs)


def _make_check(parent, *, text, variable, font=None):  # type: ignore[no-untyped-def]
    if _CTK_AVAILABLE:
        return ctk.CTkCheckBox(  # type: ignore[union-attr]
            parent,
            text=text,
            variable=variable,
            fg_color=Theme.ACCENT,
            hover_color=Theme.ACCENT_LO,
            border_color=Theme.BORDER_HI,
            text_color=Theme.TEXT,
            checkmark_color="#ffffff",
            corner_radius=4,
            border_width=2,
            font=_ctk_font(font),
        )
    return ttk.Checkbutton(
        parent,
        text=text,
        variable=variable,
        style="Dark.TCheckbutton",
    )


def _make_radio(parent, *, text, variable, value, font=None):  # type: ignore[no-untyped-def]
    if _CTK_AVAILABLE:
        return ctk.CTkRadioButton(  # type: ignore[union-attr]
            parent,
            text=text,
            variable=variable,
            value=value,
            fg_color=Theme.ACCENT,
            hover_color=Theme.ACCENT_LO,
            border_color=Theme.BORDER_HI,
            text_color=Theme.TEXT,
            font=_ctk_font(font),
        )
    return ttk.Radiobutton(
        parent,
        text=text,
        variable=variable,
        value=value,
        style="Dark.TRadiobutton",
    )


def _make_progress(parent, *, length=400):  # type: ignore[no-untyped-def]
    if _CTK_AVAILABLE:
        bar = ctk.CTkProgressBar(  # type: ignore[union-attr]
            parent,
            mode="determinate",
            height=10,
            corner_radius=6,
            width=length,
            fg_color=Theme.SURFACE,
            progress_color=Theme.ACCENT,
        )
        bar.set(0)
        # ttk.Progressbar exposes ["value"] and configure(value=...).
        # Provide a tiny shim so the rest of the app doesn't care.

        class _BarShim:
            def __init__(self, w):  # type: ignore[no-untyped-def]
                self._w = w

            def __setitem__(self, k, v):  # type: ignore[no-untyped-def]
                if k in ("value", "amount"):
                    try:
                        v = float(v) / 100.0
                    except (TypeError, ValueError):
                        v = 0.0
                    self._w.set(max(0.0, min(1.0, v)))

            def pack(self, *a, **kw):  # type: ignore[no-untyped-def]
                return self._w.pack(*a, **kw)

            def grid(self, *a, **kw):  # type: ignore[no-untyped-def]
                return self._w.grid(*a, **kw)

            def configure(self, **kw):  # type: ignore[no-untyped-def]
                if "value" in kw:
                    self.__setitem__("value", kw.pop("value"))
                if kw:
                    self._w.configure(**kw)

            def winfo_exists(self):  # type: ignore[no-untyped-def]
                return self._w.winfo_exists()

        return _BarShim(bar)
    return ttk.Progressbar(
        parent, mode="determinate", length=length
    )


# Compose CTk and tkinterdnd2 by combining their root classes. The
# DnDWrapper just monkey-patches the root with `tk_dnd_version` and a
# `drop_target_register` method, so the order doesn't matter as long as
# we bring up CTk first and then ask tkinterdnd2 to bind into it.
if _CTK_AVAILABLE and _DND_AVAILABLE:
    class _CTkDnDRoot(ctk.CTk, TkinterDnD.DnDWrapper):  # type: ignore[misc]
        """CustomTkinter root that also speaks tkinterdnd2 protocol."""

        def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            ctk.CTk.__init__(self, *args, **kwargs)
            self.TkdndVersion = TkinterDnD._require(self)


class CensorApp:
    def __init__(self) -> None:
        if _CTK_AVAILABLE and _DND_AVAILABLE:
            self.root = _CTkDnDRoot()  # type: ignore[assignment]
        elif _CTK_AVAILABLE:
            self.root = ctk.CTk()  # type: ignore[assignment]
        elif _DND_AVAILABLE:
            self.root: tk.Tk = TkinterDnD.Tk()  # type: ignore[assignment]
        else:
            self.root = tk.Tk()

        self.root.title(f"{APP_TITLE} {APP_VERSION}")
        self.root.geometry("560x720")
        # Tightened to the actual required height in _fit_minsize() once
        # the UI has been built.
        self.root.minsize(420, 520)
        self.root.configure(bg=Theme.BG)

        self._set_window_icon()

        self._input_paths: list[Path] = []
        self._last_output: Path | None = None
        self._worker: threading.Thread | None = None
        # Cancel token for the current pipeline run. The Cancel button
        # sets it; pipeline.run() observes it at stage boundaries +
        # kills any registered ffmpeg/ffprobe subprocess so a long
        # encode dies in <1 s instead of waiting for the whole pass.
        self._cancel_token: censor_cancel.CancelToken | None = None
        self._events: queue.Queue = queue.Queue()
        # after-id for the event drain, kept so a starting worker can
        # bump the next tick forward to _ACTIVE_DRAIN_MS.
        self._drain_after_id: str | None = None

        self.remove_swears = tk.BooleanVar(value=True)
        self.remove_slurs = tk.BooleanVar(value=True)
        self.mode = tk.StringVar(value="silence")
        self.mode.trace_add("write", lambda *_a: self._refresh_fun_voice_row())
        self.save_transcript = tk.BooleanVar(value=False)
        self.fuzzy_matching = tk.BooleanVar(value=False)
        self.url_var = tk.StringVar(value="")
        self.download_format_var = tk.StringVar(value="mp4")
        # Possible values for `quality_var` depend on the active Format.
        # _last_video_quality / _last_audio_quality remember the picks
        # per kind so Format swaps don't lose them.
        self.quality_var = tk.StringVar(value="Best")
        self._last_video_quality = "Best"
        self._last_audio_quality = "192 kbps"
        self._quality_kind = "video"  # "video" / "audio" / "wav"

        # Output folder. None = auto (Downloads for URL jobs, the input
        # file's folder for local jobs).
        self._output_dir: Path | None = None
        self.save_dir_display_var = tk.StringVar(value="")

        # Netscape-format cookies file for yt-dlp, set via the URL
        # field's right-click menu. Persisted on disk when
        # `remember_cookies` is on, rehydrated on next launch.
        self._cookies_file: Path | None = None
        self.cookies_display_var = tk.StringVar(value="")
        self.remember_cookies = tk.BooleanVar(value=False)
        # Guard: setting `remember_cookies` from config-load must not
        # trigger an immediate save back to disk.
        self._suppress_remember_trace = False
        self._config = load_config()

        # Fun voice + output extras (persisted on each job start).
        _fv = self._config.get("fun_voice")
        if not isinstance(_fv, str) or _fv not in funtts.fun_voice_ids():
            _fv = funtts.DEFAULT_FUN_VOICE_ID
        self._fun_voice_id = _fv

        # ElevenLabs livestream voices stay disabled until the user
        # supplies an API key via the URL right-click menu. Stored in
        # config.json (file mode 0o600 on POSIX) so the key isn't
        # leaked into command-line history or env exports.
        _ek = self._config.get("elevenlabs_api_key") or ""
        self._elevenlabs_api_key: str = (
            _ek if isinstance(_ek, str) else ""
        ).strip()

        _dsp = self._config.get("downsize_preset") or "none"
        if _dsp not in ("none", "small", "medium", "large"):
            _dsp = "none"
        self.downsize_label_var = tk.StringVar(
            value=next((lab for lab, k in _DOWNSIZE_CHOICES if k == _dsp), "Original")
        )
        self.retro_audio_var = tk.BooleanVar(value=bool(self._config.get("retro_audio")))

        self.status_var = tk.StringVar(
            value="Drop a file or paste a URL to get started."
        )

        # Re-evaluate button label + replace-with availability on every toggle.
        for var in (self.remove_swears, self.remove_slurs, self.save_transcript):
            var.trace_add("write", lambda *_a: self._on_options_changed())
        self.url_var.trace_add("write", lambda *_a: self._on_url_changed())
        self.download_format_var.trace_add(
            "write", lambda *_a: self._refresh_quality_combo()
        )
        self.remember_cookies.trace_add(
            "write", lambda *_a: self._on_remember_cookies_toggled()
        )

        self._init_fonts()
        self._init_styles()
        self._build_ui()
        self._on_options_changed()
        self._refresh_format_combo()
        self._refresh_quality_combo()
        self._refresh_save_dir_display()
        # Must run after _build_ui so the cookies strip exists.
        self._restore_cookies_from_config()
        self._fit_minsize()

        # Pre-warm the Whisper model in a daemon thread so the first
        # censor job doesn't pay the 1-2 second model-load cost on
        # the user's wall clock. Failures are silently ignored - we
        # fall back to lazy loading on the first real job. Started
        # AFTER the UI is fully constructed so the prewarm doesn't
        # contend with widget creation for the disk / CPU.
        self._url_debounce_after: str | None = None
        self.root.after(200, self._kick_off_prewarm)

        # Event drain. Adaptive interval (see _drain_events): fast while
        # a worker runs, slow when idle.
        self._drain_after_id = self.root.after(
            self._IDLE_DRAIN_MS, self._drain_events
        )

    # Event-drain interval (ms) when a worker is running. Fast enough to
    # keep the progress bar smooth.
    _ACTIVE_DRAIN_MS = 100
    # Event-drain interval (ms) when nothing is happening. ~1 wakeup/sec
    # is plenty since there's no work queued.
    _IDLE_DRAIN_MS = 1000

    def _kick_off_prewarm(self) -> None:
        """Background-load the Whisper model so the first censor job
        is instant. Runs in a daemon thread because faster-whisper's
        init does ~1.5s of disk + ctranslate2 work that would
        otherwise block the Tk event loop. Failures are non-fatal:
        if pre-warm fails the first job will pay the load cost
        normally."""
        try:
            from censor import transcribe  # type: ignore[import-not-found]
        except Exception:  # noqa: BLE001
            return
        # Match the default model size used by the pipeline. If the
        # user has overridden this in config we read it back here so
        # we pre-warm the right one.
        try:
            cfg_model = _safe_whisper_model_size(
                (self._config or {}).get("whisper_model_size")
            )
        except Exception:  # noqa: BLE001
            cfg_model = "small"
        threading.Thread(
            target=transcribe.prewarm,
            kwargs={"model_size": cfg_model, "device": "cpu", "compute_type": "int8"},
            name="whisper-prewarm",
            daemon=True,
        ).start()

    def _fit_minsize(self) -> None:
        """Set the window minsize to the natural required size of the UI,
        so the user can't shrink the window below the point where any of
        the option checkboxes/radios become hidden."""
        self.root.update_idletasks()
        req_w = self.root.winfo_reqwidth()
        req_h = self.root.winfo_reqheight()
        # Pad a touch so borders don't sit flush against widgets.
        self.root.minsize(max(420, req_w + 8), max(520, req_h + 8))

    # ---------- Window icon ----------

    def _set_window_icon(self) -> None:
        """Set the title-bar/taskbar icon. We keep references to prevent GC."""
        self._icons: list[tk.PhotoImage] = []
        for name in ("icon.png", "icon-128.png", "icon-64.png", "icon-32.png"):
            path = HERE / name
            if path.exists():
                try:
                    self._icons.append(tk.PhotoImage(file=str(path)))
                except tk.TclError:
                    pass
        if self._icons:
            try:
                self.root.iconphoto(True, *self._icons)
            except tk.TclError:
                pass

    # ---------- Fonts & styles ----------

    def _init_fonts(self) -> None:
        sans = ["Inter", "SF Pro Display", "Segoe UI", "Ubuntu", "Cantarell", "DejaVu Sans", "Helvetica"]
        self.font_title = _pick_font(sans, 22, "bold")
        self.font_tagline = _pick_font(sans, 12)
        # Body / section / status / drop-sub all bumped a touch so the
        # CTk widgets (taller than the old ttk widgets) don't dwarf the
        # text inside them.
        self.font_body = _pick_font(sans, 12)
        self.font_section = _pick_font(sans, 11, "bold")
        self.font_button = _pick_font(sans, 14, "bold")
        self.font_status = _pick_font(sans, 11)
        self.font_footer = _pick_font(sans, 10)
        self.font_drop = _pick_font(sans, 14, "bold")
        self.font_drop_sub = _pick_font(sans, 11)

    def _init_styles(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        # Frame backgrounds
        style.configure("TFrame", background=Theme.BG)
        style.configure("Card.TFrame", background=Theme.SURFACE)
        style.configure("TLabel", background=Theme.BG, foreground=Theme.TEXT)
        style.configure(
            "Title.TLabel",
            background=Theme.BG,
            foreground=Theme.TEXT,
            font=self.font_title,
        )
        style.configure(
            "Tagline.TLabel",
            background=Theme.BG,
            foreground=Theme.TEXT_MUTED,
            font=self.font_tagline,
        )
        style.configure(
            "Section.TLabel",
            background=Theme.SURFACE,
            foreground=Theme.TEXT_MUTED,
            font=self.font_section,
        )
        style.configure(
            "SectionDim.TLabel",
            background=Theme.SURFACE,
            foreground=Theme.TEXT_DIM,
            font=self.font_section,
        )
        style.configure(
            "Status.TLabel",
            background=Theme.BG,
            foreground=Theme.TEXT_MUTED,
            font=self.font_status,
        )
        style.configure(
            "Footer.TLabel",
            background=Theme.BG,
            foreground=Theme.TEXT_DIM,
            font=self.font_footer,
        )
        style.configure(
            "OptionLabel.TLabel",
            background=Theme.SURFACE,
            foreground=Theme.TEXT,
            font=self.font_body,
        )
        style.configure(
            "OptionLabelDim.TLabel",
            background=Theme.SURFACE,
            foreground=Theme.TEXT_MUTED,
            font=self.font_body,
        )

        # Checkbutton
        style.configure(
            "Dark.TCheckbutton",
            background=Theme.SURFACE,
            foreground=Theme.TEXT,
            focuscolor=Theme.SURFACE,
            indicatorbackground=Theme.SURFACE_HI,
            indicatorforeground=Theme.ACCENT,
            bordercolor=Theme.BORDER,
            lightcolor=Theme.SURFACE_HI,
            darkcolor=Theme.SURFACE_HI,
            font=self.font_body,
            padding=4,
        )
        style.map(
            "Dark.TCheckbutton",
            background=[("active", Theme.SURFACE)],
            foreground=[("active", Theme.TEXT)],
            indicatorbackground=[
                ("selected", Theme.ACCENT),
                ("!selected", Theme.SURFACE_HI),
            ],
            indicatorforeground=[
                ("selected", Theme.ACCENT),
                ("!selected", Theme.SURFACE_HI),
            ],
        )

        # Radiobutton
        style.configure(
            "Dark.TRadiobutton",
            background=Theme.SURFACE,
            foreground=Theme.TEXT,
            focuscolor=Theme.SURFACE,
            indicatorbackground=Theme.SURFACE_HI,
            indicatorforeground=Theme.ACCENT,
            bordercolor=Theme.BORDER,
            lightcolor=Theme.SURFACE_HI,
            darkcolor=Theme.SURFACE_HI,
            font=self.font_body,
            padding=4,
        )
        style.map(
            "Dark.TRadiobutton",
            background=[("active", Theme.SURFACE)],
            foreground=[
                ("disabled", Theme.TEXT_DIM),
                ("active", Theme.TEXT),
            ],
            indicatorbackground=[
                ("disabled", Theme.SURFACE),
                ("selected", Theme.ACCENT),
                ("!selected", Theme.SURFACE_HI),
            ],
            indicatorforeground=[
                ("disabled", Theme.SURFACE),
                ("selected", Theme.ACCENT),
                ("!selected", Theme.SURFACE_HI),
            ],
        )

        # Combobox (download-format picker)
        style.configure(
            "Dark.TCombobox",
            foreground=Theme.TEXT,
            fieldbackground=Theme.SURFACE,
            background=Theme.SURFACE,
            bordercolor=Theme.BORDER,
            lightcolor=Theme.SURFACE,
            darkcolor=Theme.SURFACE,
            arrowcolor=Theme.TEXT_MUTED,
            selectbackground=Theme.ACCENT,
            selectforeground="white",
            padding=4,
        )
        style.map(
            "Dark.TCombobox",
            fieldbackground=[("readonly", Theme.SURFACE), ("disabled", Theme.SURFACE)],
            foreground=[("readonly", Theme.TEXT), ("disabled", Theme.TEXT_DIM)],
            arrowcolor=[("disabled", Theme.TEXT_DIM)],
        )
        # The dropdown list popup uses option_add (not ttk).
        self.root.option_add("*TCombobox*Listbox.background", Theme.SURFACE)
        self.root.option_add("*TCombobox*Listbox.foreground", Theme.TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", Theme.ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "white")
        self.root.option_add("*TCombobox*Listbox.borderWidth", 0)

        # Progressbar - violet bar against an inset trough for depth.
        style.configure(
            "Dark.Horizontal.TProgressbar",
            background=Theme.ACCENT_GLOW,
            troughcolor=Theme.SURFACE_DEEP,
            bordercolor=Theme.BORDER,
            lightcolor=Theme.ACCENT_HI,
            darkcolor=Theme.ACCENT_LO,
            thickness=10,
        )

    # ---------- UI construction ----------

    def _build_ui(self) -> None:
        outer = tk.Frame(self.root, bg=Theme.BG)
        outer.pack(fill="both", expand=True, padx=14, pady=10)

        # Pack bottom-anchored widgets first so they survive a shrunk
        # window - only the options card in the middle gets clipped.
        self._build_footer(outer)
        self._build_action_area(outer)

        self._build_header(outer)
        self._build_drop_zone(outer)
        self._build_url_row(outer)
        self._build_save_dir_row(outer)
        self._build_options(outer)

        self._install_context_menus()
        self.root.bind("<Configure>", self._on_resize)

    def _build_header(self, parent: tk.Frame) -> None:
        header = tk.Frame(parent, bg=Theme.BG)
        header.pack(side="top", fill="x", pady=(0, 8))

        # Invisible top-right click target. 69 hits reveals an image.
        self._egg_clicks = 0
        self._egg_window = None
        self.egg_zone = tk.Frame(
            header,
            bg=Theme.BG,
            width=28,
            height=28,
            cursor="arrow",
        )
        self.egg_zone.pack(side="right", anchor="ne")
        self.egg_zone.pack_propagate(False)
        self.egg_zone.bind("<Button-1>", self._on_easter_egg_click)

        title_holder = tk.Frame(header, bg=Theme.BG)
        title_holder.pack(side="left", fill="x", expand=True)

        brand_row = tk.Frame(title_holder, bg=Theme.BG)
        brand_row.pack(anchor="w", fill="x")
        self._brand_image: tk.PhotoImage | None = None
        # Prefer the rendered wordmark (matches website); fall back to
        # the icon + plain text combo if the wordmark assets don't ship.
        # 256 is the most compact size (~47px tall with the new
        # breathing room) and renders crisply at the header height.
        # The 384/512 sizes ship for larger HiDPI scales the user can
        # opt into in a future zoom setting.
        for _name in (
            "assets/wordmark/wordmark-256.png",
            "assets/wordmark/wordmark-384.png",
            "assets/wordmark/wordmark-512.png",
        ):
            _wm = HERE / _name
            if _wm.is_file():
                try:
                    img = tk.PhotoImage(file=str(_wm))
                    if "wordmark-512" in _name:
                        img = img.subsample(2, 2)
                    self._brand_image = img
                    break
                except tk.TclError:
                    self._brand_image = None
        if self._brand_image is None:
            for _name in ("icon-64.png", "icon-128.png", "icon.png"):
                _logo = HERE / _name
                if _logo.is_file():
                    try:
                        self._brand_image = tk.PhotoImage(file=str(_logo))
                        if _name == "icon.png":
                            self._brand_image = self._brand_image.subsample(4, 4)
                        elif _name == "icon-128.png":
                            self._brand_image = self._brand_image.subsample(2, 2)
                        break
                    except tk.TclError:
                        self._brand_image = None
        if self._brand_image is not None:
            tk.Label(
                brand_row, image=self._brand_image, bg=Theme.BG
            ).pack(side="left", padx=(0, 12))
        else:
            ttk.Label(brand_row, text=APP_BRAND, style="Title.TLabel").pack(
                side="left", anchor="w"
            )
        ttk.Label(title_holder, text=APP_TAGLINE, style="Tagline.TLabel").pack(
            anchor="w", pady=(2, 0)
        )

        # Indigo -> violet -> cyan strip between header and body.
        # Redrawn on resize via _redraw_accent_strip.
        self.accent_strip = tk.Canvas(
            parent,
            bg=Theme.BG,
            height=3,
            highlightthickness=0,
            bd=0,
        )
        self.accent_strip.pack(side="top", fill="x", pady=(0, 12))
        self.accent_strip.bind("<Configure>", self._redraw_accent_strip)

    # Obfuscation key for the bundled resource. Just enough to break
    # `file` / `strings` recognition - the bundle isn't a secret, the
    # game is to keep the picture out of casual file listings.
    _ETERNAL_XOR_KEY = b"\x4e\xa7\x21\x8f\x6d\xc3\x55\x18\xb9\x02\xe7\x4a\x96\x21\xcd\x8e"

    def _on_easter_egg_click(self, _event=None) -> None:  # type: ignore[no-untyped-def]
        # Per-machine: once revealed (and persisted to config.json), the
        # zone goes inert.
        if self._config.get("eternal_seen"):
            return
        self._egg_clicks += 1
        if self._egg_clicks >= 69:
            self._egg_clicks = 0
            self._show_easter_egg()

    def _show_easter_egg(self) -> None:
        existing = getattr(self, "_egg_window", None)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.lift()
                    return
            except tk.TclError:
                pass

        blob_path = HERE / "assets" / "eternal"
        if not blob_path.is_file():
            return
        try:
            raw = blob_path.read_bytes()
            key = self._ETERNAL_XOR_KEY
            klen = len(key)
            png_bytes = bytes(b ^ key[i % klen] for i, b in enumerate(raw))
            import base64
            photo = tk.PhotoImage(data=base64.b64encode(png_bytes).decode("ascii"))
        except (tk.TclError, OSError):
            return

        win = tk.Toplevel(self.root)
        win.title("???")
        win.configure(bg=Theme.BG_DEEP)
        win.transient(self.root)
        try:
            win.iconphoto(False, *self._icons)
        except (tk.TclError, AttributeError):
            pass

        tk.Label(
            win,
            text="???",
            bg=Theme.BG_DEEP,
            fg=Theme.TEXT,
            font=self.font_title,
        ).pack(pady=(14, 8))

        label = tk.Label(win, image=photo, bg=Theme.BG_DEEP, borderwidth=0)
        label.image = photo  # type: ignore[attr-defined]
        label.pack(padx=12)

        tk.Label(
            win,
            text="eternal",
            bg=Theme.BG_DEEP,
            fg=Theme.TEXT_MUTED,
            font=self.font_drop_sub,
        ).pack(pady=(8, 16))

        win.bind("<Escape>", lambda _e: win.destroy())
        win.bind("<Button-1>", lambda _e: win.destroy())
        self._egg_window = win

        # Persist: once per machine.
        self._config["eternal_seen"] = True
        save_config(self._config)

    def _build_drop_zone(self, parent: tk.Frame) -> None:
        # 1px halo ring; the inner card keeps the real interactive border.
        halo = tk.Frame(parent, bg=Theme.BORDER, padx=1, pady=1)
        halo.pack(side="top", fill="x", pady=(0, 10))

        self.drop_card = tk.Frame(
            halo,
            bg=Theme.SURFACE,
            highlightthickness=1,
            highlightbackground=Theme.BORDER,
            highlightcolor=Theme.ACCENT_GLOW,
            cursor="hand2",
        )
        self.drop_card.pack(fill="both", expand=True)

        self.drop_inner = tk.Frame(self.drop_card, bg=Theme.SURFACE)
        self.drop_inner.pack(fill="both", expand=True, padx=16, pady=20)

        self.drop_icon = tk.Label(
            self.drop_inner,
            text="\u2B07",
            bg=Theme.SURFACE,
            fg=Theme.ACCENT_GLOW,
            font=_pick_font(["DejaVu Sans", "Symbola", "Helvetica"], 28, "bold"),
        )
        self.drop_icon.pack()

        self.drop_label = tk.Label(
            self.drop_inner,
            text="Drop a file here",
            bg=Theme.SURFACE,
            fg=Theme.TEXT,
            font=self.font_drop,
        )
        self.drop_label.pack(pady=(8, 2), fill="x")

        self.drop_sub = tk.Label(
            self.drop_inner,
            text="MP4, MOV, MP3, WAV, OGG \u2014 or click to browse",
            bg=Theme.SURFACE,
            fg=Theme.TEXT_MUTED,
            font=self.font_drop_sub,
            wraplength=420,
            justify="center",
        )
        self.drop_sub.pack(fill="x")

        for widget in (self.drop_card, self.drop_inner, self.drop_icon,
                       self.drop_label, self.drop_sub):
            widget.bind("<Button-1>", lambda _e: self._browse())
            widget.bind("<Enter>", lambda _e: self._set_drop_hover(True))
            widget.bind("<Leave>", lambda _e: self._set_drop_hover(False))

        if _DND_AVAILABLE:
            for widget in (self.drop_card, self.drop_inner, self.drop_label,
                           self.drop_icon, self.drop_sub):
                widget.drop_target_register(DND_FILES)  # type: ignore[attr-defined]
                widget.dnd_bind("<<Drop>>", self._on_drop)  # type: ignore[attr-defined]

    def _build_url_row(self, parent: tk.Frame) -> None:
        """URL entry + download-format dropdown + quality dropdown."""
        wrapper = tk.Frame(parent, bg=Theme.BG)
        wrapper.pack(side="top", fill="x", pady=(0, 10))

        tk.Label(
            wrapper,
            text="or paste a URL (YouTube and most sites)",
            bg=Theme.BG,
            fg=Theme.TEXT_MUTED,
            font=self.font_drop_sub,
            anchor="w",
        ).pack(fill="x", pady=(0, 4))

        row = tk.Frame(wrapper, bg=Theme.BG)
        row.pack(fill="x")

        self.url_entry = tk.Entry(
            row,
            textvariable=self.url_var,
            bg=Theme.SURFACE,
            fg=Theme.TEXT,
            insertbackground=Theme.TEXT,
            relief="flat",
            highlightthickness=1,
            highlightbackground=Theme.BORDER,
            highlightcolor=Theme.ACCENT,
            font=self.font_body,
        )
        self.url_entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 8))

        # Format picker
        self.format_label = tk.Label(
            row, text="Format", bg=Theme.BG, fg=Theme.TEXT_MUTED, font=self.font_drop_sub
        )
        self.format_label.pack(side="left", padx=(0, 4))
        self.format_combo = _make_combobox(
            row,
            textvariable=self.download_format_var,
            values=list(SUPPORTED_DOWNLOAD_FORMATS),
            state="readonly",
            width=6,
            font=self.font_body,
        )
        self.format_combo.pack(side="left", padx=(0, 8))

        # Quality picker (values populated by _refresh_quality_combo)
        self.quality_label = tk.Label(
            row, text="Quality", bg=Theme.BG, fg=Theme.TEXT_MUTED, font=self.font_drop_sub
        )
        self.quality_label.pack(side="left", padx=(0, 4))
        self.quality_combo = _make_combobox(
            row,
            textvariable=self.quality_var,
            values=list(VIDEO_QUALITY_LABELS),
            state="readonly",
            width=12,
            font=self.font_body,
        )
        self.quality_combo.pack(side="left")
        # When the user picks a new quality, remember it against the
        # current quality_kind so a Format swap doesn't lose it.
        self.quality_combo.bind("<<ComboboxSelected>>", self._on_quality_selected)

        # Cookies status strip. Hidden until the user picks a cookies
        # file (right-click on the URL entry -> "Use cookies file...").
        # Lives inside `wrapper` so it tucks in right under the URL row.
        self.cookies_row = tk.Frame(wrapper, bg=Theme.BG)
        # Don't pack yet - _refresh_cookies_display handles show/hide.
        self.cookies_label = tk.Label(
            self.cookies_row,
            textvariable=self.cookies_display_var,
            bg=Theme.BG,
            fg=Theme.TEXT_MUTED,
            font=self.font_drop_sub,
            anchor="w",
        )
        self.cookies_label.pack(side="left", fill="x", expand=True, pady=(4, 0))
        # 'Remember' checkbox: when ticked, the cookies path is persisted
        # to config.json and reloaded on the next launch (if the file
        # still exists). Off by default - users who care about scope
        # don't accidentally leak a session token into their config.
        self.remember_cookies_check = _make_check(
            self.cookies_row,
            text="Remember",
            variable=self.remember_cookies,
            font=self.font_body,
        )
        self.remember_cookies_check.pack(side="right", padx=(0, 8), pady=(4, 0))
        self.cookies_clear_btn = tk.Button(
            self.cookies_row,
            text="Clear",
            bg=Theme.BG,
            fg=Theme.TEXT_MUTED,
            activebackground=Theme.BG,
            activeforeground=Theme.TEXT,
            font=self.font_drop_sub,
            relief="flat",
            bd=0,
            padx=8,
            pady=0,
            cursor="hand2",
            command=self._clear_cookies_file,
        )
        self.cookies_clear_btn.pack(side="right", pady=(4, 0))

    def _build_save_dir_row(self, parent: tk.Frame) -> None:
        """Output folder picker. Empty = use a sensible auto default."""
        row = tk.Frame(parent, bg=Theme.BG)
        row.pack(side="top", fill="x", pady=(0, 12))

        tk.Label(
            row, text="Save to", bg=Theme.BG, fg=Theme.TEXT_MUTED,
            font=self.font_drop_sub,
        ).pack(side="left", padx=(0, 6))

        # The display label is read-only and middle-truncates long paths
        # so a 200-character home directory doesn't blow out the layout.
        self.save_dir_label = tk.Label(
            row,
            textvariable=self.save_dir_display_var,
            bg=Theme.SURFACE,
            fg=Theme.TEXT_MUTED,
            font=self.font_drop_sub,
            anchor="w",
            padx=8,
            pady=4,
            relief="flat",
            highlightthickness=1,
            highlightbackground=Theme.BORDER,
        )
        self.save_dir_label.pack(side="left", fill="x", expand=True, padx=(0, 8))

        self.reset_save_dir_btn = tk.Button(
            row,
            text="Reset",
            bg=Theme.SURFACE,
            fg=Theme.TEXT_MUTED,
            activebackground=Theme.SURFACE_HI,
            activeforeground=Theme.TEXT,
            disabledforeground=Theme.TEXT_DIM,
            font=self.font_drop_sub,
            relief="flat",
            bd=0,
            padx=10,
            pady=4,
            cursor="hand2",
            command=self._reset_save_dir,
        )
        self.reset_save_dir_btn.pack(side="right", padx=(6, 0))

        self.browse_save_dir_btn = tk.Button(
            row,
            text="Browse...",
            bg=Theme.SURFACE,
            fg=Theme.TEXT,
            activebackground=Theme.SURFACE_HI,
            activeforeground=Theme.TEXT,
            font=self.font_drop_sub,
            relief="flat",
            bd=0,
            padx=12,
            pady=4,
            cursor="hand2",
            command=self._choose_save_dir,
        )
        self.browse_save_dir_btn.pack(side="right")

    def _build_options(self, parent: tk.Frame) -> None:
        # Options card fills the leftover middle space. We don't draw
        # a border around it on purpose — the section labels (REMOVE /
        # REPLACE WITH / ...) read as the visual divider, and dropping
        # the 1px ring makes the whole panel breathe more.
        options_card = tk.Frame(parent, bg=Theme.SURFACE)
        options_card.pack(side="top", fill="both", expand=True, pady=(0, 10))

        opts_inner = tk.Frame(options_card, bg=Theme.SURFACE)
        opts_inner.pack(fill="x", padx=18, pady=16, anchor="n")

        ttk.Label(opts_inner, text="REMOVE", style="Section.TLabel").pack(anchor="w")
        _make_check(
            opts_inner,
            text="Swears",
            variable=self.remove_swears,
            font=self.font_body,
        ).pack(anchor="w", pady=(6, 0))
        _make_check(
            opts_inner,
            text="Racial slurs",
            variable=self.remove_slurs,
            font=self.font_body,
        ).pack(anchor="w", pady=(2, 12))

        self.replace_label = ttk.Label(
            opts_inner, text="REPLACE WITH", style="Section.TLabel"
        )
        self.replace_label.pack(anchor="w")
        self.silence_radio = _make_radio(
            opts_inner,
            text="Silence",
            variable=self.mode,
            value="silence",
            font=self.font_body,
        )
        self.silence_radio.pack(anchor="w", pady=(6, 0))
        self.beep_radio = _make_radio(
            opts_inner,
            text="Beep tone",
            variable=self.mode,
            value="beep",
            font=self.font_body,
        )
        self.beep_radio.pack(anchor="w", pady=(2, 0))
        self.fun_radio = _make_radio(
            opts_inner,
            text="TTS",
            variable=self.mode,
            value="fun",
            font=self.font_body,
        )
        self.fun_radio.pack(anchor="w", pady=(2, 12))

        self.fun_section_label = ttk.Label(
            opts_inner, text="TTS", style="Section.TLabel"
        )
        self.fun_section_label.pack(anchor="w")

        fv_row = tk.Frame(opts_inner, bg=Theme.SURFACE)
        fv_row.pack(anchor="w", fill="x", pady=(6, 0))
        self.fun_voice_label = ttk.Label(
            fv_row, text="Voice", style="OptionLabel.TLabel"
        )
        self.fun_voice_label.pack(side="left", padx=(0, 8))
        self.fun_voice_combo = _make_combobox(
            fv_row,
            values=funtts.fun_voice_labels(
                elevenlabs_key=bool(self._elevenlabs_api_key)
            ),
            state="readonly",
            width=32,
            font=self.font_body,
        )
        self.fun_voice_combo.pack(side="left")
        self.fun_voice_combo.bind(
            "<<ComboboxSelected>>", self._on_fun_voice_combo_selected
        )
        try:
            self.fun_voice_combo.current(
                funtts.fun_voice_index_for_id(self._fun_voice_id)
            )
        except tk.TclError:
            pass

        # Privacy note for online TTS voices. Sits right under the
        # voice picker so it can never be missed.
        self.fun_voice_privacy = ttk.Label(
            opts_inner,
            text=(
                "Online TTS sends only the substitute words "
                "(fudge, darn, ...) to ElevenLabs - never your audio."
            ),
            style="OptionLabelDim.TLabel",
            wraplength=460,
            justify="left",
        )
        self.fun_voice_privacy.pack(anchor="w", pady=(4, 0))

        self.retro_audio_check = _make_check(
            opts_inner,
            text="Retro audio (lo-fi crunch on the audio track)",
            variable=self.retro_audio_var,
            font=self.font_body,
        )
        self.retro_audio_check.pack(anchor="w", pady=(6, 12))

        ttk.Label(opts_inner, text="EXTRAS", style="Section.TLabel").pack(anchor="w")
        _make_check(
            opts_inner,
            text="Save transcript (.txt)",
            variable=self.save_transcript,
            font=self.font_body,
        ).pack(anchor="w", pady=(6, 0))
        _make_check(
            opts_inner,
            text="Fuzzy matching (catches fucks, fuuuck, phuck, f*ck, kunt...)",
            variable=self.fuzzy_matching,
            font=self.font_body,
        ).pack(anchor="w", pady=(2, 12))

        ttk.Label(opts_inner, text="OUTPUT", style="Section.TLabel").pack(anchor="w")
        ds_row = tk.Frame(opts_inner, bg=Theme.SURFACE)
        ds_row.pack(anchor="w", fill="x", pady=(6, 0))
        self.downsize_label = ttk.Label(
            ds_row, text="File size", style="OptionLabel.TLabel"
        )
        self.downsize_label.pack(side="left", padx=(0, 8))
        self.downsize_combo = _make_combobox(
            ds_row,
            textvariable=self.downsize_label_var,
            values=[lab for lab, _ in _DOWNSIZE_CHOICES],
            state="readonly",
            width=18,
            font=self.font_body,
        )
        self.downsize_combo.pack(side="left")

    # ---------- Context menus / right-click ----------

    def _install_context_menus(self) -> None:
        """Right-click menus for the URL entry and the drop zone, plus a
        global Ctrl+V shortcut that pastes the clipboard as a URL when
        focus isn't already in a text widget."""
        menu_kwargs = dict(
            tearoff=0,
            bg=Theme.SURFACE,
            fg=Theme.TEXT,
            activebackground=Theme.ACCENT,
            activeforeground="white",
            borderwidth=0,
            relief="flat",
        )

        # URL entry: full editing menu + paste-from-clipboard helper.
        self._url_menu = tk.Menu(self.url_entry, **menu_kwargs)
        self._url_menu.add_command(
            label="Cut", command=lambda: self.url_entry.event_generate("<<Cut>>")
        )
        self._url_menu.add_command(
            label="Copy", command=lambda: self.url_entry.event_generate("<<Copy>>")
        )
        self._url_menu.add_command(
            label="Paste", command=lambda: self.url_entry.event_generate("<<Paste>>")
        )
        self._url_menu.add_separator()
        self._url_menu.add_command(
            label="Paste URL (replace)", command=self._paste_url_replace
        )
        self._url_menu.add_separator()
        self._url_menu.add_command(label="Select All", command=self._url_select_all)
        self._url_menu.add_command(
            label="Clear", command=lambda: self.url_var.set("")
        )
        # Cookies-file picker for sites that gate content behind login
        # (Patreon paid posts, members-only YouTube, soft-paywalled
        # tubes, etc.). Path is held in self._cookies_file and snapshot
        # into CensorOptions when a job starts.
        self._url_menu.add_separator()
        self._url_menu.add_command(
            label="Use cookies file...", command=self._choose_cookies_file
        )
        self._url_menu.add_command(
            label="Clear cookies file",
            command=self._clear_cookies_file,
            state="disabled",
        )
        self._url_menu.add_command(
            label="Get browser extension...", command=self._show_cookies_help
        )
        # ElevenLabs livestream-style TTS voices (Brian / Adam / Sam /
        # ...). User supplies their own API key here; the key is stored
        # in config.json (mode 0o600 on POSIX). Removing it falls back
        # to the espeak-ng Klatt voices.
        self._url_menu.add_separator()
        self._url_menu.add_command(
            label="Set ElevenLabs API key...",
            command=self._set_elevenlabs_api_key,
        )
        self._url_menu.add_command(
            label="Clear ElevenLabs API key",
            command=self._clear_elevenlabs_api_key,
            state="disabled",
        )
        # Power-user hooks for sites with no official yt-dlp support.
        # Surfaces the per-user plugin folder + a friendly explainer
        # for paywalled platforms (OnlyFans, Fansly, etc.) where neither
        # cookies nor a plugin can defeat DRM.
        self._url_menu.add_separator()
        self._url_menu.add_command(
            label="Open plugins folder...", command=self._open_plugins_folder
        )
        self._url_menu.add_command(
            label="About paywall sites...", command=self._show_paywall_help
        )
        # Bind on ButtonRelease-3 (not Button-3): this means the menu pops
        # up after the right mouse button is released, so there's no
        # release event left to close it. Combined with NOT calling
        # grab_release(), this gives the "tap right-click, menu stays
        # until you click elsewhere" behaviour every user expects.
        self.url_entry.bind("<ButtonRelease-3>", self._show_url_menu)

        # Drop zone: paste URL + folder ingest + clear queue.
        self._drop_menu = tk.Menu(self.root, **menu_kwargs)
        self._drop_menu.add_command(
            label="Paste URL", command=self._paste_url_replace
        )
        # Recursive folder ingest. Handy for bulk workflows where some
        # other tool (browser auto-saver, screen recorder batch, separate
        # downloader) dumps a pile of MP4s/MP3s/etc. into a folder.
        self._drop_menu.add_command(
            label="Add all files from folder...",
            command=self._add_folder_via_dialog,
        )
        self._drop_menu.add_separator()
        self._drop_menu.add_command(
            label="Clear queue", command=self._clear_queue, state="disabled"
        )
        for w in (
            self.drop_card,
            self.drop_inner,
            self.drop_icon,
            self.drop_label,
            self.drop_sub,
        ):
            w.bind("<ButtonRelease-3>", self._show_drop_menu)

        # Global Ctrl+V: if focus is in a text entry let Tk handle it
        # normally, otherwise treat it as 'paste URL'.
        self.root.bind_all("<Control-v>", self._on_global_paste)
        self.root.bind_all("<Control-V>", self._on_global_paste)

    def _show_url_menu(self, event) -> None:  # type: ignore[no-untyped-def]
        # Live-toggle 'Clear cookies file' so it only lights up when
        # there's actually a cookies file loaded.
        try:
            state = "normal" if self._cookies_file is not None else "disabled"
            self._url_menu.entryconfig("Clear cookies file", state=state)
        except tk.TclError:
            pass
        try:
            ek_state = "normal" if self._elevenlabs_api_key else "disabled"
            self._url_menu.entryconfig("Clear ElevenLabs API key", state=ek_state)
        except tk.TclError:
            pass
        # Do NOT call grab_release() here - tk_popup manages its own grab
        # on Linux, and releasing it immediately kills the menu the moment
        # the right-mouse button comes back up.
        self._url_menu.tk_popup(event.x_root, event.y_root)

    def _show_drop_menu(self, event) -> None:  # type: ignore[no-untyped-def]
        # Live-toggle 'Clear queue' so it's only clickable when there's
        # actually something to clear.
        try:
            state = "normal" if self._input_paths else "disabled"
            self._drop_menu.entryconfig("Clear queue", state=state)
        except tk.TclError:
            pass
        self._drop_menu.tk_popup(event.x_root, event.y_root)

    def _url_select_all(self) -> None:
        self.url_entry.select_range(0, "end")
        self.url_entry.icursor("end")

    def _paste_url_replace(self) -> None:
        """Read the clipboard and stuff it into the URL field, overwriting
        whatever was there. Used by both right-click menus and Ctrl+V."""
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            return
        text = text.strip()
        if not text:
            return
        self.url_var.set(text)
        self.url_entry.focus_set()
        self.url_entry.icursor("end")

    def _on_global_paste(self, event) -> None:  # type: ignore[no-untyped-def]
        """Ctrl+V anywhere -> paste into URL field, unless focus is already
        in a text-entry widget (Entry / Spinbox / Text) so we don't break
        normal copy-paste behaviour there."""
        focused = self.root.focus_get()
        text_widget_classes = (tk.Entry, tk.Spinbox, tk.Text, ttk.Entry)
        if isinstance(focused, text_widget_classes):
            return  # let Tk handle the paste normally
        self._paste_url_replace()

    # ---------- Action area ----------

    def _build_action_area(self, parent: tk.Frame) -> None:
        """Status -> progress -> Censor button, packed bottom-up so they
        stay visible when the window is shrunk."""
        # Status line - uses tk.Label (not ttk) so we can set wraplength to
        # keep long messages / filenames visible when the window is narrow.
        self.status_label = tk.Label(
            parent,
            textvariable=self.status_var,
            bg=Theme.BG,
            fg=Theme.TEXT_MUTED,
            font=self.font_status,
            anchor="w",
            justify="left",
            wraplength=500,
        )
        self.status_label.pack(side="bottom", fill="x", pady=(2, 0))

        # Progress bar above the status
        self.progress = _make_progress(parent, length=400)
        self.progress.pack(side="bottom", fill="x", pady=(0, 4))

        # Primary action button. CTk gives us a website-matched rounded
        # button with smooth hover; tk.Button is the fallback if CTk
        # isn't installed.
        if _CTK_AVAILABLE:
            self.action_btn = ctk.CTkButton(  # type: ignore[union-attr]
                parent,
                text="Censor",
                command=self._on_action,
                fg_color=Theme.BORDER,
                hover_color=Theme.ACCENT,
                text_color=Theme.TEXT_MUTED,
                text_color_disabled=Theme.TEXT_DIM,
                corner_radius=12,
                height=46,
                font=_ctk_font(self.font_button),
                state="disabled",
            )
            self.action_btn.pack(side="bottom", fill="x", pady=(8, 10))
        else:
            self.action_btn = tk.Button(
                parent,
                text="Censor",
                bg=Theme.BORDER,
                fg=Theme.TEXT_MUTED,
                activebackground=Theme.ACCENT_LO,
                activeforeground="white",
                disabledforeground=Theme.TEXT_DIM,
                highlightthickness=0,
                font=self.font_button,
                relief="flat",
                bd=0,
                padx=24,
                pady=12,
                cursor="arrow",
                state="disabled",
                command=self._on_action,
            )
            self.action_btn.pack(side="bottom", fill="x", pady=(8, 10))
            self.action_btn.bind("<Enter>", self._on_btn_hover)
            self.action_btn.bind("<Leave>", self._on_btn_leave)

    def _build_footer(self, parent: tk.Frame) -> None:
        """Copyright + divider, pinned at the very bottom."""
        if not _DND_AVAILABLE:
            ttk.Label(
                parent,
                text="(drag-and-drop disabled - tkinterdnd2 missing)",
                style="Status.TLabel",
            ).pack(side="bottom", pady=(4, 0))

        ttk.Label(
            parent, text=COPYRIGHT, style="Footer.TLabel", anchor="center"
        ).pack(side="bottom", fill="x", pady=(6, 0))

        divider = tk.Frame(parent, bg=Theme.BORDER, height=1)
        divider.pack(side="bottom", fill="x", pady=(8, 0))

    # ---------- Options reactivity ----------

    def _on_options_changed(self) -> None:
        """Single entry point invoked whenever any option BooleanVar changes."""
        self._refresh_action_label()
        self._refresh_replace_section()

    # Debounce delay for the URL entry (ms). A user pasting a 100-char
    # URL fires 100 trace events; without this we'd run 6 UI-refresh
    # methods per character. 80ms is short enough to feel instant on
    # paste-and-pause workflows, long enough to coalesce typing bursts.
    _URL_DEBOUNCE_MS = 80

    def _on_url_changed(self) -> None:
        """Fired whenever the URL entry text changes. Coalesces fast
        bursts (typing / pasting) into a single UI refresh after the
        burst settles, then runs the actual work in
        `_apply_url_change`."""
        # Maintain the queue invariant immediately so callers that
        # peek at `self._input_paths` between trace events see the
        # right state. The visible UI refresh, which is the expensive
        # part, is what we defer.
        text = self.url_var.get().strip()
        if text:
            self._input_paths = []
        if getattr(self, "_url_debounce_after", None) is not None:
            try:
                self.root.after_cancel(self._url_debounce_after)
            except tk.TclError:
                pass
        self._url_debounce_after = self.root.after(
            self._URL_DEBOUNCE_MS, self._apply_url_change
        )

    def _apply_url_change(self) -> None:
        """The expensive half of `_on_url_changed`, run once per
        debounce interval."""
        self._url_debounce_after = None
        self._refresh_drop_display()
        self._refresh_action_label()
        self._refresh_action_enabled()
        self._refresh_format_combo()
        self._refresh_save_dir_display()

    def _refresh_drop_display(self) -> None:
        """Single source of truth for what the drop card shows.

        Priority: URL > queued files > empty placeholder. When 2+ files
        are queued, lists the first four by name and summarizes the rest.
        """
        url_text = self.url_var.get().strip()
        if url_text:
            if is_url(url_text):
                self.drop_icon.config(text="\u2713", fg=Theme.SUCCESS)
                self.drop_label.config(text="URL ready", fg=Theme.SUCCESS)
                self.drop_sub.config(
                    text=_short(url_text, 60), fg=Theme.TEXT_MUTED
                )
            else:
                self.drop_icon.config(text="\u26A0", fg=Theme.DANGER)
                self.drop_label.config(text="Not a URL", fg=Theme.DANGER)
                self.drop_sub.config(
                    text="Paste a full link starting with http(s)://",
                    fg=Theme.TEXT_MUTED,
                )
            return

        n = len(self._input_paths)
        if n == 0:
            self.drop_icon.config(text="\u2B07", fg=Theme.ACCENT)
            self.drop_label.config(text="Drop file(s) here", fg=Theme.TEXT)
            self.drop_sub.config(
                text="MP4, MOV, MP3, WAV, OGG \u2014 drop multiple to batch, "
                     "or click to browse",
                fg=Theme.TEXT_MUTED,
            )
            return
        if n == 1:
            self.drop_icon.config(text="\u2713", fg=Theme.SUCCESS)
            self.drop_label.config(text="Ready", fg=Theme.SUCCESS)
            self.drop_sub.config(
                text=self._input_paths[0].name, fg=Theme.TEXT_MUTED
            )
            return
        # 2+ files queued
        self.drop_icon.config(text="\u2713", fg=Theme.SUCCESS)
        self.drop_label.config(text=f"{n} files queued", fg=Theme.SUCCESS)
        shown = self._input_paths[:4]
        lines = [_short(p.name, 60) for p in shown]
        if n > len(shown):
            lines.append(f"+{n - len(shown)} more")
        self.drop_sub.config(text="\n".join(lines), fg=Theme.TEXT_MUTED)

    def _refresh_replace_section(self) -> None:
        """Disable the Silence/Beep/Fun radios when no censor is ticked."""
        if not hasattr(self, "silence_radio"):
            return
        censoring = self.remove_swears.get() or self.remove_slurs.get()
        try:
            for radio in (self.silence_radio, self.beep_radio, self.fun_radio):
                if hasattr(radio, "state"):
                    # ttk.Radiobutton path
                    radio.state(["!disabled"] if censoring else ["disabled"])
                else:
                    # CTk path
                    radio.configure(state="normal" if censoring else "disabled")
            self.replace_label.configure(
                style="Section.TLabel" if censoring else "SectionDim.TLabel"
            )
        except tk.TclError:
            pass
        self._refresh_fun_voice_row()

    def _on_fun_voice_combo_selected(self, _event=None) -> None:  # type: ignore[no-untyped-def]
        idx = int(self.fun_voice_combo.current())
        self._fun_voice_id = funtts.fun_voice_id_at_index(idx)

    def _downsize_key(self) -> str:
        lab = self.downsize_label_var.get()
        for dlab, key in _DOWNSIZE_CHOICES:
            if dlab == lab:
                return key
        return "none"

    def _refresh_fun_voice_row(self) -> None:
        """Enable the Fun voice picker only when Fun mode can run.

        Retro audio stays enabled regardless because it's also useful
        on plain download / silence / beep jobs.
        """
        if not hasattr(self, "fun_voice_combo"):
            return
        censoring = self.remove_swears.get() or self.remove_slurs.get()
        fun_ok = censoring and self.mode.get() == "fun"
        try:
            self.fun_voice_combo.configure(state="readonly" if fun_ok else "disabled")
            self.fun_voice_label.configure(
                style="OptionLabel.TLabel" if fun_ok else "OptionLabelDim.TLabel"
            )
        except tk.TclError:
            pass

    def _refresh_format_combo(self) -> None:
        """Format + Quality dropdowns only matter when a URL is the input."""
        if not hasattr(self, "format_combo"):
            return
        active = bool(self.url_var.get().strip())
        try:
            self.format_combo.configure(
                state="readonly" if active else "disabled"
            )
        except tk.TclError:
            pass
        # The Quality combo's "active" state depends on both URL presence
        # AND whether the current format actually has a quality choice
        # (WAV is always lossless).
        self._refresh_quality_combo_state()

    def _refresh_quality_combo(self) -> None:
        """Swap the Quality combo's values to match the chosen Format,
        without losing the user's previous choice for the other kind.

        Called from a trace on download_format_var and on first build.
        """
        if not hasattr(self, "quality_combo"):
            return

        # Save whatever's currently shown back to its bucket so it
        # survives a round-trip through other formats.
        current = self.quality_var.get()
        if self._quality_kind == "video" and current in VIDEO_QUALITY_LABELS:
            self._last_video_quality = current
        elif self._quality_kind == "audio" and current in AUDIO_QUALITY_LABELS:
            self._last_audio_quality = current

        fmt = self.download_format_var.get().lower()
        # Video containers: full resolution ladder.
        if fmt in ("mp4", "mov", "mkv", "webm", "avi", "flv"):
            self._quality_kind = "video"
            self.quality_combo.configure(values=list(VIDEO_QUALITY_LABELS))
            self.quality_var.set(self._last_video_quality)
        # Lossy audio: kbps ladder.
        elif fmt in ("mp3", "m4a", "ogg", "opus"):
            self._quality_kind = "audio"
            self.quality_combo.configure(values=list(AUDIO_QUALITY_LABELS))
            self.quality_var.set(self._last_audio_quality)
        # Lossless audio: no choice to make.
        elif fmt in ("wav", "flac"):
            self._quality_kind = "lossless"
            self.quality_combo.configure(values=["Lossless"])
            self.quality_var.set("Lossless")

        self._refresh_quality_combo_state()

    def _refresh_quality_combo_state(self) -> None:
        """Enable the Quality combo only when a URL is present AND the
        chosen format actually supports a quality choice."""
        if not hasattr(self, "quality_combo"):
            return
        url_active = bool(self.url_var.get().strip())
        usable = url_active and self._quality_kind not in ("wav", "lossless")
        try:
            self.quality_combo.configure(
                state="readonly" if usable else "disabled"
            )
        except tk.TclError:
            pass

    def _on_quality_selected(self, _event=None) -> None:  # type: ignore[no-untyped-def]
        """Remember the user's pick against the active quality kind."""
        val = self.quality_var.get()
        if self._quality_kind == "video":
            self._last_video_quality = val
        elif self._quality_kind == "audio":
            self._last_audio_quality = val

    # ---------- Save folder ----------

    def _choose_save_dir(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        initial = (
            str(self._output_dir)
            if self._output_dir is not None
            else str(self._auto_save_dir())
        )
        chosen = filedialog.askdirectory(
            title="Choose a folder to save to",
            initialdir=initial,
            mustexist=True,
        )
        if not chosen:
            return
        self._output_dir = Path(chosen)
        self._refresh_save_dir_display()

    def _reset_save_dir(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._output_dir = None
        self._refresh_save_dir_display()

    def _auto_save_dir(self) -> Path:
        """The folder we'd use if the user didn't pick one explicitly.

        With multiple files queued this still uses the first file's
        folder as a stand-in, since "auto" only matters when nothing
        explicit has been chosen.
        """
        if self._input_paths:
            return self._input_paths[0].parent
        return default_download_dir()

    def _refresh_save_dir_display(self) -> None:
        if not hasattr(self, "save_dir_label"):
            return
        if self._output_dir is None:
            auto = self._auto_save_dir()
            self.save_dir_display_var.set(f"(auto: {_short(str(auto), 60)})")
            try:
                self.save_dir_label.configure(fg=Theme.TEXT_DIM)
                self.reset_save_dir_btn.configure(state="disabled", cursor="arrow")
            except tk.TclError:
                pass
        else:
            self.save_dir_display_var.set(_short(str(self._output_dir), 60))
            try:
                self.save_dir_label.configure(fg=Theme.TEXT)
                self.reset_save_dir_btn.configure(state="normal", cursor="hand2")
            except tk.TclError:
                pass

    # ---------- Cookies file (yt-dlp auth) ----------

    def _choose_cookies_file(self) -> None:
        """Pick a Netscape-format cookies.txt for yt-dlp to use.

        See: yt-dlp's `--cookies` option. Browser extensions like
        "Get cookies.txt LOCALLY" (Chrome) or "cookies.txt" (Firefox)
        export a file in the right format. We hold the path in
        `self._cookies_file` for the rest of the session, and persist
        it across launches when `remember_cookies` is on.
        """
        if self._worker and self._worker.is_alive():
            return
        initial_dir = (
            str(self._cookies_file.parent)
            if self._cookies_file is not None
            else str(Path.home())
        )
        chosen = filedialog.askopenfilename(
            title="Choose a cookies.txt file (Netscape format)",
            initialdir=initial_dir,
            filetypes=[
                ("Cookies files", "*.txt"),
                ("All files", "*.*"),
            ],
        )
        if not chosen:
            return
        path = Path(chosen)
        if not path.is_file():
            messagebox.showerror(
                APP_TITLE, f"Not a readable file:\n{path}"
            )
            return
        self._cookies_file = path
        self._refresh_cookies_display()
        self._persist_cookies_pref()

    def _clear_cookies_file(self) -> None:
        """Forget the loaded cookies file. Doesn't delete the file."""
        if self._worker and self._worker.is_alive():
            return
        if self._cookies_file is None:
            return
        self._cookies_file = None
        self._refresh_cookies_display()
        self._persist_cookies_pref()

    # ---------- ElevenLabs API key (online TTS) ----------

    def _set_elevenlabs_api_key(self) -> None:
        """Prompt for an ElevenLabs API key and persist it.

        The key is stored in config.json; on POSIX `save_config()`
        sets the file mode to 0o600 so a casual ``cat`` won't leak it
        to other users on the box.
        """
        if self._worker and self._worker.is_alive():
            return
        prompt = (
            "Paste your ElevenLabs API key (starts with 'sk_'). "
            "Leave blank to clear."
        )
        # CTkInputDialog (smooth) when CTk is available, else simpledialog.
        new_key: str | None
        if _CTK_AVAILABLE:
            dialog = ctk.CTkInputDialog(  # type: ignore[union-attr]
                title="ElevenLabs API key",
                text=prompt,
            )
            new_key = dialog.get_input()
        else:
            from tkinter import simpledialog
            new_key = simpledialog.askstring(
                "ElevenLabs API key", prompt, parent=self.root, show="*"
            )
        if new_key is None:
            return  # user cancelled
        new_key = new_key.strip()
        self._elevenlabs_api_key = new_key
        self._config["elevenlabs_api_key"] = new_key
        save_config(self._config)
        # Re-decorate the Fun voice combo so the `(needs API key)`
        # suffix appears / disappears immediately.
        self._refresh_fun_voice_options()
        msg = (
            "ElevenLabs API key saved. Pick a livestream voice "
            "(Brian / Adam / ...) in the Fun voice list."
            if new_key
            else "ElevenLabs API key cleared."
        )
        self.status_var.set(msg)

    def _clear_elevenlabs_api_key(self) -> None:
        """Forget the stored ElevenLabs API key."""
        if self._worker and self._worker.is_alive():
            return
        if not self._elevenlabs_api_key:
            return
        self._elevenlabs_api_key = ""
        self._config["elevenlabs_api_key"] = ""
        save_config(self._config)
        self._refresh_fun_voice_options()
        self.status_var.set("ElevenLabs API key cleared.")

    def _refresh_fun_voice_options(self) -> None:
        """Update the Fun voice picker labels to reflect API-key state."""
        if not hasattr(self, "fun_voice_combo"):
            return
        try:
            self.fun_voice_combo.configure(
                values=funtts.fun_voice_labels(
                    elevenlabs_key=bool(self._elevenlabs_api_key)
                )
            )
            # Restore the user's selected voice by index.
            self.fun_voice_combo.current(
                funtts.fun_voice_index_for_id(self._fun_voice_id)
            )
        except tk.TclError:
            pass

    def _refresh_cookies_display(self) -> None:
        """Show or hide the cookies strip under the URL row depending on
        whether a cookies file is loaded."""
        if not hasattr(self, "cookies_row"):
            return
        if self._cookies_file is None:
            try:
                self.cookies_row.pack_forget()
            except tk.TclError:
                pass
            self.cookies_display_var.set("")
            return
        self.cookies_display_var.set(
            f"Cookies: {_short(self._cookies_file.name, 60)}"
        )
        try:
            # Pack just below the entry/format/quality row inside the
            # same wrapper. fill='x' so it lines up with the entry above.
            self.cookies_row.pack(fill="x", pady=(0, 0))
        except tk.TclError:
            pass

    def _on_remember_cookies_toggled(self) -> None:
        """Trace handler for the Remember checkbox."""
        if self._suppress_remember_trace:
            return
        self._persist_cookies_pref()

    def _persist_cookies_pref(self) -> None:
        """Sync the current cookies state to config.json.

        - Remember ticked + cookies loaded -> persist the path.
        - Remember unticked OR no cookies -> wipe the persisted path,
          and remember=False so the next launch starts clean.
        """
        remember = bool(self.remember_cookies.get())
        if remember and self._cookies_file is not None:
            self._config["cookies_file"] = str(self._cookies_file)
            self._config["remember_cookies"] = True
        else:
            self._config.pop("cookies_file", None)
            self._config["remember_cookies"] = False
        save_config(self._config)

    def _restore_cookies_from_config(self) -> None:
        """Rehydrate the cookies pref from config.json at startup.

        Falls back to no cookies and clears the stored entry if the
        remembered file is missing.
        """
        if not self._config.get("remember_cookies"):
            return
        raw = self._config.get("cookies_file")
        if not raw:
            return
        path = Path(str(raw))
        if not path.is_file():
            self._config.pop("cookies_file", None)
            self._config["remember_cookies"] = False
            save_config(self._config)
            return
        self._cookies_file = path
        self._suppress_remember_trace = True
        try:
            self.remember_cookies.set(True)
        finally:
            self._suppress_remember_trace = False
        self._refresh_cookies_display()

    def _show_cookies_help(self) -> None:
        """Help dialog for the cookies-file workflow."""
        win = tk.Toplevel(self.root)
        win.title("Get a cookies.txt browser extension")
        win.configure(bg=Theme.BG)
        win.transient(self.root)
        try:
            win.grab_set()
        except tk.TclError:
            pass
        win.resizable(False, False)

        wrap = tk.Frame(win, bg=Theme.BG)
        wrap.pack(padx=20, pady=18, fill="both", expand=True)

        tk.Label(
            wrap,
            text="Export cookies for yt-dlp",
            bg=Theme.BG,
            fg=Theme.TEXT,
            font=self.font_section,
            anchor="w",
        ).pack(fill="x", pady=(0, 8))

        body = (
            "Install one of these free extensions, sign in to the site you "
            "want to download from, click the extension and save the "
            "cookies.txt to your computer. Then right-click the URL field "
            "in CMVideo and pick 'Use cookies file...'.\n\n"
            "Keep the file private - it contains your session token. "
            "Re-export it when login expires or the site logs you out."
        )
        tk.Label(
            wrap,
            text=body,
            bg=Theme.BG,
            fg=Theme.TEXT_MUTED,
            font=self.font_drop_sub,
            justify="left",
            wraplength=420,
            anchor="w",
        ).pack(fill="x", pady=(0, 14))

        for label, url in COOKIES_EXTENSION_LINKS:
            row = tk.Frame(wrap, bg=Theme.BG)
            row.pack(fill="x", pady=(0, 6))
            tk.Label(
                row, text=label, bg=Theme.BG, fg=Theme.TEXT,
                font=self.font_drop_sub, anchor="w",
            ).pack(side="left", fill="x", expand=True)
            tk.Button(
                row,
                text="Open",
                bg=Theme.ACCENT,
                fg="white",
                activebackground=Theme.ACCENT_HI,
                activeforeground="white",
                font=self.font_drop_sub,
                relief="flat",
                bd=0,
                padx=14,
                pady=4,
                cursor="hand2",
                command=lambda u=url: webbrowser.open_new_tab(u),
            ).pack(side="right")

        btn_row = tk.Frame(wrap, bg=Theme.BG)
        btn_row.pack(fill="x", pady=(14, 0))
        tk.Button(
            btn_row,
            text="Close",
            bg=Theme.SURFACE,
            fg=Theme.TEXT,
            activebackground=Theme.SURFACE_HI,
            activeforeground=Theme.TEXT,
            font=self.font_drop_sub,
            relief="flat",
            bd=0,
            padx=14,
            pady=4,
            cursor="hand2",
            command=win.destroy,
        ).pack(side="right")

    # ---------- Plugins folder / paywall help ----------

    def _open_plugins_folder(self) -> None:
        """Reveal the plugin folder in the OS file manager.

        First call creates the folder and README. If no file manager is
        available, falls back to a messagebox showing the path.
        """
        path = ensure_user_plugin_dir()
        try:
            if sys.platform.startswith("linux"):
                subprocess.Popen(["xdg-open", str(path)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            elif sys.platform.startswith("win"):
                subprocess.Popen(["explorer", str(path)])
            else:
                raise OSError("unsupported platform")
        except Exception as e:  # noqa: BLE001
            messagebox.showinfo(
                APP_TITLE,
                f"Plugin folder:\n{path}\n\n"
                f"(Could not auto-open: {e})",
            )

    def _show_paywall_help(self) -> None:
        """Help dialog for paywalled / login-gated platforms."""
        win = tk.Toplevel(self.root)
        win.title("Paywalled & login-gated sites")
        win.configure(bg=Theme.BG)
        win.transient(self.root)
        try:
            win.grab_set()
        except tk.TclError:
            pass
        win.resizable(False, False)

        wrap = tk.Frame(win, bg=Theme.BG)
        wrap.pack(padx=20, pady=18, fill="both", expand=True)

        tk.Label(
            wrap,
            text="Paywalled and login-gated sites",
            bg=Theme.BG,
            fg=Theme.TEXT,
            font=self.font_section,
            anchor="w",
        ).pack(fill="x", pady=(0, 8))

        body = (
            "CMVideo handles every site yt-dlp has an extractor for "
            "(~1,800 sites). It can't fetch from sites without an "
            "extractor and it can't decrypt DRM.\n\n"
            "OnlyFans / Fansly / JustForFans / LoyalFans / Fanvue\n"
            "  No extractor ships. Cookies don't help on their own. "
            "Drop a community plugin into the plugins folder below, "
            "or screen-record playback with OBS and feed the MP4 in.\n\n"
            "Patreon / Substack video / members-only YouTube\n"
            "  Extractor exists. Export cookies (right-click the URL "
            "field \u2192 'Get browser extension...') and paste the URL.\n\n"
            "Netflix / Disney+ / Prime / Apple TV+ / Spotify / paid "
            "Pornhub Premium / Brazzers\n"
            "  Widevine DRM. Refused by yt-dlp regardless of plugin or "
            "cookies. Screen-record with OBS if your license allows."
        )
        tk.Label(
            wrap,
            text=body,
            bg=Theme.BG,
            fg=Theme.TEXT_MUTED,
            font=self.font_drop_sub,
            justify="left",
            wraplength=460,
            anchor="w",
        ).pack(fill="x", pady=(0, 14))

        btn_row = tk.Frame(wrap, bg=Theme.BG)
        btn_row.pack(fill="x")
        tk.Button(
            btn_row,
            text="Open plugins folder",
            bg=Theme.ACCENT,
            fg="white",
            activebackground=Theme.ACCENT_HI,
            activeforeground="white",
            font=self.font_drop_sub,
            relief="flat",
            bd=0,
            padx=14,
            pady=4,
            cursor="hand2",
            command=lambda: (win.destroy(), self._open_plugins_folder()),
        ).pack(side="left")
        tk.Button(
            btn_row,
            text="yt-dlp plugin docs",
            bg=Theme.SURFACE,
            fg=Theme.TEXT,
            activebackground=Theme.SURFACE_HI,
            activeforeground=Theme.TEXT,
            font=self.font_drop_sub,
            relief="flat",
            bd=0,
            padx=14,
            pady=4,
            cursor="hand2",
            command=lambda: webbrowser.open_new_tab(
                "https://github.com/yt-dlp/yt-dlp/wiki/Plugins"
            ),
        ).pack(side="left", padx=(8, 0))
        tk.Button(
            btn_row,
            text="Close",
            bg=Theme.SURFACE,
            fg=Theme.TEXT,
            activebackground=Theme.SURFACE_HI,
            activeforeground=Theme.TEXT,
            font=self.font_drop_sub,
            relief="flat",
            bd=0,
            padx=14,
            pady=4,
            cursor="hand2",
            command=win.destroy,
        ).pack(side="right")

    def _refresh_action_enabled(self) -> None:
        """Enable/disable the action button based on whether we have a source."""
        if not hasattr(self, "action_btn"):
            return
        has_source = bool(self._input_paths) or bool(
            self.url_var.get().strip()
        )
        current = self.action_btn.cget("text")
        if current == "Working...":
            return
        if current == "Open Folder" and not has_source:
            # Post-job idle state: keep "Open Folder" reachable until the
            # user actually queues something new. The moment fresh input
            # arrives we fall through and relabel/re-enable normally.
            return
        if has_source:
            self._enable_action_btn(self._intended_action_label())
        else:
            self._disable_action_btn(self._intended_action_label())

    # ---------- Action label ----------

    def _intended_action_label(self) -> str:
        """The label the action button SHOULD show given current option state."""
        url_mode = bool(self.url_var.get().strip())
        censoring = self.remove_swears.get() or self.remove_slurs.get()
        transcript_only = (not censoring) and self.save_transcript.get()
        n = len(self._input_paths)
        if url_mode:
            # URL mode is always a single job.
            if censoring:
                return "Download & Censor"
            if transcript_only:
                return "Download & Save Transcript"
            return "Download"
        base = "Save Transcript" if transcript_only else "Censor"
        if n > 1:
            return f"{base} ({n} files)"
        return base

    # Labels that mean "the button is doing something, don't relabel it".
    # Anything else is treated as an idle, relabel-friendly state.
    _BUSY_LABELS = {"Working...", "Open Folder"}

    def _refresh_action_label(self) -> None:
        """Re-label the action button when options change.

        Skips when the worker is running ('Working...') or when the
        button is showing the post-job 'Open Folder' affordance *and*
        no new input has been queued yet - that way the user can still
        jump to the just-finished result. Once they drop a file or
        paste a URL we relabel back to the intended action ('Censor',
        'Download', etc.).
        """
        if not hasattr(self, "action_btn"):
            return
        try:
            current = self.action_btn.cget("text")
        except tk.TclError:
            return
        if current == "Working...":
            return
        has_source = bool(self._input_paths) or bool(
            self.url_var.get().strip()
        )
        if current == "Open Folder" and not has_source:
            return
        self.action_btn.configure(text=self._intended_action_label())

    # ---------- Resize handling ----------

    # Debounce window-resize work to ~60 fps max. Without this the
    # accent-strip repaint and three wraplength updates fire on every
    # single Configure event Tk emits while the user drags the window
    # corner, which on Linux can be hundreds of times per second and
    # is the main cause of "the app is laggy as hell".
    _RESIZE_DEBOUNCE_MS = 16

    def _on_resize(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.widget is not self.root:
            return
        self._pending_resize_w = event.width
        if getattr(self, "_resize_after_id", None) is not None:
            return
        self._resize_after_id = self.root.after(
            self._RESIZE_DEBOUNCE_MS, self._apply_resize
        )

    def _apply_resize(self) -> None:
        self._resize_after_id = None
        width = getattr(self, "_pending_resize_w", None)
        if width is None:
            return
        # Skip the work entirely when the width hasn't actually
        # changed since the last apply (Configure events fire for
        # plain re-layout and tab focus changes too).
        if width == getattr(self, "_last_applied_width", None):
            return
        self._last_applied_width = width
        wrap = max(180, width - 60)
        try:
            self.status_label.config(wraplength=wrap)
            self.drop_sub.config(wraplength=wrap - 40)
            self.drop_label.config(wraplength=wrap - 40)
        except tk.TclError:
            pass

    # ---------- Visual state helpers ----------

    @staticmethod
    def _lerp_hex(c1: str, c2: str, t: float) -> str:
        """Linearly interpolate two #rrggbb hex colours. t in [0, 1]."""
        r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
        r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
        r = round(r1 + (r2 - r1) * t)
        g = round(g1 + (g2 - g1) * t)
        b = round(b1 + (b2 - b1) * t)
        return f"#{r:02x}{g:02x}{b:02x}"

    def _redraw_accent_strip(self, _event=None) -> None:
        """Paint a horizontal indigo -> violet -> cyan gradient onto the
        accent strip canvas.

        Performance: we used to call ``create_line`` once per pixel
        column (~860 Tk round-trips per repaint at 1080p); the resize
        handler is debounced now but a brand new repaint is still O(w).
        We batch the whole row into a single ``PhotoImage.put`` call,
        which is ~50x faster on Linux because Tk only crosses the C
        boundary once.
        """
        c = getattr(self, "accent_strip", None)
        if c is None:
            return
        try:
            w = c.winfo_width()
            h = c.winfo_height()
        except tk.TclError:
            return
        if w < 2 or h < 1:
            return

        # Cache + reuse the gradient PhotoImage. We only redraw when
        # the width changes, since the height of the strip is fixed.
        cache = getattr(self, "_grad_cache", None)
        if cache is not None and cache.get("w") == w and cache.get("h") == h:
            return  # nothing to do

        # Debounce: during a window-drag <Configure> fires dozens of
        # times per second per widget. Coalesce them via after_idle so
        # we only build the PhotoImage once per Tk update cycle.
        if getattr(self, "_grad_pending", False):
            return
        self._grad_pending = True
        self.root.after_idle(self._do_redraw_accent_strip, w, h)

    def _do_redraw_accent_strip(self, w: int, h: int) -> None:  # type: ignore[no-untyped-def]
        self._grad_pending = False
        c = getattr(self, "accent_strip", None)
        if c is None:
            return
        try:
            cur_w = c.winfo_width()
            cur_h = c.winfo_height()
        except tk.TclError:
            return
        # The widget may have changed size again since the after_idle
        # was scheduled; pull the live width/height instead of using
        # the stale args.
        w, h = max(2, cur_w), max(1, cur_h)
        cache = getattr(self, "_grad_cache", None)
        if cache is not None and cache.get("w") == w and cache.get("h") == h:
            return

        stops = (Theme.ACCENT_LO, Theme.ACCENT_GLOW, Theme.COOL)
        n = len(stops) - 1
        # Build one row of hex colors; PhotoImage.put accepts a
        # whitespace-separated list of `#rrggbb` tokens for a row.
        row_tokens: list[str] = []
        for x in range(w):
            t = x / max(1, w - 1) * n
            seg = min(int(t), n - 1)
            row_tokens.append(self._lerp_hex(stops[seg], stops[seg + 1], t - seg))
        row = "{" + " ".join(row_tokens) + "}"
        # Pillow + tk PhotoImage both accept the same put() string. We
        # keep the image around on `self` so the GC doesn't reap it
        # while it's still referenced by the canvas.
        img = tk.PhotoImage(width=w, height=h)
        img.put(" ".join([row] * h))
        c.delete("grad")
        c.create_image(0, 0, anchor="nw", image=img, tags="grad")
        self._grad_cache = {"w": w, "h": h, "img": img}

    def _set_drop_hover(self, hover: bool) -> None:
        if self._worker and self._worker.is_alive():
            return
        color = Theme.BORDER_GLOW if hover else Theme.BORDER
        self.drop_card.config(highlightbackground=color)

    def _enable_action_btn(self, label: str = "Censor") -> None:
        if _CTK_AVAILABLE:
            self.action_btn.configure(
                state="normal",
                text=label,
                fg_color=Theme.ACCENT,
                hover_color=Theme.ACCENT_HI,
                text_color="white",
            )
        else:
            self.action_btn.config(
                state="normal",
                text=label,
                bg=Theme.ACCENT,
                fg="white",
                cursor="hand2",
            )

    def _disable_action_btn(self, label: str = "Censor") -> None:
        if _CTK_AVAILABLE:
            self.action_btn.configure(
                state="disabled",
                text=label,
                fg_color=Theme.BORDER,
                text_color=Theme.TEXT_MUTED,
            )
        else:
            self.action_btn.config(
                state="disabled",
                text=label,
                bg=Theme.BORDER,
                fg=Theme.TEXT_MUTED,
                cursor="arrow",
            )

    def _set_action_working(self, label: str = "Cancel") -> None:
        """Switch the action button into 'job-running' mode.

        While a job is alive the button is the cancel control. The
        click handler in :meth:`_on_action` looks at the button text
        to decide which path to dispatch."""
        if _CTK_AVAILABLE:
            self.action_btn.configure(
                state="normal",
                text=label,
                fg_color=Theme.DANGER,
                hover_color="#ef4444",
                text_color="white",
            )
        else:
            self.action_btn.config(
                state="normal",
                text=label,
                bg=Theme.DANGER,
                fg="white",
                cursor="hand2",
            )

    def _on_btn_hover(self, _event) -> None:  # type: ignore[no-untyped-def]
        if _CTK_AVAILABLE:
            return  # CTk handles hover internally
        if self.action_btn.cget("state") == "normal":
            self.action_btn.config(bg=Theme.ACCENT_HI)

    def _on_btn_leave(self, _event) -> None:  # type: ignore[no-untyped-def]
        if _CTK_AVAILABLE:
            return  # CTk handles hover internally
        if self.action_btn.cget("state") == "normal":
            self.action_btn.config(bg=Theme.ACCENT)

    # ---------- File selection ----------

    def _browse(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        paths = filedialog.askopenfilenames(
            title="Choose video or audio file(s)",
            filetypes=SUPPORTED_FILETYPES,
        )
        if paths:
            self._add_inputs(paths)

    def _on_drop(self, event) -> None:  # type: ignore[no-untyped-def]
        if self._worker and self._worker.is_alive():
            return
        paths = _parse_drop_data(event.data)
        if paths:
            self._add_inputs(paths)

    def _add_inputs(self, paths) -> None:  # type: ignore[no-untyped-def]
        """Append the given paths to the queue, validating and deduping.

        Accepts an iterable of `str` or `Path`. Unsupported / missing
        files trigger a single popup at the end describing what was
        skipped. Files already in the queue (resolved absolute path) are
        silently ignored.
        """
        if self._worker and self._worker.is_alive():
            return

        accepted: list[Path] = []
        rejected: list[tuple[str, str]] = []
        existing_resolved: set[str] = {
            str(p.resolve()) for p in self._input_paths
        }
        for entry in paths:
            p = Path(entry)
            if not p.exists():
                rejected.append((p.name, "file not found"))
                continue
            ext = p.suffix.lower()
            if ext not in SUPPORTED_INPUT_EXTS:
                rejected.append(
                    (p.name, f"unsupported type {ext or '(no extension)'}")
                )
                continue
            key = str(p.resolve())
            if key in existing_resolved:
                continue
            existing_resolved.add(key)
            accepted.append(p)

        if accepted:
            # Adding files overrides any pasted URL.
            if self.url_var.get():
                self.url_var.set("")  # triggers _on_url_changed
            self._input_paths.extend(accepted)
            self._last_output = None
            self.progress["value"] = 0

        self._refresh_drop_display()
        n = len(self._input_paths)
        if n == 1:
            self.status_var.set("Ready. Click the button below.")
        elif n > 1:
            self.status_var.set(
                f"{n} files queued. Click the button below."
            )
        self._refresh_action_enabled()
        self._refresh_format_combo()
        self._refresh_save_dir_display()

        if rejected:
            body = "\n".join(f"\u2022 {name}: {reason}" for name, reason in rejected)
            if accepted:
                messagebox.showwarning(
                    APP_TITLE, f"Skipped {len(rejected)} file(s):\n\n{body}"
                )
            else:
                supported_str = ", ".join(sorted(SUPPORTED_INPUT_EXTS))
                messagebox.showerror(
                    APP_TITLE,
                    f"No files accepted:\n\n{body}\n\n"
                    f"Supported types: {supported_str}",
                )

    def _add_folder_via_dialog(self) -> None:
        """Pick a folder and queue every supported media file inside it.

        Walks recursively so deeply-nested batches (e.g. per-model
        subfolders from an external downloader) are picked up in one
        shot. The actual validation / dedupe is delegated to
        `_add_inputs`, which already handles unsupported extensions
        and duplicates cleanly.
        """
        if self._worker and self._worker.is_alive():
            return
        chosen = filedialog.askdirectory(
            title="Choose a folder to queue all media from",
            mustexist=True,
        )
        if not chosen:
            return
        root = Path(chosen)
        if not root.is_dir():
            return
        # Pre-filter to supported extensions so _add_inputs doesn't have
        # to scream about every JSON / thumbnail / README it'd otherwise
        # see in a downloader's output folder.
        found: list[Path] = []
        for p in root.rglob("*"):
            try:
                if p.is_file() and p.suffix.lower() in SUPPORTED_INPUT_EXTS:
                    found.append(p)
            except OSError:
                continue
        if not found:
            supported_str = ", ".join(sorted(SUPPORTED_INPUT_EXTS))
            messagebox.showinfo(
                APP_TITLE,
                f"No supported media found in:\n{root}\n\n"
                f"Looking for: {supported_str}",
            )
            return
        # Sort so the queue order is predictable across platforms.
        found.sort()
        self._add_inputs(found)

    def _clear_queue(self) -> None:
        """Empty the file queue (right-click 'Clear queue' on drop zone)."""
        if self._worker and self._worker.is_alive():
            return
        if not self._input_paths:
            return
        self._input_paths = []
        self._last_output = None
        self.progress["value"] = 0
        self._refresh_drop_display()
        self.status_var.set("Drop a file or paste a URL to get started.")
        self._refresh_action_enabled()
        self._refresh_save_dir_display()

    def _reset_inputs_post_job(self) -> None:
        """Clear the queue / URL / drop-zone after a successful job.

        Preserves `_last_output` and the 'Open Folder' button text so
        the finished result stays one click away, plus the status line
        and progress bar so the success message stays visible. The
        'Open Folder' state is released by the action-label refreshers
        as soon as fresh input arrives.
        """
        if self._worker and self._worker.is_alive():
            return
        self._input_paths = []
        if self.url_var.get():
            # Trace on url_var fans out to drop display + save dir +
            # action label/enable refreshes.
            self.url_var.set("")
        else:
            self._refresh_drop_display()
            self._refresh_save_dir_display()

    # ---------- Main action ----------

    def _on_action(self) -> None:
        label = self.action_btn.cget("text")
        if label == "Open Folder":
            self._open_output_folder()
            return
        if label == "Cancel":
            self._cancel_running_job()
            return
        self._start_job()

    def _cancel_running_job(self) -> None:
        """Set the cancel token + reflect it in the UI immediately.

        The worker thread itself takes a beat to unwind (ffmpeg has to
        actually die, the next stage check has to fire), so we don't
        wait — the action button flips to "Cancelling..." and the
        worker emits its own "Cancelled" status when it bails."""
        token = self._cancel_token
        if token is None:
            return
        token.cancel()
        try:
            self.action_btn.configure(text="Cancelling...", state="disabled")
        except tk.TclError:
            pass
        self.status_var.set("Cancelling...")

    def _start_job(self) -> None:
        url_text = self.url_var.get().strip()
        url_mode = bool(url_text)

        if not url_mode and not self._input_paths:
            return

        if url_mode and not is_url(url_text):
            messagebox.showerror(
                APP_TITLE,
                "That doesn't look like a URL. Make sure it starts with "
                "http:// or https:// and points to a video page.",
            )
            return

        # In URL mode, any/none of swears/slurs/transcript are fine - "no
        # options" means a pure download. For local files, require at
        # least one operation to be selected.
        if not url_mode and not (
            self.remove_swears.get()
            or self.remove_slurs.get()
            or self.save_transcript.get()
        ):
            messagebox.showwarning(
                APP_TITLE,
                "Tick at least one option (Swears, Slurs, or Save transcript) "
                "before continuing.",
            )
            return

        # Stash the current value of the active quality kind back into
        # its bucket, in case the user typed into the combo without
        # firing <<ComboboxSelected>>.
        self._on_quality_selected()

        self._config["fun_voice"] = self._fun_voice_id
        self._config["downsize_preset"] = self._downsize_key()
        self._config["retro_audio"] = self.retro_audio_var.get()
        save_config(self._config)

        options = pipeline.CensorOptions(
            remove_swears=self.remove_swears.get(),
            remove_slurs=self.remove_slurs.get(),
            mode=self.mode.get(),
            save_transcript=self.save_transcript.get(),
            fuzzy_matching=self.fuzzy_matching.get(),
            download_format=self.download_format_var.get(),
            output_dir=self._output_dir,
            video_quality=self._last_video_quality,
            audio_quality=self._last_audio_quality,
            # Only meaningful in URL mode; pipeline ignores it for
            # local-file jobs since `download.download` isn't called.
            cookies_file=self._cookies_file,
            fun_voice=self._fun_voice_id,
            elevenlabs_api_key=self._elevenlabs_api_key or None,
            downsize_preset=self._downsize_key(),
            retro_audio=self.retro_audio_var.get(),
            model_size=_safe_whisper_model_size(
                self._config.get("whisper_model_size")
            ),
        )

        # Snapshot the work list. URL mode is always a single-item job;
        # batch mode walks a copy of the queue so user-driven additions
        # mid-run don't disturb the in-flight batch.
        if url_mode:
            items: list[tuple[str, str | None, Path | None]] = [
                ("url", url_text, None)
            ]
        else:
            items = [("file", None, p) for p in list(self._input_paths)]
        total = len(items)

        # Freeze stage ranges to the mode this job was started in so
        # later UI changes don't warp the progress bar mid-run.
        self._active_ranges = self._stage_ranges_for_current_job()

        self._cancel_token = censor_cancel.CancelToken()
        self._set_action_working("Cancel")
        self.progress["value"] = 0
        self.status_var.set("Starting..." if total == 1 else f"Starting batch of {total}...")

        events = self._events
        cancel_token = self._cancel_token

        def worker() -> None:
            results: list[tuple[Path | str, pipeline.CensorResult | None, str | None]] = []
            for idx, (kind, url, path) in enumerate(items):
                if cancel_token.cancelled:
                    # Bail out of the remainder of the batch instantly
                    # once the user hits Cancel.
                    results.append(
                        (path or (url or ""), None, "Cancelled by user.")
                    )
                    continue
                name = path.name if path is not None else (url or "")

                def cb(stage, frac, msg, _idx=idx, _name=name):  # type: ignore[no-untyped-def]
                    events.put(("progress", _idx, total, _name, stage, frac, msg))

                try:
                    if kind == "url":
                        result = pipeline.run(
                            input_path=None,
                            output_path=None,
                            options=options,
                            progress=cb,
                            url=url,
                            cancel_token=cancel_token,
                        )
                        src: Path | str = url or ""
                    else:
                        out = pipeline.auto_output_path(
                            path, options.output_dir  # type: ignore[arg-type]
                        )
                        result = pipeline.run(
                            input_path=path,
                            output_path=out,
                            options=options,
                            progress=cb,
                            url=None,
                            cancel_token=cancel_token,
                        )
                        src = path  # type: ignore[assignment]
                    results.append((src, result, None))
                except censor_cancel.PipelineCancelled:
                    results.append(
                        (path or (url or ""), None, "Cancelled by user.")
                    )
                except Exception as e:  # noqa: BLE001
                    results.append((path or (url or ""), None, str(e)))
            events.put(("batch_done", results))

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()

        # The drain loop may have been on its slow-idle schedule (up to
        # 1s away). Reschedule it now so the first progress event from
        # the worker is picked up quickly.
        if self._drain_after_id is not None:
            try:
                self.root.after_cancel(self._drain_after_id)
            except tk.TclError:
                pass
            self._drain_after_id = None
        self._drain_after_id = self.root.after(
            self._ACTIVE_DRAIN_MS, self._drain_events
        )

    # ---------- Event pump ----------

    # Stage ranges used when the source is a local file (no download stage).
    _STAGE_RANGES_LOCAL = {
        "extract": (0.0, 0.05),
        "transcribe": (0.05, 0.78),
        "match": (0.78, 0.80),
        "transcript": (0.80, 0.82),
        "render": (0.82, 0.97),
        "post": (0.97, 1.00),
        "done": (1.0, 1.0),
    }
    # When downloading, the download takes meaningful time; carve out a slice.
    _STAGE_RANGES_DOWNLOAD = {
        "download": (0.0, 0.20),
        "extract": (0.20, 0.23),
        "transcribe": (0.23, 0.80),
        "match": (0.80, 0.81),
        "transcript": (0.81, 0.83),
        "render": (0.83, 0.97),
        "post": (0.97, 1.00),
        "done": (1.0, 1.0),
    }
    # Download-only mode: the download fills the whole bar.
    _STAGE_RANGES_DOWNLOAD_ONLY = {
        "download": (0.0, 1.0),
        "done": (1.0, 1.0),
    }
    _STAGE_RANGES_DOWNLOAD_ONLY_POST = {
        "download": (0.0, 0.92),
        "post": (0.92, 1.0),
        "done": (1.0, 1.0),
    }

    def _stage_ranges_for_current_job(self) -> dict:
        url_mode = bool(self.url_var.get().strip())
        if not url_mode:
            return self._STAGE_RANGES_LOCAL
        will_process = (
            self.remove_swears.get()
            or self.remove_slurs.get()
            or self.save_transcript.get()
        )
        if will_process:
            return self._STAGE_RANGES_DOWNLOAD
        # Pure download: optional finalize pass uses the "post" slice.
        if getattr(self, "retro_audio_var", None) and self.retro_audio_var.get():
            return self._STAGE_RANGES_DOWNLOAD_ONLY_POST
        try:
            if self._downsize_key() != "none":
                return self._STAGE_RANGES_DOWNLOAD_ONLY_POST
        except Exception:  # noqa: BLE001
            pass
        return self._STAGE_RANGES_DOWNLOAD_ONLY

    def _drain_events(self) -> None:
        self._drain_after_id = None
        drained_any = False
        try:
            while True:
                event = self._events.get_nowait()
                drained_any = True
                kind = event[0]
                if kind == "progress":
                    _, idx, total, name, stage, frac, msg = event
                    ranges = getattr(
                        self, "_active_ranges", self._STAGE_RANGES_LOCAL
                    )
                    lo, hi = ranges.get(stage, (0.0, 1.0))
                    # Each file owns a 1/N slice of the overall bar.
                    file_slice = 1.0 / max(1, total)
                    overall = (
                        idx * file_slice
                        + file_slice * (lo + (hi - lo) * max(0.0, min(1.0, frac)))
                    )
                    self.progress["value"] = overall * 100.0
                    if total > 1:
                        self.status_var.set(
                            f"[{idx + 1}/{total}] {name} \u2014 {msg}"
                            if name else f"[{idx + 1}/{total}] {msg}"
                        )
                    else:
                        self.status_var.set(msg)
                elif kind == "batch_done":
                    _, results = event
                    self._on_batch_done(results)
        except queue.Empty:
            pass
        # Reschedule adaptively. When nothing is happening we drop to
        # ~1 wakeup/sec, which is invisible to the user but stops the
        # main thread from busy-polling at 10 Hz forever.
        worker_alive = bool(self._worker and self._worker.is_alive())
        interval = (
            self._ACTIVE_DRAIN_MS
            if (worker_alive or drained_any)
            else self._IDLE_DRAIN_MS
        )
        self._drain_after_id = self.root.after(interval, self._drain_events)

    def _render_single_status(self, result: pipeline.CensorResult) -> str:
        """Compose the status message for a single completed result."""
        was_url_job = bool(self.url_var.get().strip())
        any_processing = (
            self.remove_swears.get()
            or self.remove_slurs.get()
            or self.save_transcript.get()
        )

        if result.output_path is None and result.transcript_path is not None:
            msg = f"Transcript saved: {result.transcript_path.name}"
        elif was_url_job and not any_processing and result.output_path is not None:
            msg = f"Downloaded: {result.output_path.name}"
        elif result.output_path is not None and result.flagged_count == 0:
            msg = (
                f"No flagged words found. Saved a clean copy: "
                f"{result.output_path.name}"
            )
        elif result.output_path is not None:
            msg = (
                f"Done. Censored {result.flagged_count} word(s) -> "
                f"{result.output_path.name}"
            )
        else:
            msg = "Done."
        if result.transcript_path is not None and result.output_path is not None:
            msg += f"\nTranscript: {result.transcript_path.name}"
        return msg

    def _on_batch_done(
        self,
        results: list[tuple[object, pipeline.CensorResult | None, str | None]],
    ) -> None:
        # Cancel token only lives for the duration of one batch.
        self._cancel_token = None

        # Detect a user-initiated cancel and short-circuit the regular
        # success/failure flow: the user already knows what happened
        # and a "Censor failed" alert would just be noise.
        was_cancelled = any(
            err == "Cancelled by user." for _, _, err in results
        )
        if was_cancelled:
            self.progress["value"] = 0
            self.status_var.set("Cancelled.")
            self._enable_action_btn(self._intended_action_label())
            return

        self.progress["value"] = 100.0

        successes = [(src, r) for src, r, err in results if r is not None]
        failures = [(src, err) for src, _r, err in results if err is not None]

        if successes:
            last = successes[-1][1]
            self._last_output = last.output_path or last.transcript_path

        if len(results) == 1:
            # Single-file or URL job: keep the focused status string.
            _src, result, err = results[0]
            if err is not None:
                # Don't reset the queue on total failure - the user may
                # just want to fix something and retry the same file.
                self._on_error(err)
                return
            self.status_var.set(self._render_single_status(result))  # type: ignore[arg-type]
            self._enable_action_btn("Open Folder")
            self._reset_inputs_post_job()
            return

        # Batch summary
        total_flagged = sum(r.flagged_count for _, r in successes)
        out_count = sum(1 for _, r in successes if r.output_path is not None)
        tr_count = sum(1 for _, r in successes if r.transcript_path is not None)

        parts = [f"Processed {len(successes)} of {len(results)} file(s)."]
        if total_flagged:
            parts.append(f"Censored {total_flagged} word(s) total.")
        if out_count:
            parts.append(f"{out_count} media output(s).")
        if tr_count:
            parts.append(f"{tr_count} transcript(s).")
        if failures:
            parts.append(f"{len(failures)} failed.")
        self.status_var.set(" ".join(parts))

        if successes:
            self._enable_action_btn("Open Folder")
            # At least one file made it through - clear the queue so the
            # next drop/paste lands on a fresh canvas. 'Open Folder'
            # stays put until the user queues something new.
            self._reset_inputs_post_job()
        else:
            # Total batch failure: keep the queue intact for retry.
            self._enable_action_btn(self._intended_action_label())

        if failures:
            body = "\n\n".join(
                f"\u2022 {Path(str(p)).name}: {err}" for p, err in failures[:10]
            )
            if not successes:
                messagebox.showerror(APP_TITLE, f"All files failed:\n\n{body}")
            else:
                messagebox.showwarning(
                    APP_TITLE, f"{len(failures)} file(s) failed:\n\n{body}"
                )

    def _on_error(self, msg: str) -> None:
        self.progress["value"] = 0
        self.status_var.set("Failed.")
        self._enable_action_btn(self._intended_action_label())
        messagebox.showerror(APP_TITLE, msg)

    def _open_output_folder(self) -> None:
        if self._last_output is None:
            return
        folder = self._last_output.parent
        try:
            if sys.platform.startswith("linux"):
                subprocess.Popen(["xdg-open", str(folder)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            elif sys.platform.startswith("win"):
                subprocess.Popen(["explorer", str(folder)])
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP_TITLE, f"Could not open folder:\n{e}")

    # ---------- Main loop ----------

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    CensorApp().run()


if __name__ == "__main__":
    main()
