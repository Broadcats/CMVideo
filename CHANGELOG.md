# Changelog

All notable changes to CMVideo are recorded here. The project follows
[Semantic Versioning](https://semver.org/) once it leaves the alpha series.

## [0.4.13.3-alpha] - 2026-05-16

Mini-app: second resolution fix. v0.4.13.2 fixed yt-dlp's selector
but tube-style sites (thisvid, etc.) don't actually go through
yt-dlp - they fall through to the multi-extractor chain and end up
at the Playwright tier. The Playwright candidate ranker had two
cascading bugs that produced low-quality output regardless of
which height the user picked.

### Fixed

- **Playwright scorer is now resolution-aware.** The old scorer
  ranked candidates by `(HLS, DASH, MP4, response_size)` and
  ignored resolution entirely. New `_rank_candidates` parses height
  hints out of CDN URLs (`_720p.mp4`, `?quality=1080`,
  `/resolution=1280x720`, `/720/abc.mp4`, etc) and picks the highest
  variant at or below the cap. Falls back to legacy size-based
  ranking only when no height can be inferred.
- **HLS master playlists are now resolved server-side.** When a
  captured candidate is an HLS master `.m3u8`, we fetch it,
  parse the `#EXT-X-STREAM-INF` lines (RESOLUTION, BANDWIDTH), and
  pick the variant playlist URL whose height fits the cap.
  Previously we handed ffmpeg the master and ffmpeg defaulted to
  the FIRST variant in the manifest - usually the lowest. This
  was the dominant cause of "looks like 480p regardless of choice"
  on thisvid.
- **`target_height` plumbed through the extractor chain.** Added a
  `target_height` kwarg to `extract_with_fallbacks`,
  `playwright_download`, `_playwright_download_locked`, and
  `_playwright_download_from_capture`. The user's quality choice
  now actually reaches the tier that does the work.

### Tests

10/10 height-hint extraction cases pass (covers `_720p.mp4`,
`?quality=`, `?res=`, `?resolution=NxH`, bare `/720/`, and the
opaque-URL fallback). Synthetic HLS master correctly resolves to
720p, 1080p, falls back gracefully when cap is below all
variants, and picks the highest when the cap is unset.

## [0.4.13.2-alpha] - 2026-05-16

Mini-app: fix the resolution regression. Downloads were silently
capping at the source's progressive-MP4 ceiling (360p on YouTube,
480p on a number of other sites) regardless of which height tier
the user selected, because the format selector introduced in
v0.4.11 ordered clauses by I/O cost (no-merge path first) instead
of by quality.

### Fixed

- **Format selector reordered to put quality first.**
  `_video_format_selector` now leads with
  `bestvideo*[height<=H][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]`
  - the canonical "highest video at the cap, merge with best m4a"
  pattern - across all sources. The progressive single-file no-merge
  path is still in the chain, but only fires when the progressive
  variant is actually AT the chosen height (`best[height=H]`, not
  `best[height<=H]`), so it can't sneak in below the cap any more.
- The fix applies to both `Standard (720p)` and `HD (1080p)` tiers
  and to all three modes (download, silence, beep). Censor mode
  was previously transcribing and rendering 360p sources too -
  same root cause.

### Note on speed

The 0.4.11 ordering was based on an incorrect assumption that
progressive MP4 was widely available at 720p+. For YouTube
specifically that's just wrong (everything 720p+ is split
tracks). The merge step is real but small - ~10-30 s for a typical
~150 MB file - and well under the 360 s download cap. For sources
that genuinely do ship single-file 720p / 1080p (most non-YouTube
CDNs), the no-merge path still triggers via the `[height=H]`
clauses lower in the chain, so they don't lose any speed.

## [0.4.13.1-alpha] - 2026-05-16

Mini-widget UX hotfix on top of v0.4.13-alpha.

### Fixed

- **Removed duplicate "720p" label.** The format chip was reading
  `MP4 · 720p` while the new quality chip below it also read `720p`.
  Resolution is now controlled in exactly one place; the format chip
  is now just `MP4`.
- **Quality row promoted to the top.** The 720p / 1080p chips now
  sit inline with the format chips at the top of the form,
  separated from MP3 by a vertical divider, so the quality choice
  is visible at a glance instead of buried below the fps row.
- **Quality order reversed.** `1080p · larger · slower` first,
  `720p · fastest` second, so the upgrade is the leftmost option in
  its group. Default selection is still `720p` to preserve the fast
  pull path.

### Layout

