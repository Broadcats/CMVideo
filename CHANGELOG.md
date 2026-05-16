# Changelog

All notable changes to CMVideo are recorded here. The project follows
[Semantic Versioning](https://semver.org/) once it leaves the alpha series.

## [0.4.9-alpha] - 2026-05-16

Mini-service overhaul: real progress bar + a fix for the "stuck on
'Pulling MP4...' forever" reports on hot-link-protected sites.

### Added

- **Async job model + progress bar in the mini.** `POST /api/process`
  now accepts `?async=1` and returns `{job_id}`; the frontend polls
  `GET /api/jobs/{id}` (`stage`, `pct`, `ready`, `error`) every 700 ms
  and renders a real progress bar with stage labels (`Pulling
  source...` -> `Transcribing audio...` -> `Rendering output...`).
  yt-dlp's progress hook drives the fetch stage, faster-whisper's
  segment timestamps drive transcription, and ffmpeg's
  `-progress pipe:1` drives rendering. The synchronous endpoint is
  preserved for backwards compatibility.

### Fixed

- **403 from hot-linked CDNs (thisvid.com, the *.tube family, most
  porn-CDN dragnet vendors).** The Playwright extractor was
  capturing the manifest URL but throwing away the request headers
  Chromium sent, so ffmpeg's follow-up GET arrived without the
  Referer / Cookie / Origin the CDN was checking and got rejected.
  We now snapshot per-candidate request headers AND the page's
  cookie jar, then replay them with `-user_agent / -referer /
  -headers / Cookie:`. Same fix applies to the LLM-assisted tier.
- Playwright tier now retries the next two highest-scored
  candidates if the primary 403s/404s, which catches sites that
  serve a tracking-pixel mp4 ahead of the real manifest.

## [0.4.8-alpha] - 2026-05-16

UI hotfix on top of 0.4.7. The wordmark has full breathing room,
text is readable from across a desk, and the three power-user
actions (API key, cookies, plugins) are now both buttons AND a
right-click-anywhere menu.

### Added

- **Right-click anywhere** in the window opens a tools menu: set /
  clear ElevenLabs API key, pick / clear cookies file, get the
  cookies browser extension, open the plugins folder, paywall help,
  and Paste URL. Bound globally so it works no matter what you
  click on. The URL entry and drop-zone keep their own more-
  specific menus (cut / copy / paste / clear queue).
- **Toolbar row** above the Censor button with three explicit
  buttons: "ElevenLabs API key" (accent), "Cookies extension",
  and "Plugins". They mirror the right-click menu's headline
  actions for users who don't think to right-click.

### Changed

- Body / section / status / drop-sub fonts each bumped another pt
  (now 13 / 12 / 12 / 12) so labels read clearly on 1080p displays
  from a normal viewing distance.
- Wordmark generator now leaves ~45 % cap-height of clean pixels
  above the camera silhouette and the header packs ``pady=(8, 10)``
  so the camera silhouette never kisses the title bar on any WM.

## [0.4.7-alpha] - 2026-05-16

UX + perf hotfix on top of 0.4.6. Cancel button, lighter UI, and
the website wordmark + drop-down lag are gone.

### Added

- **Cancel button.** While a job is running the action button flips
  to "Cancel". Clicking it sets a cooperative cancel token observed
  at every pipeline stage boundary AND terminates the in-flight
  ffmpeg / ffprobe / yt-dlp subprocess, so a 5-minute encode dies
  in well under a second instead of waiting for the pass to finish.

### Changed

- Renamed "Fun (retro robotic TTS saying PG words)" to just **"TTS"**
  in both the desktop options panel and the website feature copy. The
  voice combo label became "Voice".
- Wordmark generator now leaves ~22 % cap-height of clean pixels
  above the camera silhouette so it can never kiss the title bar.
  Header loads the compact 256 wordmark by default.
- App padding tightened from 20 / 16 px to 14 / 10 px; options card
  border dropped; drop-zone halo trimmed from 2 px to 1 px. Body /
  section / drop-sub fonts each bumped 1 pt so the text fills the
  taller CTk widgets.

### Fixed

- **The lag.** Window-resize handler is now debounced and skips work
  when the width hasn't actually changed. The accent-strip gradient
  was being repainted with ~860 ``create_line`` round-trips per
  Configure event; rewrote it to draw a single ``PhotoImage.put`` and
  cache the resulting bitmap, ~50× faster.

## [0.4.6-alpha] - 2026-05-16

UI overhaul to match the website, livestream-style ElevenLabs TTS
voices, the website wordmark baked into the desktop header, and a small
security audit on cmvideo.online.

### Added

- **Brand wordmark in the header.** The "Clean My V[camera]deo"
  wordmark from cmvideo.online is pre-rendered to transparent PNGs in
  `assets/wordmark/` and loaded at startup. Falls back to the icon +
  text combo when the assets aren't bundled.
- **Six ElevenLabs livestream voices** in the Fun voice list (Brian,
  Adam, Sam, Rachel, Antoni, Domi). Right-click the URL field -> "Set
  ElevenLabs API key..." to enable. Synth results are cached on disk
  per voice + word so repeat runs never touch the network. Failures
  fall back transparently to the espeak Klatt voices.

### Changed

- **CustomTkinter for the entire UI.** Drop-downs (Format, Quality,
  Fun voice, File size), radios (Silence/Beep/Fun), checkboxes, the
  primary action button, and the progress bar are all CTk widgets now,
  matching the website's dark/rounded palette. Drag-and-drop survives
  via the `CTkDnD` recipe; the legacy ttk fallback ships if
  CustomTkinter is missing on the host.
- **Site security headers.** `index.html` now sets `Permissions-Policy`
  and `X-Content-Type-Options` via meta. The two `innerHTML` writes in
  `app.js` were replaced with `createElement` + `textContent`
  defense-in-depth (no XSS surface today, but no future regression
  surface either).

### Fixed

- Fun voice combo's `(needs API key)` decoration now updates live when
  the user pastes a key into Settings, no app restart required.

## [0.4.5-alpha] - 2026-05-16

Brand polish, a new **FUN** section in the desktop app, and two new
post-process effects: ten selectable Fun-mode voices and a "Retro
audio" colour you can layer on top of any output.

### Added

- **FUN section** in the options panel, separate from REMOVE / REPLACE
  WITH / EXTRAS / OUTPUT. Houses the new voice picker and the Retro
  audio toggle.
- **Ten Fun-mode voices.** When Fun mode is selected the new "Fun voice"
  combo offers Classic / Bright / Deep / Alt Klatt plus six regional
  English variants (US, UK, Scotland, RP, Lancashire, West Midlands).
  Choice is remembered per-machine in `config.json` (`fun_voice`).
- **Retro audio** toggle. Adds a lo-fi bit-reduction colour to the audio
  track of any output - downloads, censored renders, transcripts. Works
  on every video and audio container CMVideo writes. State is persisted.
- **Smaller-file presets.** New "File size" picker in OUTPUT with four
  choices: Original / Small / Medium / Large. Re-encodes video with
  libx264 (or libvpx-vp9 for WebM) and the matching audio bitrate;
  audio-only outputs use a sensible bitrate ladder. Lossless containers
  (`wav` / `flac`) ignore the picker for downsize but still accept the
  Retro audio colour.
- **Logo in the header.** The app now shows the bundled CMVideo icon
  next to the "Clean My Video" wordmark, matching the website brand.

### Changed

- `whisper_model_size` from `config.json` is now whitelisted before it
  reaches `WhisperModel(...)` so a hand-edited config can't point the
  loader at an arbitrary path. Unknown values fall back to `small`.
- Pipeline progress bar gets a dedicated `post` slice for the optional
  finalize pass; bar no longer jumps from "rendering" to "done" when a
  retro / downsize pass is queued.
- Fun-mode TTS now resolves the espeak `-v` voice from
  `CensorOptions.fun_voice`, with a graceful fallback to the default
  Klatt voice when an unknown id is stored.

### Fixed

- Header brand fall-back: when no icon ships with the bundle, the
  wordmark text renders on its own instead of leaving a blank header.

## [0.4.4-alpha] - 2026-05-16

Adds a multi-tool extractor fallback chain so URLs that yt-dlp
struggles with get a second, third and fourth chance from
specialised tools, plus targeted desktop-app performance fixes.

### Added

- **Multi-extractor fallback chain** for both desktop and mini,
  routing URLs through up to eight tiers in sequence:
  yt-dlp -> gallery-dl -> Cobalt -> lux -> you-get -> streamlink
  -> Playwright -> LLM. Each tier targets a different class of
  site: yt-dlp covers the long tail of named extractors,
  gallery-dl handles social media gallery pages (Tumblr, Twitter,
  Reddit), Cobalt handles hand-tuned anti-bot sites, lux and
  you-get cover East-Asian portals (Bilibili, Douyin, Iqiyi),
  streamlink captures live streams, Playwright is the universal
  HTML5-player fallback, and the LLM tier reasons over the page
  when nothing else can.
- **LLM-assisted Tier 8 extractor** (opt-in). Configured via
  `CMVIDEO_LLM_BASE_URL` / `CMVIDEO_LLM_API_KEY` /
  `CMVIDEO_LLM_MODEL` env vars. Works with any OpenAI-compatible
  endpoint - Groq's free tier is the recommended provider. When
  the seven traditional tiers fail, this tier opens the page in
  Playwright, captures the full DOM and network log, and asks an
  LLM to identify the video's manifest URL. Hardened with SSRF
  guards, a CDN-domain allowlist, an anti-hallucination check
  (LLM URL must appear verbatim in the network log), a confidence
  floor of 0.4, and prompt-input caps. Disabled-by-default - the
  dispatcher silently skips it if env vars are unset, so nothing
  changes for users who don't wire up an API key.
- **Memory guard** for Playwright (Tier 7) and the LLM tier (also
  Playwright-backed). A `psutil`-driven check prevents either
  tier from launching headless Chromium on machines with less
  than 600 MB free RAM. A bounded semaphore caps concurrent
  Chromium instances at 1 to keep memory pressure predictable.
- **/api/limits diagnostics** on the mini now expose
  per-extractor availability and version, plus live free-memory
  numbers and the LLM tier's configuration state.
- **107-site stress test** (`scripts/stress/`) that exercises the
  full chain against a curated list spanning mainstream video,
  news, Asian portals, adult tubes, social media and live
  streams. Run with `python scripts/stress/stress_extractors.py`
  to baseline coverage and track regressions.

### Changed

- **Mini caps relaxed**: max clip duration raised from 30 min to
  60 min, max output filesize raised from 200 MB to 800 MB,
  /api/info rate limit raised from 30/hour to 120/hour.
- **FPS picker** added to the desktop app (24/30/48/60 fps) and
  the mini (30/60 fps for download mode). For download mode the
  picker maps to a yt-dlp format filter that prefers a matching
  fps without forcing a re-encode; for censor mode the value is
  passed to ffmpeg's `-r` flag (audio is already re-encoded so
  the cost is negligible).
