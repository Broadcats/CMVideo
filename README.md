# CMVideo

Drag-and-drop desktop app that automatically removes swears and racial
slurs from video and audio files. Runs locally on **Windows** and **Linux**.
Uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper) on your
NVIDIA GPU (or CPU as a fallback) to transcribe with word-level
timestamps, then ffmpeg to mute, beep, or replace the flagged regions
with a robotic Microsoft-Sam-style voice.

**Supported input formats:** MP4, MOV (video) and MP3, WAV, OGG (audio).
Output preserves the input format - the video stream is copied through
without re-encoding for MP4/MOV.

**YouTube and other URLs:** paste a video URL (anything yt-dlp supports)
into the URL field and the app will download it first. You can choose
the download format (MP4 / MOV / MP3 / WAV / OGG) and quality, and
combine with any of the censor / transcript options - or leave
everything unticked to just download. Default save folder is your
Downloads directory, overridable via the **Save to** picker.

**Batch processing:** drop multiple files at once and the app walks
through them sequentially with a per-file progress bar.

---

## Install

### Windows (recommended: portable .exe)

Download `CMVideo-<version>-win-amd64.exe` from
[Releases](https://github.com/Broadcats/CMVideo/releases/latest) and double-click
it. Everything (Python, ffmpeg, espeak-ng, libraries) lives inside that one file;
nothing is installed under `Program Files` and no separate Python install is
required. The first transcription run still downloads Whisper model weights into
your user cache (~150 MB for the default `small` model).

SmartScreen may warn because the executable is not code-signed yet — use
**More info** → **Run anyway** if that happens.

Builds are produced automatically when a version tag is pushed (see
`scripts/build-windows.ps1` and `.github/workflows/release.yml`). To compile
locally on your own PC: open PowerShell in the repo folder and run
`pwsh ./scripts/build-windows.ps1`.

### Windows (optional: source + `install.ps1`)

If you want a normal Python environment (e.g. to experiment with CUDA wheels
yourself):

1. Download or clone this folder.
2. **Double-click `install.bat`** (or right-click `install.ps1` &rarr; Run
   with PowerShell).

The installer:

- Detects what's missing and installs it. Uses **winget** when
  available (Windows 10 1809+ / Windows 11). Falls back to downloading
  portable binaries into a local `bin\` folder if winget isn't usable.
- Installs **Python 3.12**, **ffmpeg**, and **espeak-ng**. Already have
  them? They're detected and skipped.
- Creates `.venv` and installs `faster-whisper`, `yt-dlp`, etc.
- Creates a Start Menu shortcut and a Desktop shortcut, both pointing
  to `run.bat` with the CMVideo icon.

No admin / UAC prompts required. Everything is installed user-scope.

> **Heads up:** If your PowerShell ExecutionPolicy blocks scripts,
> use `install.bat` (it bootstraps PowerShell with `-ExecutionPolicy
> Bypass`), or run the install command yourself:
> `powershell -ExecutionPolicy Bypass -File install.ps1`.

To uninstall: **double-click `uninstall.bat`**. The uninstaller removes
shortcuts and (with confirmation) the local `.venv` and `bin\` folder.
It does **not** uninstall system Python, ffmpeg, or espeak-ng - those
might be useful for other apps.

### Linux (recommended: AppImage)

The fastest path is the single-file [AppImage](https://github.com/Broadcats/CMVideo/releases/latest):

```
chmod +x CMVideo-0.4.0-alpha-x86_64.AppImage
./CMVideo-0.4.0-alpha-x86_64.AppImage
```

That's it. The AppImage carries Python, ffmpeg, espeak-ng and every Python
dep inside (~230 MB). No `sudo`, no `apt install`, no venv. Whisper model
weights are downloaded on first transcribe and cached under
`~/.cache/huggingface/`.

If you want to integrate it into your apps menu, drop it anywhere on disk
and double-click - tools like [`appimaged`](https://github.com/probonopd/go-appimage)
or your file manager's "open with" will register the desktop entry.

### Linux (source install)

If you prefer to use your system Python (e.g. for a GPU build), clone the
repo and run:

```
./install.sh
```

The installer detects your distribution (apt / dnf / pacman / zypper)
and installs the system packages it needs (`ffmpeg`, `python3-tk`,
`python3-venv`, `espeak-ng`) with a single `sudo` prompt. It then
registers **CMVideo** in your applications menu and drops a clickable
shortcut on your Desktop. The first launch creates a local `.venv/` and
downloads the Python deps (1-3 minutes); subsequent launches are instant.

To uninstall:
```
./uninstall.sh
```

---

## How to use

1. Click **CMVideo** in your apps menu (or the desktop icon, or
   `run.bat` / `run.sh`).
2. Drag a video/audio file (or several at once) onto the window, or
   click the box to browse. Or paste a YouTube/etc URL into the URL
   field.
3. Tick which categories to remove:
   - **Remove swears** (on by default)
   - **Remove racial slurs** (on by default)
4. Choose **Silence**, **Beep**, or **Fun**:
   - **Silence** mutes the flagged region.
   - **Beep** lays a 1 kHz tone over it.
   - **Fun** has Microsoft Sam say a random PG word (`fudge`, `darn`,
     `fiddlesticks`, `for crying out loud`...) in place of the swear.
     Powered by espeak-ng's Klatt formant synthesizer.
5. Optional extras:
   - **Save transcript (.txt)** writes an uncensored transcript with
     word-level timestamps. Flagged words are marked `[*]` so you can
     audit what was caught.
   - **Fuzzy matching** catches inflections (`fucks`, `fucking`),
     stretched letters (`fuuuck`), phonetic swaps (`phuck`, `kunt`,
     `fone`), wildcards (`f*ck`, `f**k`), and common leet substitutions
     (`b1tch`, `$hit`) without ever firing on innocent words like
     `Scunthorpe` or `count`.
6. Optional **Save to** folder: pick where final outputs go. Default
   is the input's folder (or `~/Downloads/` for URL jobs).
7. Click the big button. It re-labels itself based on what you've
   selected (e.g. `Censor (3 files)`, `Save Transcript`,
   `Download & Censor`).
8. When the progress bar finishes, click **Open Folder** to see the
   result.

Censored output is written next to the input with `_clean` inserted
before the extension, e.g. `interview.mp4` → `interview_clean.mp4`,
`podcast.mp3` → `podcast_clean.mp3`. If that name already exists, the
app suffixes with `_clean_2`, `_clean_3`, etc. The original file is
never modified.

---

## Requirements

### Windows
- Windows 10 (1809+) or Windows 11.
- Roughly 4 GB free disk space (Python + Whisper models + venv).
- An NVIDIA GPU is **recommended** (long videos go from hours-on-CPU
  to minutes-on-GPU). The installer falls back to CPU automatically if
  no GPU is detected.

### Linux
- A modern distro with apt / dnf / pacman / zypper.
- Python 3.10+ (`install.sh` installs it via your package manager).
- `ffmpeg`, `python3-tk`, `python3-venv`, `espeak-ng` (also installed
  by `install.sh`).

### GPU (optional, both platforms)
The Python deps `nvidia-cublas-cu12` and `nvidia-cudnn-cu12` are
installed automatically into the venv if `install.sh` / `install.ps1`
detects an NVIDIA GPU. If CUDA still fails at runtime for any reason,
the app falls back to CPU silently and your job still completes -
just slower.

To force a GPU install on an existing setup:
- Linux: `./enable-gpu.sh`
- Windows: `.venv\Scripts\pip install nvidia-cublas-cu12 nvidia-cudnn-cu12`

---

## Customising the wordlists

The lists are plain text files, one word per line:

- [wordlists/swears.txt](wordlists/swears.txt) - pre-populated with common English swears.
- [wordlists/slurs.txt](wordlists/slurs.txt) - **intentionally empty.** Add the specific
  terms you want auto-removed, one per line. Save the file and run the
  app again - no restart of anything else needed.

Lines starting with `#` are comments. Matching is case-insensitive and
ignores surrounding punctuation, so `Shit,` and `SHIT!` both match `shit`.

---

## How it works

```
input.mp4
   |
   v
ffmpeg extracts 16kHz mono WAV (audio only, fast)
   |
   v
faster-whisper transcribes with word timestamps (GPU or CPU)
   |
   v
Each transcribed word is matched against the active wordlists
   |   (exact match by default; fuzzy regex if "Fuzzy matching" is on)
   v
intervals = padded + merged timestamps of flagged words
   |
   v
ffmpeg single-pass filter:
   - Silence: volume=0 over each interval
   - Beep:    sine wave overlay
   - Fun:     espeak-ng PG word clips mixed in via amix
   |   (video stream is always copied losslessly)
   v
output_clean.mp4
```

---

## Project layout

```
CMVideo/
  app.py                  - Tkinter GUI entry point
  requirements.txt
  icon.svg / icon.png / icon.ico - app icons (cross-platform)

  install.sh              - Linux installer (apt/dnf/pacman/zypper)
  run.sh                  - Linux launcher (creates venv on first run)
  uninstall.sh            - Linux uninstaller
  enable-gpu.sh           - opt-in CUDA runtime installer

  install.ps1             - Windows installer (winget + portable fallback)
  install.bat             - double-click wrapper for install.ps1
  run.bat                 - Windows launcher (adds bin\ to PATH, runs pythonw)
  uninstall.ps1           - Windows uninstaller
  uninstall.bat           - double-click wrapper for uninstall.ps1

  censor/
    transcribe.py         - faster-whisper wrapper with CUDA fallback
    wordlist.py           - exact + fuzzy regex matcher
    audio.py              - ffmpeg extract / silence / beep / fun render
    funtts.py             - espeak-ng wrapper for the Fun mode
    download.py           - yt-dlp wrapper for URL downloads
    pipeline.py           - orchestrates the full pipeline

  wordlists/
    swears.txt
    slurs.txt
```
