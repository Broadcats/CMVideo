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

from censor import pipeline
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


class CensorApp:
    def __init__(self) -> None:
        if _DND_AVAILABLE:
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
        self._events: queue.Queue = queue.Queue()
        # after-id for the event drain, kept so a starting worker can
        # bump the next tick forward to _ACTIVE_DRAIN_MS.
        self._drain_after_id: str | None = None

        self.remove_swears = tk.BooleanVar(value=True)
        self.remove_slurs = tk.BooleanVar(value=True)
        self.mode = tk.StringVar(value="silence")
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
        self.font_tagline = _pick_font(sans, 11)
        self.font_body = _pick_font(sans, 11)
        self.font_section = _pick_font(sans, 10, "bold")
        self.font_button = _pick_font(sans, 13, "bold")
        self.font_status = _pick_font(sans, 10)
        self.font_footer = _pick_font(sans, 9)
        self.font_drop = _pick_font(sans, 13, "bold")
        self.font_drop_sub = _pick_font(sans, 10)

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
        outer.pack(fill="both", expand=True, padx=20, pady=16)

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
        ttk.Label(title_holder, text=APP_BRAND, style="Title.TLabel").pack(anchor="w")
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
        halo.pack(side="top", fill="x", pady=(0, 12))

        self.drop_card = tk.Frame(
            halo,
            bg=Theme.SURFACE,
            highlightthickness=2,
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
        self.format_combo = ttk.Combobox(
            row,
            textvariable=self.download_format_var,
            values=list(SUPPORTED_DOWNLOAD_FORMATS),
            state="readonly",
            width=6,
            style="Dark.TCombobox",
        )
        self.format_combo.pack(side="left", padx=(0, 8))

        # Quality picker (values populated by _refresh_quality_combo)
        self.quality_label = tk.Label(
            row, text="Quality", bg=Theme.BG, fg=Theme.TEXT_MUTED, font=self.font_drop_sub
        )
        self.quality_label.pack(side="left", padx=(0, 4))
        self.quality_combo = ttk.Combobox(
            row,
            textvariable=self.quality_var,
            values=list(VIDEO_QUALITY_LABELS),
            state="readonly",
            width=12,
            style="Dark.TCombobox",
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
        self.remember_cookies_check = ttk.Checkbutton(
            self.cookies_row,
            text="Remember",
            variable=self.remember_cookies,
            style="Dark.TCheckbutton",
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
        # Options card fills the leftover middle space.
        options_card = tk.Frame(
            parent,
            bg=Theme.SURFACE,
            highlightthickness=1,
            highlightbackground=Theme.BORDER,
        )
        options_card.pack(side="top", fill="both", expand=True, pady=(0, 12))

        opts_inner = tk.Frame(options_card, bg=Theme.SURFACE)
        opts_inner.pack(fill="x", padx=18, pady=16, anchor="n")

        ttk.Label(opts_inner, text="REMOVE", style="Section.TLabel").pack(anchor="w")
        ttk.Checkbutton(
            opts_inner,
            text="Swears",
            variable=self.remove_swears,
            style="Dark.TCheckbutton",
        ).pack(anchor="w", pady=(6, 0))
        ttk.Checkbutton(
            opts_inner,
            text="Racial slurs",
            variable=self.remove_slurs,
            style="Dark.TCheckbutton",
        ).pack(anchor="w", pady=(2, 12))

        self.replace_label = ttk.Label(
            opts_inner, text="REPLACE WITH", style="Section.TLabel"
        )
        self.replace_label.pack(anchor="w")
        self.silence_radio = ttk.Radiobutton(
            opts_inner,
            text="Silence",
            variable=self.mode,
            value="silence",
            style="Dark.TRadiobutton",
        )
        self.silence_radio.pack(anchor="w", pady=(6, 0))
        self.beep_radio = ttk.Radiobutton(
            opts_inner,
            text="Beep tone",
            variable=self.mode,
            value="beep",
            style="Dark.TRadiobutton",
        )
        self.beep_radio.pack(anchor="w", pady=(2, 0))
        self.fun_radio = ttk.Radiobutton(
            opts_inner,
            text="Fun (retro robotic TTS saying PG words)",
            variable=self.mode,
            value="fun",
            style="Dark.TRadiobutton",
        )
        self.fun_radio.pack(anchor="w", pady=(2, 12))

        ttk.Label(opts_inner, text="EXTRAS", style="Section.TLabel").pack(anchor="w")
        ttk.Checkbutton(
            opts_inner,
            text="Save transcript (.txt)",
            variable=self.save_transcript,
            style="Dark.TCheckbutton",
        ).pack(anchor="w", pady=(6, 0))
        ttk.Checkbutton(
            opts_inner,
            text="Fuzzy matching (catches fucks, fuuuck, phuck, f*ck, kunt...)",
            variable=self.fuzzy_matching,
            style="Dark.TCheckbutton",
        ).pack(anchor="w", pady=(2, 0))

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
        self.progress = ttk.Progressbar(
            parent,
            style="Dark.Horizontal.TProgressbar",
            mode="determinate",
            maximum=100.0,
            value=0,
        )
        self.progress.pack(side="bottom", fill="x", pady=(0, 4))

        # Primary action button. tk.Button gives reliable colour control
        # on Linux where ttk.Button often ignores theme overrides.
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

    def _on_url_changed(self) -> None:
        """Fired whenever the URL entry text changes."""
        text = self.url_var.get().strip()
        if text:
            # URL and local-file queues are mutually exclusive sources.
            # Pasting a URL drops the queue.
            self._input_paths = []
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
        state = "!disabled" if censoring else "disabled"
        try:
            self.silence_radio.state([state])
            self.beep_radio.state([state])
            self.fun_radio.state([state])
            self.replace_label.configure(
                style="Section.TLabel" if censoring else "SectionDim.TLabel"
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
        self.action_btn.config(text=self._intended_action_label())

    # ---------- Resize handling ----------

    def _on_resize(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.widget is not self.root:
            return
        wrap = max(180, event.width - 60)
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
        accent strip canvas. Called on every <Configure> so the gradient
        rescales with the window width.
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
        c.delete("grad")
        stops = (Theme.ACCENT_LO, Theme.ACCENT_GLOW, Theme.COOL)
        n = len(stops) - 1
        for x in range(w):
            t = x / max(1, w - 1) * n
            seg = min(int(t), n - 1)
            col = self._lerp_hex(stops[seg], stops[seg + 1], t - seg)
            c.create_line(x, 0, x, h, fill=col, tags="grad")

    def _set_drop_hover(self, hover: bool) -> None:
        if self._worker and self._worker.is_alive():
            return
        color = Theme.BORDER_GLOW if hover else Theme.BORDER
        self.drop_card.config(highlightbackground=color)

    def _enable_action_btn(self, label: str = "Censor") -> None:
        self.action_btn.config(
            state="normal",
            text=label,
            bg=Theme.ACCENT,
            fg="white",
            cursor="hand2",
        )

    def _disable_action_btn(self, label: str = "Censor") -> None:
        self.action_btn.config(
            state="disabled",
            text=label,
            bg=Theme.BORDER,
            fg=Theme.TEXT_MUTED,
            cursor="arrow",
        )

    def _set_action_working(self, label: str = "Working...") -> None:
        self.action_btn.config(
            state="disabled",
            text=label,
            bg=Theme.SURFACE_HI,
            fg=Theme.TEXT_MUTED,
            cursor="watch",
        )

    def _on_btn_hover(self, _event) -> None:  # type: ignore[no-untyped-def]
        if self.action_btn.cget("state") == "normal":
            self.action_btn.config(bg=Theme.ACCENT_HI)

    def _on_btn_leave(self, _event) -> None:  # type: ignore[no-untyped-def]
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
        if self.action_btn.cget("text") == "Open Folder":
            self._open_output_folder()
            return
        self._start_job()

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

        self._set_action_working("Working...")
        self.progress["value"] = 0
        self.status_var.set("Starting..." if total == 1 else f"Starting batch of {total}...")

        events = self._events

        def worker() -> None:
            results: list[tuple[Path | str, pipeline.CensorResult | None, str | None]] = []
            for idx, (kind, url, path) in enumerate(items):
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
                        )
                        src = path  # type: ignore[assignment]
                    results.append((src, result, None))
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
        "render": (0.82, 1.00),
        "done": (1.0, 1.0),
    }
    # When downloading, the download takes meaningful time; carve out a slice.
    _STAGE_RANGES_DOWNLOAD = {
        "download": (0.0, 0.20),
        "extract": (0.20, 0.23),
        "transcribe": (0.23, 0.80),
        "match": (0.80, 0.81),
        "transcript": (0.81, 0.83),
        "render": (0.83, 1.00),
        "done": (1.0, 1.0),
    }
    # Download-only mode: the download fills the whole bar.
    _STAGE_RANGES_DOWNLOAD_ONLY = {
        "download": (0.0, 1.0),
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
        return (
            self._STAGE_RANGES_DOWNLOAD
            if will_process
            else self._STAGE_RANGES_DOWNLOAD_ONLY
        )

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