- **YouTube transcript fetching** more robust: catches
  `YouTubeTranscriptApiException`, generic exceptions, and the
  newer 1.x cookie / parser failures - all degrade to a
  user-friendly 502 instead of a 500 stack trace.
- **Cobalt adapter** updated for the v10+ POST endpoint and JWT
  flow, and now disabled by default (requires self-host with
  `COBALT_API_BASE` + `COBALT_API_KEY` env vars).

### Performance

- **URL paste debounced** to 80 ms in the desktop app. Before,
  every keystroke fired six UI refresh methods, so pasting a
  100-char URL triggered 600 widget updates. Now coalesced to
  one update per burst.
- **faster-whisper model cached** in a thread-safe singleton
  keyed on (model_size, device, compute_type). A batch of N
  files now pays the 1-2s model-load cost once, not N times.
- **Background pre-warm** of the Whisper model on app start so
  the first job starts transcribing instantly. Failures are
  non-fatal; if the pre-warm crashes the first job just pays
  the load cost normally.
- **Cold init** down from ~325 ms to ~275 ms.

### Fixed

- Linux desktop integration (`.desktop` file install via
  `--install-shortcut`, WM_CLASS now matches the desktop entry,
  taskbar icon is no longer generic).
- Windows taskbar icon grouping via
  `SetCurrentProcessExplicitAppUserModelID`.

## [0.4.3-alpha] - 2026-05-15

Maintenance release. Minor internal asset and config-store tweaks; no
user-facing behaviour changes versus 0.4.2-alpha.

### Added (out-of-tree, alongside 0.4.3)

- **CMVideo Mini** - a browser-only "URL &rarr; MP4 / MP3" slice of the
  full app, designed to deploy as a free Hugging Face Space. Sources
  live in [`web-mini/`](web-mini/). Capped to 720p / 192 kbps / 30 min
  / 200 MB / 5 downloads per hour per IP so it stays free and keeps
  the desktop app the obvious next step. The cmvideo.online landing
  page now links to it from the hero and from a dedicated "Try in the
  browser" section.

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