- New `.shot-fmtquality-row` flex wrapper holds both groups.
  `.shot-divider` renders the vertical bar; on very narrow phones
  (<= 380 px) the divider collapses into a horizontal hairline so
  the chips can stack cleanly.
- The divider auto-hides when MP3 is selected (it'd otherwise dangle
  next to a lonely format chip).

## [0.4.13-alpha] - 2026-05-16

Mini-app: optional **1080p HD** download tier. 720p stays the default
(it's the fast path and what makes "Preview & Pull" feel snappy);
HD is one click away when the user wants better quality and is happy
to wait for the bigger file.

### Added

- **Quality chip group** under the existing fps row: `720p ·
  fastest` (default) vs `1080p · larger · slower`. Sends a new
  `quality` form field (`standard` / `hd`) on `/api/process`.
- **Per-quality download size cap.** `standard` keeps the existing
  800 MB ceiling; `hd` lifts it to 1.5 GB to fit a 1-hour 1080p AVC
  source. Censor mode is unaffected (transcript is the bottleneck,
  not bytes).
- **HD-aware format selector.** `_video_format_selector` now takes a
  `height` arg. When 1080p is requested it tries progressive 1080p
  first, then split tracks at 1080p, then falls back through 720p
  rather than failing the job outright on sources that don't expose
  HD.
- `/api/limits` and `/api/info` now expose `qualities`,
  `default_quality`, `quality_heights`, and (on `/api/limits`)
  `quality_download_caps_mb` so the widget can stay in sync if the
  caps are tuned later.

### Performance guardrails

- **1080p + 30/60 fps override is rejected.** libx264 ultrafast at
  1080p on the Space's 2 shared vCPUs runs ~5x slower than realtime,
  which would blow the ffmpeg cap on any clip more than a couple of
  minutes long. The frontend visibly disables the override fps pills
  when HD is selected; the backend returns a 400 with a clear "use
  the desktop app for that combination" message if anyone tries the
  combination via raw API.
- HD download mode keeps the existing 360 s download timeout - HF
  egress at ~10 MB/s easily completes a 1.5 GB pull inside the
  budget, with headroom for the merge step on split-track sources.

### UI

- Caps blurb on the homepage now reads "≤1 hr / 800 MB at 720p (or
  1.5 GB at 1080p)" and lists 720p / 1080p in the quality summary.
- Inline cyan heads-up appears below the chips when 1080p is
  selected, calling out the larger file, the longer pull time, and
  why 30/60 fps is locked to Source for HD on the mini.

### Why the default is still 720p

A bumped global default would slow down everyone's first impression
of the widget - 1080p is roughly 2x the bytes of 720p and the
single-file fast path that gives us 4x speedups in 0.4.11 hits less
often at 1080p. Keeping 720p as default preserves the fast path for
the 80%+ of pulls where nobody actually cares about resolution, and
lets the people who do explicitly opt in.

## [0.4.12-alpha] - 2026-05-16

Mini-service hardening pass for "what happens when this blows up
on Reddit." All defenses are in-process, cost ~zero CPU, and tunable
via env vars so ops can dial them up under live load without a
redeploy.

### Hardening

