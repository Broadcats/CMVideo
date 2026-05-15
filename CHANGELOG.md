# Changelog

All notable changes to CMVideo are recorded here. The project follows
[Semantic Versioning](https://semver.org/) once it leaves the alpha series.

## [0.4.0-alpha] - 2026-05-15

First public download. Everything in this release is feature-complete but
considered alpha quality - expect rough edges and breaking changes before 1.0.

### Added

- **Linux AppImage** (`CMVideo-0.4.0-alpha-x86_64.AppImage`, ~230 MB).
  Bundles Python 3.12, ffmpeg, ffprobe, espeak-ng and every Python dep
  (faster-whisper, ctranslate2, yt-dlp with all 1054 extractors, av,
  onnxruntime, tkinterdnd2, Pillow). Download, `chmod +x`, double-click -
  no install, no Python on the host required. Built with
  `scripts/build-appimage.sh` (PyInstaller + linuxdeploy + appimagetool).
  CPU-only by default; CUDA users keep using the source install.
- **Drag-and-drop GUI** (Tkinter + `tkinterdnd2`). Drop a video or audio
  file onto the window to queue it.
- **URL ingest**. Paste a YouTube or yt-dlp-supported URL and pick the
  download format (`mp4`, `mov`, `mp3`, `wav`, `ogg`) and quality.
- **Profanity removal**. Per-run toggles for swears and racial slurs;
  bundled lists in `wordlists/`.
- **Three censor modes**:
  - `Silence` - mute the offending interval.
  - `Beep` - overlay a 1 kHz tone.
  - `Fun` - drop a Microsoft Sam style TTS replacement (via `espeak-ng`).
- **Transcript-only mode**. Save a full uncensored `.txt` transcript
  without re-rendering the media.
- **Phonetic / leetspeak matching** - catches `fucks`, `fuuuck`, `phuck`,
  `f*ck`, `kunt`, etc., not just exact tokens.
- **Batch processing**. Drop multiple files or use the right-click
  "Add all files from folder..." option; CMVideo processes them
  sequentially with per-file progress.
- **User-installable yt-dlp plugins**. Plugin folder at
  `~/.config/cmvideo/plugins/` (or platform equivalent) - drop a
  community extractor in and CMVideo picks it up on next launch.
- **Cookies file support** for paywalled / login-gated sites
  (Patreon, members-only YouTube, etc.). Optional "remember" toggle
  persists the path in `config.json`.
- **Adaptive event loop**. Idles at ~1 wake-up per second; ramps to
  20 ms while a worker is running. Background CPU usage is negligible.
- **Cool-shade theme** - indigo / violet / cyan palette with a
  Canvas-rendered gradient strip under the header.

### Fixed

- `[Errno 7] Argument list too long: '/usr/bin/ffmpeg'` when running
  Fun mode with thousands of TTS clips. The filter graph is now spilled
  to a temp script file (`-filter_complex_script` / `-filter_script:a`)
  whenever it would exceed the kernel's 128 KB per-argv-string cap.
- CUDA failures fall back to CPU automatically; `enable-gpu.sh`
  bootstraps the CUDA runtime libraries on demand.
- `tkinterdnd2` missing is non-fatal - the Browse button still works.
- Right-click context menus stay open on release instead of requiring
  the button to be held.

### Known issues

- Standalone Windows / macOS builds are still source-archive only. Linux
  has the AppImage; equivalent Windows `.exe` and macOS `.app` tracked
  for 0.5.0.
- macOS launchers exist but are untested.
- Fun mode CPU spend scales linearly with the clip count; a 7-hour
  stream with 1,300 TTS replacements takes ~30-60 min on a modern CPU.
