# Changelog

All notable changes to CMVideo are recorded here. The project follows
[Semantic Versioning](https://semver.org/) once it leaves the alpha series.

## [0.4.3-alpha] - 2026-05-15

Maintenance release. Minor internal asset and config-store tweaks; no
user-facing behaviour changes versus 0.4.2-alpha.

## [0.4.2-alpha] - 2026-05-15

Maintenance release. No user-facing behaviour changes versus
0.4.1-alpha.

## [0.4.1-alpha] - 2026-05-15

Maintenance release that broadens the format / quality matrix and rolls
the bundled Windows .exe and Linux AppImage off the same source tree.
Older versions stay available on the
[releases page](https://github.com/Broadcats/CMVideo/releases) - this
release does not change any wordlists or runtime behaviour for existing
inputs.

### Added

- **Six more output formats** for URL downloads and censor runs:
  - Video: `mkv`, `webm`, `avi`, `flv` (joining `mp4` / `mov`).
  - Audio: `m4a`, `opus`, `flac` (joining `mp3` / `ogg` / `wav`).
  Each format uses a codec that the container actually supports, so the
  written file plays back without a "this container can't hold that
  codec" stumble (e.g. webm gets Opus audio, avi gets MP3, mkv accepts
  anything, flac/wav stay lossless).
- **Full video quality ladder**: `Best`, `4K (2160p)`, `1440p`, `1080p`,
  `720p`, `480p`, `360p`, `240p`, `144p`, `Worst`. Caps `bestvideo`
  selection by height; `Worst` swaps in `worstvideo+worstaudio` for the
  smallest stream the site offers.
- **Full audio quality ladder**: `Best`, `320 kbps`, `256 kbps`, `192
  kbps`, `160 kbps`, `128 kbps`, `96 kbps`, `64 kbps`, `48 kbps`,
  `Worst`. Lossless containers (`wav` / `flac`) display "Lossless" and
  ignore the kbps choice.
- **All new formats accepted as inputs** too - drag-and-drop, browse
  dialog and the right-click "Add all files from folder..." action all
  walk the expanded extension list (`.mp4 .mov .mkv .webm .avi .flv
  .mp3 .m4a .aac .ogg .opus .wav .flac`).
- Legacy quality labels (`High (192k)`, raw `192` etc.) still resolve so
  any saved config from 0.4.0-alpha keeps working.

## [0.4.0-alpha] - 2026-05-15

First public download. Everything in this release is feature-complete but
considered alpha quality - expect rough edges and breaking changes before 1.0.

### Added

- **Windows x64 executable** (`CMVideo-0.4.0-alpha-win-amd64.exe`, ~230 MB
  single file). Built on every tag via GitHub Actions (`windows-exe` job in
  `.github/workflows/release.yml`); bundles Python, ffmpeg, ffprobe,
  espeak-ng (with voice data), and the same Python stack as the AppImage.
  CPU-only; uses `scripts/build-windows.ps1` locally on a Windows machine.
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
  - `Fun` - drop a retro robotic TTS replacement (via `espeak-ng`).
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

- macOS ships as source only (instructions mirror Linux); no signed `.app`
  bundle yet and nothing has been validated on Apple hardware.
- Fun mode CPU spend scales linearly with the clip count; a 7-hour
  stream with 1,300 TTS replacements takes ~30-60 min on a modern CPU.