- **Concurrency right-sized for the box.** `JOB_MAX_INFLIGHT`
  dropped from 8 to 3 (a 2-vCPU shared CPU box can't actually run 8
  concurrent ffmpeg passes without thrashing) and `JOB_MAX_PER_IP`
  from 3 to 1 (one user gets one job at a time so a single client
  can't monopolise the queue). Both env-overridable.
- **Burst limit on top of the hourly cap.** `/api/process` is now
  `5/hour AND 2/minute`; `/api/info` is `120/hour AND 10/minute`.
  Stops cheap rapid-fire submissions from eating slots.
- **LRU cache on `/api/info` (5-min TTL, 256 entries).** When the
  widget gets linked from a popular thread and N users all preview
  the same URL, only the first one hits yt-dlp - the rest get the
  same metadata back instantly with zero load on the scraper.
- **Per-IP failure cooldown.** Sliding window: `FAILURE_THRESHOLD`
  (default 5) failed submissions inside `FAILURE_WINDOW_S` (default
  600 s) puts the IP in a `FAILURE_COOLDOWN_S` (default 300 s)
  cooldown with a `Retry-After` header. Stops botnets from burning
  job slots on guaranteed-to-fail attempts.
- **Operator kill-switch.** `CMVIDEO_MINI_DISABLED=1` flips the
  service into "overload mode": all submission endpoints return 503
  with `Retry-After: 300` and a friendly nudge to the desktop app.
  `/healthz` and `/api/limits` stay alive so the operator can
  confirm the gate is up.
- **User-Agent gate.** Empty UAs get a 400; known-bad scraper UAs
  (Ahrefs, SemRush, MJ12, ByteSpider, Petalbot, Scrapy, ...) get a
  403. Real browsers / curl / wget / Python clients are unaffected.
- **`/api/limits` exposes hardening telemetry** so ops can confirm
  the gate state (`killswitch_active`, `live_jobs_now`,
  `ips_in_cooldown_now`, `info_cache_size`) without reading code.

### What this does NOT defend against

Volumetric L3/L4 floods are HF Spaces' edge to handle. Determined
botnets with rotating IPv6 /64s need a CDN-level WAF; if traffic
ever justifies it, put Cloudflare in front of `cmvideo-mini.hf.space`
and the existing per-IP defenses chain on top.

## [0.4.11-alpha] - 2026-05-16

Mini-service performance pass. Pulls feel ~2-5x faster on most
sites; the progress bar now actually tells the user what's
happening so even slow pulls feel responsive.

### Performance

- **Direct-media-URL fast path.** When a URL ends in a known media
  extension and the host returns a matching Content-Type, we stream
  the bytes ourselves with `urllib` and skip the yt-dlp init +
  scrape + post-processor entirely. Saves 3-5 s per request and
  avoids needless ffmpeg remuxing for plain `.mp4` / `.mp3` links.
- **Parallel HLS / DASH segments.** Bumped
  `concurrent_fragment_downloads` from 1 to 4. This is the single
  biggest win on Twitch / IG / TikTok / `*.tube` tubes - those serve
  HLS playlists with ~6 s segments, and 4-way parallel turns a
  10-min wall time into ~2.5 min on the same connection.
  `fragment_retries=2` + `skip_unavailable_fragments=True` add some
  robustness to the speedup.
- **Progressive MP4 first in the format selector.** Old order
  always picked `bestvideo+bestaudio` which forced an ffmpeg merge
  step. New order tries `best[ext=mp4][acodec!=none]` first - one
  HTTP GET, no merge - and only falls through to the split-track
  form when no progressive variant exists at the cap.
- **Pre-extract 16 kHz mono WAV before transcription.** Whisper
  has to do this internally before encoding; doing it once with
  ffmpeg up-front skips faster-whisper's redundant resample pass
  and shaves ~10-15% off transcription wall-clock on the free CPU.
- **Live download speed + ETA in the progress bar.** `/api/jobs/{id}`
  now returns `bytes_done`, `bytes_total`, `speed_bps`, `eta_s` and
  the widget renders `Pulling source... 47%  ·  3.2 MB/s  ·  9s left
   ·  18.4 MB / 39.2 MB`. Backed by an EMA-smoothed
  bytes/sec estimate so the rate doesn't jitter between segments.

## [0.4.10-alpha] - 2026-05-16

Polish + security maintenance pass. No new features; fewer ways to
shoot yourself in the foot.

### Added

- **Mini: explicit 8-minute censor cap.** The "Silence swears" /
  "Beep swears" chips now show the `<= 8 min` limit inline, the
  yellow heads-up banner appears any time a censor mode is selected,
  and the caps panel below the form leads with the censor cap. The
  widget also pre-flights the source duration against `/api/info`
  before submitting, so URLs over 8 min are rejected instantly
  instead of after a cold-start round-trip.

### Security

- **Mini: job IDs are now 32-byte unpredictable tokens** (was
  `uuid.uuid4().hex`, 16 bytes) generated via `secrets.token_urlsafe`.
- **Mini: per-IP scope on `/api/jobs/{id}` and `/api/jobs/{id}/file`.**
  Even a leaked job_id can't be used by anyone outside the original
  requester's network identity. Failed checks return 404 (not 403)
  to avoid leaking whether the id was ever valid.
- **Mini: per-IP inflight cap (3 jobs)** on top of the existing
  global cap (8). One client can't lock everyone else out.
- **Mini: rate limits on the new poll endpoints** (180/min on state,
  30/min on file) so a runaway client can't turn the polling loop
  into a flood.
- **App: `config.json` is now opened with `O_CREAT | 0o600`** at file
  creation time on POSIX, instead of write-then-chmod. The parent
  config dir is also clamped to `0o700`. Closes a small TOCTOU window
  during writes.
- **App: yt-dlp plugin loader refuses world- or group-writable
  plugin folders** on POSIX. Plugins are arbitrary Python under your
  account, so a writable plugins dir on a shared workstation is a
  privilege-escalation primitive. Logs a warning with the exact
  `chmod 700` command to fix it.

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
