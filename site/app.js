/* CMVideo landing site behaviour:
 *   1. OS detection swaps the primary download button + highlights
 *      the matching tile in the downloads grid.
 *   2. The hero mini widget posts to the CMVideo Mini backend (a free
 *      Hugging Face Space) - URL or uploaded file, MP4 or MP3, and
 *      either pure download or Silence / Beep censoring. Capped on the
 *      server so the desktop app stays the obvious next step.
 */
(function () {
  "use strict";

  /* ---------- (1) OS detection ---------- */

  function detectOS() {
    var p = (navigator.userAgentData && navigator.userAgentData.platform) || "";
    var ua = navigator.userAgent || "";
    var combined = (p + " " + ua).toLowerCase();
    if (combined.indexOf("win") !== -1) return "windows";
    if (combined.indexOf("mac") !== -1) return "mac";
    if (combined.indexOf("linux") !== -1 || combined.indexOf("x11") !== -1) return "linux";
    return null;
  }

  var os = detectOS();
  var labels = {
    linux:   { name: "Linux",   meta: ".AppImage \u00b7 no install \u00b7 just run it" },
    windows: { name: "Windows", meta: ".exe \u00b7 no install \u00b7 double-click" },
    mac:     { name: "macOS",   meta: ".tar.gz \u00b7 untested" }
  };

  var primary = document.getElementById("primary-download");
  var primaryMeta = document.getElementById("primary-download-meta");
  var match = os ? document.querySelector('.dl-card[data-os="' + os + '"]') : null;

  if (match && primary && primaryMeta) {
    primary.setAttribute("href", match.getAttribute("href"));
    primary.querySelector(".btn-label").textContent = "Download the full app for " + labels[os].name;
    primaryMeta.textContent = labels[os].meta;
    match.classList.add("recommended");
  } else if (primary && primaryMeta) {
    primary.setAttribute("href", "#downloads");
    primaryMeta.textContent = "pick your OS below";
  }

  /* ---------- (2) Mini widget ---------- */

  // mini.cmvideo.online is a custom-domain CNAME -> HF Spaces
  // (Dandyfeet/cmvideo-mini). The original hf.space URL still
  // works at the network level and is kept allowed in the CSP
  // below, so reverting is a one-line change with no redeploy
  // chain if the custom domain ever has issues.
  var MINI_API_BASE = "https://mini.cmvideo.online";

  // YouTube URLs are detected here for two reasons:
  //   1. Download mode now goes through the regular /api/process
  //      pipeline (best-effort via the residential proxy on the
  //      mini-app), so we DON'T short-circuit those.
  //   2. Censor mode (silence / beep) still needs the desktop app
  //      because the transcript step is harder than the download
  //      itself, even with a proxy. So censor-mode YT URLs short-
  //      circuit to the "use the desktop app" message + scroll.
  // The earlier in-browser iframe-player censor flow was removed
  // (see CHANGELOG v0.4.16-alpha) - it added an extra third-party
  // script source, a global-scoped player object, and intervalled
  // timers, all for a flow that was never actually wired into the
  // submit path. Keeping unused code in the bundle expands the
  // attack surface for no benefit.
  // Pattern matches youtube.com/watch?v=ID, youtu.be/ID, /embed/ID,
  // /shorts/ID, plus youtube-nocookie.com variants.
  var YT_URL_RE = /(?:(?:youtube|youtube-nocookie)\.com\/(?:watch\?(?:.*&)?v=|embed\/|shorts\/|v\/)|youtu\.be\/)([A-Za-z0-9_-]{11})/i;
  function extractYouTubeId(u) { var m = YT_URL_RE.exec(String(u||"")); return m ? m[1] : null; }

  /* Per-URL submit cooldown.
   *
   * The mini-app shares one outbound IP across all visitors. Sites
   * that rate-limit anonymous scrapers (Instagram in particular,
   * also Facebook / Threads / Tiktok / X) start throttling after a
   * handful of requests for the same URL in rapid succession. The
   * canonical bug pattern: a user pastes a reel and tests
   * 720p -> 1080p -> MP3 in 60 seconds, the first call succeeds,
   * the rest fail with "rate-limit reached". Quality has nothing to
   * do with it; the source site simply blocked our IP for that URL.
   *
   * To prevent the user from accidentally inducing this, we hold a
   * tiny in-memory map of {normalized_url: last_submit_ts} and
   * refuse same-URL resubmits inside the cooldown window. The
   * window is short (15s default) and longer for known throttle-
   * happy domains.
   *
   * Lives in plain JS state (not localStorage) so a hard refresh
   * resets it - this is a guardrail, not a punishment. */
  var THROTTLE_HEAVY_DOMAINS = [
    "instagram.com", "facebook.com", "fb.watch", "threads.net",
    "tiktok.com", "x.com", "twitter.com",
  ];
  var COOLDOWN_DEFAULT_MS = 15 * 1000;
  var COOLDOWN_HOSTILE_MS = 60 * 1000;
  var lastSubmitForUrl = Object.create(null);

  function normalizeUrlForCooldown(u) {
    /* We want "same reel" to count as same URL even if the user
     * pasted ?utm_source=... or fragment differences. Strip
     * query/fragment, lowercase host, drop trailing slash. */
    try {
      var parsed = new URL(String(u || ""));
      var host = (parsed.hostname || "").toLowerCase();
      var path = (parsed.pathname || "").replace(/\/+$/, "");
      return host + path;
    } catch (_) {
      return String(u || "").trim().toLowerCase();
    }
  }

  function cooldownMsFor(u) {
    var host;
    try { host = new URL(String(u || "")).hostname.toLowerCase(); }
    catch (_) { return COOLDOWN_DEFAULT_MS; }
    for (var i = 0; i < THROTTLE_HEAVY_DOMAINS.length; i++) {
      var d = THROTTLE_HEAVY_DOMAINS[i];
      if (host === d || host.endsWith("." + d)) return COOLDOWN_HOSTILE_MS;
    }
    return COOLDOWN_DEFAULT_MS;
  }

  function checkSameUrlCooldown(u) {
    /* Returns 0 if the submit is allowed, or the seconds remaining
     * if the cooldown is still active. */
    var key = normalizeUrlForCooldown(u);
    if (!key) return 0;
    var now = Date.now();
    var last = lastSubmitForUrl[key] || 0;
    var window_ms = cooldownMsFor(u);
    var elapsed = now - last;
    if (elapsed >= window_ms) return 0;
    return Math.ceil((window_ms - elapsed) / 1000);
  }

  function recordSubmitForUrl(u) {
    var key = normalizeUrlForCooldown(u);
    if (!key) return;
    lastSubmitForUrl[key] = Date.now();
  }


  var form        = document.getElementById("mini-form");
  if (!form) return;

  /* Probe the mini service on page load. If HF returns a definitive
   * 404 (i.e. the Space doesn't exist or is paused) hide the form
   * entirely and show a clean offline state instead of letting people
   * try and fail. Cold-start 503s and network errors are treated as
   * "might be waking up" - we leave the widget alone for those. */
  (function probeService() {
    var ctrl = ("AbortController" in window) ? new AbortController() : null;
    var timer = setTimeout(function () { if (ctrl) ctrl.abort(); }, 4000);
    fetch(MINI_API_BASE + "/healthz", { method: "GET", mode: "cors", signal: ctrl ? ctrl.signal : undefined })
      .then(function (res) {
        clearTimeout(timer);
        if (res.status === 404) showOffline();
      })
      .catch(function () { clearTimeout(timer); /* network/timeout: leave UI alone */ });
  })();

  function showOffline() {
    // Defense-in-depth: build the offline panel via DOM nodes
    // rather than innerHTML. Every string here is a static
    // literal, but createElement makes that property visible to
    // every reviewer + every static-analysis tool.
    var hero = form.closest(".hero-mini");
    if (!hero) return;
    var body = hero.querySelector(".shot-body") || hero;
    while (body.firstChild) body.removeChild(body.firstChild);

    var wrap = document.createElement("div");
    wrap.className = "shot-offline";

    var eyebrow = document.createElement("div");
    eyebrow.className = "shot-offline-eyebrow";
    eyebrow.textContent = "Mini service offline";
    wrap.appendChild(eyebrow);

    var p = document.createElement("p");
    p.textContent = "The free web slice isn\u2019t reachable right now. " +
      "The desktop app is the full deal anyway \u2014 unlimited length, " +
      "every format, real censoring, and it runs locally.";
    wrap.appendChild(p);

    var a = document.createElement("a");
    a.className = "btn btn-primary";
    a.href = "#downloads";
    a.textContent = "Download CMVideo";
    wrap.appendChild(a);

    body.appendChild(wrap);
  }

  var card        = form.closest(".hero-mini") || form;
  var urlInput    = document.getElementById("mini-url");
  var fileInput   = document.getElementById("mini-file");
  var fileRow     = document.getElementById("mini-file-row");
  var fileName    = document.getElementById("mini-file-name");
  var fileClear   = document.getElementById("mini-file-clear");
  var btn         = document.getElementById("mini-btn");
  var statusEl    = document.getElementById("mini-status");

  // Caps mirror MAX_DOWNLOAD_DURATION_SECONDS / MAX_CENSOR_DURATION_SECONDS
  // in web-mini/app.py. Kept duplicated here because the widget has no
  // way to ask the backend for them cheaply, and these don't change
  // often. If you bump them in app.py, bump them here too.
  var MAX_DOWNLOAD_DURATION_S = 60 * 60;     // 1 hour
  var MAX_CENSOR_DURATION_S   = 8 * 60;      // 8 min - the surprising one
  var MAX_DOWNLOAD_FILESIZE_MB = 800;
  var MAX_CENSOR_FILESIZE_MB   = 100;

  var BTN_LABELS = {
    download: "Download",
    silence:  "Silence swears (\u2264 8 min)",
    beep:     "Beep swears (\u2264 8 min)"
  };
  var BUSY_LABELS = {
    download: "Downloading\u2026",
    silence:  "Transcribing (\u2264 8 min cap)\u2026",
    beep:     "Transcribing (\u2264 8 min cap)\u2026"
  };
  var modeNoteEl = document.getElementById("mini-mode-note");

  function getMode()    { var s = form.querySelector('input[name="mini-mode"]:checked');    return s ? s.value : "download"; }
  function getFormat()  { var s = form.querySelector('input[name="mini-fmt"]:checked');     return s ? s.value : "mp4"; }
  function getFPS()     { var s = form.querySelector('input[name="mini-fps"]:checked');     return s ? s.value : "source"; }
  function getQuality() { var s = form.querySelector('input[name="mini-quality"]:checked'); return s ? s.value : "standard"; }

  function setStatus(text, kind) {
    if (!statusEl) return;
    statusEl.textContent = text || "";
    statusEl.classList.remove("error", "ok", "busy");
    if (kind) statusEl.classList.add(kind);
  }

  function syncBtnLabel() {
    if (!btn) return;
    btn.textContent = BTN_LABELS[getMode()] || "Download";
  }

  function syncModeNote() {
    if (!modeNoteEl) return;
    var m = getMode();
    if (m === "silence" || m === "beep") modeNoteEl.removeAttribute("hidden");
    else                                 modeNoteEl.setAttribute("hidden", "");
  }

  function setBusy(b) {
    if (!btn) return;
    btn.disabled = b;
    btn.textContent = b ? (BUSY_LABELS[getMode()] || "Working\u2026") : (BTN_LABELS[getMode()] || "Download");
  }

  /* ---- pill highlight + button-label sync ---- */
  var fpsRow         = document.getElementById("mini-fps-row");
  var qualityRow     = document.getElementById("mini-quality-row");
  var qualityNote    = document.getElementById("mini-quality-note");
  var fmtQualityDiv  = document.getElementById("mini-fmtquality-divider");

  function syncFpsRow() {
    if (!fpsRow) return;
    // MP3 has no frames; hide the fps row entirely when audio is selected.
    var hideForAudio = getFormat() === "mp3";
    fpsRow.classList.toggle("hidden", hideForAudio);

    // 1080p + 30/60 fps would re-encode at 1080p on shared CPU and
    // blow our ffmpeg cap. Disable the override pills (and force
    // "Source") whenever HD is picked so the user sees what's allowed
    // without waiting for the backend to bounce the request.
    var lockToSource = !hideForAudio && getQuality() === "hd";
    Array.prototype.forEach.call(
      form.querySelectorAll('input[name="mini-fps"]'),
      function (i) {
        var pill = i.closest(".pill");
        var isOverride = i.value === "30" || i.value === "60";
        if (lockToSource && isOverride) {
          if (i.checked) {
            i.checked = false;
            var src = form.querySelector('input[name="mini-fps"][value="source"]');
            if (src) {
              src.checked = true;
              var srcPill = src.closest(".pill");
              if (srcPill) srcPill.classList.add("on");
            }
          }
          i.disabled = true;
          if (pill) {
            pill.classList.remove("on");
            pill.classList.add("disabled");
            pill.setAttribute("title", "1080p uses Source fps only on the mini app.");
          }
        } else {
          i.disabled = false;
          if (pill) {
            pill.classList.remove("disabled");
            pill.removeAttribute("title");
          }
        }
      }
    );
  }

  function syncQualityRow() {
    var isAudio = getFormat() === "mp3";
    // Quality is video-only: hide both the chips AND the vertical
    // divider when MP3 is picked, otherwise the divider would dangle
    // next to the format pills with nothing on its right side.
    if (qualityRow)    qualityRow.classList.toggle("hidden", isAudio);
    if (fmtQualityDiv) fmtQualityDiv.classList.toggle("hidden", isAudio);
    if (qualityNote) {
      if (getQuality() === "hd" && !isAudio) qualityNote.removeAttribute("hidden");
      else                                   qualityNote.setAttribute("hidden", "");
    }
  }

  Array.prototype.forEach.call(
    form.querySelectorAll(
      'input[name="mini-fmt"], input[name="mini-mode"], input[name="mini-fps"], input[name="mini-quality"]'
    ),
    function (input) {
      input.addEventListener("change", function () {
        var groupName = input.getAttribute("name");
        Array.prototype.forEach.call(
          form.querySelectorAll('input[name="' + groupName + '"]'),
          function (i) {
            var pill = i.closest(".pill");
            if (pill) pill.classList.toggle("on", i.checked);
          }
        );
        if (groupName === "mini-mode")    { syncBtnLabel(); syncModeNote(); }
        if (groupName === "mini-fmt")     { syncFpsRow(); syncQualityRow(); }
        if (groupName === "mini-quality") { syncFpsRow(); syncQualityRow(); }
      });
    }
  );
  syncBtnLabel();
  syncQualityRow();
  syncFpsRow();
  syncModeNote();

  /* ---- selected file row ---- */
  var selectedFile = null;

  function setFile(file) {
    selectedFile = file || null;
    if (selectedFile) {
      if (urlInput) urlInput.value = "";
      if (fileName) fileName.textContent = selectedFile.name + " (" + Math.round(selectedFile.size / 1024) + " KB)";
      if (fileRow)  fileRow.classList.remove("hidden");
    } else {
      if (fileName) fileName.textContent = "";
      if (fileRow)  fileRow.classList.add("hidden");
      if (fileInput) fileInput.value = "";
    }
  }

  if (fileInput) {
    fileInput.addEventListener("change", function () {
      if (fileInput.files && fileInput.files[0]) setFile(fileInput.files[0]);
    });
  }
  if (fileClear) {
    fileClear.addEventListener("click", function () { setFile(null); setStatus(""); });
  }
  if (urlInput) {
    urlInput.addEventListener("input", function () {
      if (urlInput.value && selectedFile) setFile(null);
    });
  }

  /* ---- drag-drop on the whole card ---- */
  function blockEvent(e) { e.preventDefault(); e.stopPropagation(); }

  ["dragenter", "dragover"].forEach(function (ev) {
    card.addEventListener(ev, function (e) { blockEvent(e); card.classList.add("dragover"); });
  });
  ["dragleave", "dragend"].forEach(function (ev) {
    card.addEventListener(ev, function (e) { blockEvent(e); card.classList.remove("dragover"); });
  });
  card.addEventListener("drop", function (e) {
    blockEvent(e);
    card.classList.remove("dragover");
    var dt = e.dataTransfer;
    if (!dt || !dt.files || !dt.files.length) return;
    setFile(dt.files[0]);
    setStatus("Loaded \u201C" + dt.files[0].name + "\u201D. Pick a mode and hit the button.", "ok");
  });

  /* ---- submit ---- */
  function downloadBlob(blob, name) {
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name || "cmvideo-mini";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 60000);
  }

  function parseFilename(headers, fallback) {
    var cd = headers.get("Content-Disposition") || "";
    var m = /filename\*=UTF-8''([^;]+)|filename="?([^";]+)"?/i.exec(cd);
    if (!m) return fallback;
    try { return decodeURIComponent(m[1] || m[2]); }
    catch (_) { return m[1] || m[2] || fallback; }
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();

    var mode = getMode();
    var fmt  = getFormat();
    var url  = (urlInput && urlInput.value || "").trim();

    if (!url && !selectedFile) {
      setStatus("Paste a URL or drop a file onto the card first.", "error");
      if (urlInput) urlInput.focus();
      return;
    }
    if (url && selectedFile) {
      setStatus("Pick one: URL OR file, not both. Clearing the URL.", "error");
      if (urlInput) urlInput.value = "";
    }
    if (mode === "download" && selectedFile) {
      setStatus("Download mode is URL-only \u2014 you already have the file. Switching to Silence.", "error");
      var s = form.querySelector('input[name="mini-mode"][value="silence"]');
      if (s) { s.checked = true; s.dispatchEvent(new Event("change")); mode = "silence"; }
    }

    // YouTube routing.
    //
    // Old behaviour (pre-residential-proxy): a hard short-circuit
    // back to "use the desktop app" because YT was 100% broken
    // from the HF Space's datacenter IP. Now that the mini routes
    // youtube.com / googlevideo.com through the residential proxy,
    // download mode actually has a real chance, so we let it
    // through to /api/process and surface the real backend error
    // if YT does fail (anti-bot, login wall, etc.).
    //
    // Censor mode (silence / beep) is more involved on YT: it
    // needs the transcript API, which is harder than the video
    // download even with a proxy, and the in-browser iframe
    // alternative isn't fully wired yet. Until that's stable we
    // keep redirecting censor-mode YT to the desktop app.
    var ytId = extractYouTubeId(url);
    if (ytId && !selectedFile && (mode === "silence" || mode === "beep")) {
      setStatus(
        "YouTube censoring needs the transcript API, which the mini can\u2019t " +
        "always reach. Censoring runs perfectly in the desktop app at " +
        "cmvideo.online \u2014 grab it below. (Plain MP4 / MP3 download from " +
        "YouTube is supported in the mini, just switch the mode.)",
        "error"
      );
      var dl = document.getElementById("downloads");
      if (dl && dl.scrollIntoView) {
        try { dl.scrollIntoView({ behavior: "smooth", block: "start" }); } catch (_) {}
      }
      return;
    }

    // Same-URL cooldown. Stops the test pattern of submitting the
    // same URL 3x with different quality / format options inside a
    // minute, which is the canonical way to get throttled by IG /
    // FB / TT. Skipped entirely for file uploads (no source-site
    // hit) and for empty URLs.
    if (url && !selectedFile) {
      var waitS = checkSameUrlCooldown(url);
      if (waitS > 0) {
        setStatus(
          "You just submitted this URL. Wait " + waitS + "s before retrying \u2014 " +
          "rapid resubmits trigger the source site's anti-scraping limits and " +
          "make every following attempt fail.",
          "error"
        );
        return;
      }
    }

    // Local pre-flight checks: file size for uploads, source duration
    // for URLs in censor mode. Cheaper than letting the backend reject
    // with a 413/504 after the user already waited for the cold-start.
    var capMB   = (mode === "download") ? MAX_DOWNLOAD_FILESIZE_MB : MAX_CENSOR_FILESIZE_MB;
    if (selectedFile && selectedFile.size > capMB * 1024 * 1024) {
      setStatus(
        "That file is " + Math.round(selectedFile.size / 1024 / 1024) +
        " MB \u2014 over the " + capMB + " MB " + (mode === "download" ? "download" : "censor") +
        " cap. Use the desktop app for full-size files.",
        "error"
      );
      return;
    }

    setBusy(true);
    setStatus("Submitting\u2026", "busy");
    setProgress(0, "Submitting\u2026", true);
    if (url && !selectedFile) recordSubmitForUrl(url);

    // Censor mode + URL: ask the backend for duration before we
    // commit to the rate-limiter. If it's over 8 min we never burn a
    // job slot and the user gets an instant, clear rejection.
    var preflight = Promise.resolve();
    if ((mode === "silence" || mode === "beep") && !selectedFile) {
      preflight = fetch(MINI_API_BASE + "/api/info", {
        method: "POST", mode: "cors",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: url })
      }).then(function (res) {
        if (!res.ok) return null;       // info failed - let the backend handle it
        return res.json().catch(function () { return null; });
      }).then(function (info) {
        if (!info || info.duration == null) return;
        if (info.duration > MAX_CENSOR_DURATION_S) {
          var mins = Math.floor(info.duration / 60);
          var secs = Math.round(info.duration % 60);
          var len  = mins + ":" + (secs < 10 ? "0" : "") + secs;
          throw new Error(
            "That clip is " + len + " \u2014 over the 8-minute mini-app censor cap. " +
            "Use the desktop app at cmvideo.online for full-length censoring."
          );
        }
      });
    }

    var fd = new FormData();
    fd.append("format", fmt);
    fd.append("mode", mode);
    if (fmt === "mp4") {
      fd.append("fps", getFPS());
      fd.append("quality", getQuality());
    }
    if (selectedFile) fd.append("file", selectedFile);
    else              fd.append("url", url);

    // Pipeline. Two paths:
    //
    //   FAST  (y2mate-style stream pass-through):
    //     POST /api/stream-download   -> { stream_url, filename, filesize }
    //     <a href={stream_url} click> -> browser native download
    //   Used for raw URL downloads where yt-dlp can resolve the
    //   source to a single complete MP4 over plain HTTP. No
    //   server-side disk usage, no 360s cap, no filesize cap.
    //   The mini server only proxies bytes - the user pays
    //   upstream-to-them bandwidth, not 2x via the server's disk.
    //
    //   SLOW  (server-pull async pipeline):
    //     POST /api/process?async=1   -> { job_id }
    //     poll GET /api/jobs/{id}     -> { stage, pct, ready, ... }
    //     when ready: GET /api/jobs/{id}/file
    //   Used for everything else: censor mode, MP3 (needs ffmpeg),
    //   HLS / DASH sources that need fragment merging, and any
    //   raw-download case where the fast path returned 422
    //   "needs_processing".
    //
    // Once the fast path triggers a browser download we pass `null`
    // through the rest of the chain so the SLOW-path .then steps
    // become no-ops. Cleaner than two separate chains and keeps
    // error handling unified.
    preflight
      .then(function () {
        // Fast path is only meaningful for URL-based MP4 downloads.
        // File uploads, MP3 (needs ffmpeg), and censor modes
        // (silence / beep) all skip straight to the SLOW path.
        if (mode !== "download" || fmt !== "mp4" || selectedFile) return null;
        return fetch(MINI_API_BASE + "/api/stream-download", {
          method: "POST", mode: "cors",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url: url, format: fmt, quality: getQuality() })
        }).then(function (res) {
          // 422 = backend says "this URL needs the slow path";
          // that's the expected branch for HLS, DASH, MP3, and
          // sites with separated streams. Fall through silently.
          if (res.status === 422) return null;
          if (!res.ok) return mapHttpError(res);
          return res.json();
        });
      })
      .then(function (streamInit) {
        if (streamInit && streamInit.stream_url) {
          // Browser-native download via an invisible <a download>
          // click. Cross-origin link, but the mini server sets
          // Content-Disposition: attachment so the browser saves
          // instead of navigating - same trick y2mate uses.
          var streamUrl = MINI_API_BASE + streamInit.stream_url;
          var a = document.createElement("a");
          a.href = streamUrl;
          if (streamInit.filename) a.download = streamInit.filename;
          a.rel = "noopener";
          a.style.display = "none";
          document.body.appendChild(a);
          a.click();
          setTimeout(function () {
            if (a.parentNode) a.parentNode.removeChild(a);
          }, 1000);

          hideProgress();
          var name = streamInit.filename || ("cmvideo-mini." + fmt);
          var sizeStr = "";
          if (streamInit.filesize) {
            sizeStr = " (\u2248" + Math.round(streamInit.filesize / 1024 / 1024) + " MB)";
          }
          setStatus(
            "Streaming " + name + sizeStr + " directly to your browser \u2014 " +
            "no caps, no waiting on the server. Want full quality, censoring, " +
            "and offline use? Grab the desktop app below.",
            "ok"
          );
          return null;          // sentinel for SLOW-path no-op
        }
        return fetch(MINI_API_BASE + "/api/process?async=1", { method: "POST", mode: "cors", body: fd });
      })
      .then(function (res) {
        if (res === null) return null;      // fast path took it
        if (!res.ok) return mapHttpError(res);
        return res.json();
      })
      .then(function (data) {
        if (data === null) return null;
        if (!data || !data.job_id) throw new Error("Mini service didn't return a job id.");
        return pollJob(data.job_id);
      })
      .then(function (jobId) {
        if (jobId === null) return null;
        setProgress(100, "Saving\u2026", false);
        return fetch(MINI_API_BASE + "/api/jobs/" + encodeURIComponent(jobId) + "/file", { method: "GET", mode: "cors" })
          .then(function (res) {
            if (!res.ok) return mapHttpError(res);
            var name = parseFilename(res.headers, "cmvideo-mini." + fmt);
            return res.blob().then(function (blob) { return { blob: blob, name: name }; });
          });
      })
      .then(function (out) {
        if (out === null) return;          // fast path handled it
        downloadBlob(out.blob, out.name);
        hideProgress();
        if (mode === "download") {
          setStatus("Saved " + out.name + ". Want fuzzy matching, more formats, and the actual censoring? Grab the app below.", "ok");
        } else {
          setStatus("Saved " + out.name + ". Mini uses exact-token matching; the full app catches leetspeak / phonetic variants and TTS replacement.", "ok");
        }
      })
      .catch(function (err) {
        hideProgress();
        var msg = (err && err.message) || String(err);
        if (msg === "Failed to fetch" || /NetworkError/i.test(msg)) {
          msg = "Couldn't reach the mini-app service. It might be cold-booting \u2014 retry in 30 seconds, or grab the desktop app below.";
        }
        setStatus(msg, "error");
      })
      .then(function () { setBusy(false); });
  });

  /* ---- async helpers ---- */

  function mapHttpError(res) {
    if (res.status === 404) {
      // 404 on a job poll/fetch USED to be the "mini offline"
      // message because the backend's _get_job returned 404 in
      // two cases:
      //   (a) the job genuinely doesn't exist (rare: deploy /
      //       30-min TTL / typo in job_id), OR
      //   (b) the polling client's IP shifted between submit
      //       and poll - which on mobile CGNAT is normal and
      //       happens to legitimate users mid-session.
      // (b) is now removed in the backend (v0.4.16.3-alpha) -
      // see web-mini/app.py _get_job. So a real 404 here is
      // (a): the job evaporated. The right user message is
      // "retry" rather than "we are offline" because /healthz
      // is independent and the /healthz probe at page load
      // would have already drawn the offline panel if the
      // service were actually down.
      throw new Error(
        "That download seems to have expired \u2014 click Download again to retry. " +
        "If this keeps happening, grab the desktop app below."
      );
    }
    if (res.status === 429) {
      // Don't hardcode the cap number - the operator can tune
      // CMVIDEO_RATE_LIMIT_PER_HOUR without touching this file.
      // The /api/limits endpoint advertises the live value if a
      // future UI wants to surface it.
      throw new Error("Hit the per-hour mini-app cap. Try again later, or grab the desktop app below for unlimited jobs.");
    }
    if (res.status === 413) {
      throw new Error("That clip is over the mini-app size cap. Use the desktop app for full-length / full-quality runs.");
    }
    if (res.status === 503) {
      throw new Error("Mini service is busy right now \u2014 try again in a minute, or grab the desktop app.");
    }
    return res.json().then(
      function (data) { throw new Error((data && data.detail) || ("HTTP " + res.status)); },
      function ()    { throw new Error("HTTP " + res.status + " from the mini service"); }
    );
  }

  function pollJob(jobId) {
    var POLL_MS = 700;
    var IDLE_TIMEOUT_MS = 6 * 60 * 1000;   // give up if stage hasn't changed for 6 min
    var lastStage = null;
    var lastPct = -1;
    var idleSince = Date.now();
    return new Promise(function (resolve, reject) {
      function step() {
        fetch(MINI_API_BASE + "/api/jobs/" + encodeURIComponent(jobId), { method: "GET", mode: "cors" })
          .then(function (res) {
            if (!res.ok) return mapHttpError(res);
            return res.json();
          })
          .then(function (state) {
            if (!state) throw new Error("Empty job state.");
            if (state.error) throw new Error(state.error);
            if (state.stage !== lastStage || state.pct !== lastPct) {
              idleSince = Date.now();
              lastStage = state.stage;
              lastPct = state.pct;
            }
            var label = state.stage_label || state.stage || "Working\u2026";
            var pctTxt = state.pct + "%";
            // Add a "2.4 MB/s · 14s left" detail strip during the
            // fetch stage so users actually see the bar moving and
            // can see the network is busy. Falls back to plain pct
            // when the backend hasn't reported telemetry yet.
            var detail = "";
            if (state.stage === "fetching") {
                detail = formatRate(state.speed_bps) + formatEta(state.eta_s) + formatBytes(state.bytes_done, state.bytes_total);
                label = "Pulling source\u2026 " + pctTxt + (detail ? "  \u00B7  " + detail : "");
            } else if (state.stage === "transcribing") {
                label = "Transcribing audio\u2026 " + pctTxt + " (this is the slow step)";
            } else if (state.stage === "rendering") {
                label = "Rendering output\u2026 " + pctTxt;
            }
            setProgress(state.pct, label, false);

            if (state.ready) { resolve(jobId); return; }
            if (Date.now() - idleSince > IDLE_TIMEOUT_MS) {
              reject(new Error("The job stopped reporting progress. The mini service may have crashed \u2014 retry, or grab the desktop app."));
              return;
            }
            setTimeout(step, POLL_MS);
          })
          .catch(function (err) { reject(err); });
      }
      step();
    });
  }

  /* ---- progress detail formatters ---- */

  function formatRate(bps) {
    if (!bps || bps < 1) return "";
    var units = ["B/s", "KB/s", "MB/s", "GB/s"];
    var i = 0; var v = bps;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return (v >= 10 ? Math.round(v) : Math.round(v * 10) / 10) + " " + units[i];
  }

  function formatEta(s) {
    if (!s || s <= 0) return "";
    if (s < 60)   return "  \u00B7  " + Math.round(s) + "s left";
    var m = Math.floor(s / 60); var r = Math.round(s - m * 60);
    return "  \u00B7  " + m + "m" + (r ? " " + r + "s" : "") + " left";
  }

  function formatBytes(done, total) {
    if (!done) return "";
    function fmt(b) {
      if (b < 1024) return b + " B";
      if (b < 1024 * 1024) return (b / 1024).toFixed(1) + " KB";
      if (b < 1024 * 1024 * 1024) return (b / (1024 * 1024)).toFixed(1) + " MB";
      return (b / (1024 * 1024 * 1024)).toFixed(1) + " GB";
    }
    if (total && total > 0) return "  \u00B7  " + fmt(done) + " / " + fmt(total);
    return "  \u00B7  " + fmt(done);
  }

  /* ---- progress bar UI (created lazily, lives above the status line) ---- */

  var progressEl = null;
  var progressFill = null;
  var progressLabel = null;

  function ensureProgress() {
    if (progressEl) return;
    progressEl = document.createElement("div");
    progressEl.className = "mini-progress";
    progressEl.setAttribute("aria-live", "polite");
    progressEl.style.display = "none";

    var label = document.createElement("div");
    label.className = "mini-progress-label";
    progressLabel = label;

    var bar = document.createElement("div");
    bar.className = "mini-progress-bar";
    var fill = document.createElement("div");
    fill.className = "mini-progress-fill";
    bar.appendChild(fill);
    progressFill = fill;

    progressEl.appendChild(label);
    progressEl.appendChild(bar);

    if (statusEl && statusEl.parentNode) {
      statusEl.parentNode.insertBefore(progressEl, statusEl);
    } else {
      form.appendChild(progressEl);
    }
  }

  function setProgress(pct, label, indeterminate) {
    ensureProgress();
    progressEl.style.display = "block";
    progressEl.classList.toggle("indeterminate", !!indeterminate);
    var p = Math.max(0, Math.min(100, Math.round(pct || 0)));
    progressFill.style.width = (indeterminate ? 0 : p) + "%";
    progressLabel.textContent = label || (p + "%");
  }

  function hideProgress() {
    if (!progressEl) return;
    progressEl.style.display = "none";
    progressEl.classList.remove("indeterminate");
    if (progressFill) progressFill.style.width = "0%";
  }

  /* ---- handy: click on the URL field shows a hint about drag-drop ---- */
  if (urlInput) {
    urlInput.addEventListener("focus", function () {
      if (!statusEl || statusEl.textContent) return;
      setStatus("Tip: you can also drag an MP4 / MP3 file onto this card.", "");
    });
  }

  // The YouTube iframe-API loader and the in-browser cleanwatch
  // (mute-scheduler + /api/yt-censor + renderEmbedPlayer) used to
  // live here, but the integration was never wired into the submit
  // path - YT URLs in censor mode redirect to the desktop app
  // section and download mode goes through the regular /api/process
  // pipeline. Keeping unused code in the bundle expands the attack
  // surface for no benefit (one extra third-party script source,
  // global-scoped player object, intervalled timers), so it's
  // dropped here. If the iframe path is ever revived, restore from
  // git history at v0.4.15.3-alpha.
})();
