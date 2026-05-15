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
    primary.querySelector(".btn-label").textContent = "Download for " + labels[os].name;
    primaryMeta.textContent = labels[os].meta;
    match.classList.add("recommended");
  } else if (primary && primaryMeta) {
    primary.setAttribute("href", "#downloads");
    primaryMeta.textContent = "pick your OS below";
  }

  /* ---------- (2) Mini widget ---------- */

  var MINI_API_BASE = "https://dandyfeet-cmvideo-mini.hf.space";

  // YouTube URLs take a separate, fully-legal path: we fetch the
  // transcript via the backend (no video download), then embed the
  // official YouTube iframe player here and schedule client-side
  // mute()/unMute() calls at every flagged word. No copyrighted
  // bytes ever touch our infrastructure. Pattern matches
  // youtube.com/watch?v=ID, youtu.be/ID, /embed/ID, /shorts/ID.
  var YT_URL_RE = /(?:youtube\.com\/(?:watch\?(?:.*&)?v=|embed\/|shorts\/|v\/)|youtu\.be\/)([A-Za-z0-9_-]{11})/i;
  function extractYouTubeId(u) { var m = YT_URL_RE.exec(String(u||"")); return m ? m[1] : null; }


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
    var hero = form.closest(".hero-mini");
    if (!hero) return;
    var body = hero.querySelector(".shot-body") || hero;
    body.innerHTML =
      '<div class="shot-offline">' +
      '  <div class="shot-offline-eyebrow">Mini service offline</div>' +
      '  <p>The free web slice isn’t reachable right now. The desktop app is the full deal anyway — unlimited length, every format, real censoring, and it runs locally.</p>' +
      '  <a class="btn btn-primary" href="#downloads">Download CMVideo</a>' +
      '</div>';
  }

  var card        = form.closest(".hero-mini") || form;
  var urlInput    = document.getElementById("mini-url");
  var fileInput   = document.getElementById("mini-file");
  var fileRow     = document.getElementById("mini-file-row");
  var fileName    = document.getElementById("mini-file-name");
  var fileClear   = document.getElementById("mini-file-clear");
  var btn         = document.getElementById("mini-btn");
  var statusEl    = document.getElementById("mini-status");

  var BTN_LABELS = {
    download: "Download",
    silence:  "Silence swears",
    beep:     "Beep swears"
  };
  var BUSY_LABELS = {
    download: "Downloading\u2026",
    silence:  "Transcribing\u2026",
    beep:     "Transcribing\u2026"
  };

  function getMode()   { var s = form.querySelector('input[name="mini-mode"]:checked'); return s ? s.value : "download"; }
  function getFormat() { var s = form.querySelector('input[name="mini-fmt"]:checked');  return s ? s.value : "mp4"; }

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

  function setBusy(b) {
    if (!btn) return;
    btn.disabled = b;
    btn.textContent = b ? (BUSY_LABELS[getMode()] || "Working\u2026") : (BTN_LABELS[getMode()] || "Download");
  }

  /* ---- pill highlight + button-label sync ---- */
  Array.prototype.forEach.call(
    form.querySelectorAll('input[name="mini-fmt"], input[name="mini-mode"]'),
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
        if (groupName === "mini-mode") syncBtnLabel();
      });
    }
  );
  syncBtnLabel();

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

    // YouTube takes the embed-and-censor path regardless of mode/format.
    var ytId = extractYouTubeId(url);
    if (ytId && !selectedFile) {
      runYouTubeCensorFlow(url, ytId);
      return;
    }

    setBusy(true);
    var busyMsg;
    if (mode === "download") {
      busyMsg = "Pulling " + fmt.toUpperCase() + "\u2026 typically 10\u201360 sec.";
    } else {
      busyMsg = "Transcribing + " + mode + "\u2026 typically 30\u2013120 sec on free CPU. Stay on this tab.";
    }
    setStatus(busyMsg, "busy");

    var fd = new FormData();
    fd.append("format", fmt);
    fd.append("mode", mode);
    if (selectedFile) fd.append("file", selectedFile);
    else              fd.append("url", url);

    fetch(MINI_API_BASE + "/api/process", { method: "POST", mode: "cors", body: fd })
      .then(function (res) {
        if (!res.ok) {
          // Special-case the common "Space not deployed yet" path: the
          // Hugging Face frontend returns 404 with an HTML body for any
          // route on a Space that doesn't exist.
          if (res.status === 404) {
            throw new Error(
              "The mini service is offline right now — grab the desktop app below, it does everything this widget does (and a lot more)."
            );
          }
          if (res.status === 429) {
            throw new Error(
              "Hit the 5-jobs-per-hour mini-app cap. The desktop app has no caps."
            );
          }
          if (res.status === 413) {
            throw new Error(
              "That clip is over the mini-app size cap. Use the desktop app for full-length / full-quality runs."
            );
          }
          return res.json().then(
            function (data) { throw new Error((data && data.detail) || ("HTTP " + res.status)); },
            function ()    { throw new Error("HTTP " + res.status + " from the mini service"); }
          );
        }
        var name = parseFilename(res.headers, "cmvideo-mini." + fmt);
        return res.blob().then(function (blob) { return { blob: blob, name: name }; });
      })
      .then(function (out) {
        downloadBlob(out.blob, out.name);
        if (mode === "download") {
          setStatus("Saved " + out.name + ". Want fuzzy matching, more formats, and the actual censoring? Grab the app below.", "ok");
        } else {
          setStatus("Saved " + out.name + ". Mini uses exact-token matching; the full app catches leetspeak / phonetic variants and 'Fun' TTS replacement.", "ok");
        }
      })
      .catch(function (err) {
        var msg = (err && err.message) || String(err);
        if (msg === "Failed to fetch" || /NetworkError/i.test(msg)) {
          msg = "Couldn't reach the mini-app service. It might be cold-booting \u2014 retry in 30 seconds, or grab the desktop app below.";
        }
        setStatus(msg, "error");
      })
      .then(function () { setBusy(false); });
  });

  /* ---- handy: click on the URL field shows a hint about drag-drop ---- */
  if (urlInput) {
    urlInput.addEventListener("focus", function () {
      if (!statusEl || statusEl.textContent) return;
      setStatus("Tip: you can also drag an MP4 / MP3 file onto this card.", "");
    });
  }

  // ============================================================
  // YouTube embed + client-side mute scheduler (Option B)
  // ============================================================
  var ytApiLoading = false;
  var ytApiReady   = (window.YT && window.YT.Player) ? true : false;
  var ytApiCbs     = [];

  function loadYouTubeIframeAPI(cb) {
    if (ytApiReady) { cb(); return; }
    ytApiCbs.push(cb);
    if (ytApiLoading) return;
    ytApiLoading = true;
    var prev = window.onYouTubeIframeAPIReady;
    window.onYouTubeIframeAPIReady = function () {
      ytApiReady = true;
      if (typeof prev === "function") { try { prev(); } catch(_){} }
      var cbs = ytApiCbs.slice(); ytApiCbs.length = 0;
      cbs.forEach(function (f) { try { f(); } catch (e) { console.error(e); } });
    };
    var s = document.createElement("script");
    s.src = "https://www.youtube.com/iframe_api";
    s.async = true;
    document.head.appendChild(s);
  }

  function runYouTubeCensorFlow(url, ytId) {
    setBusy(true);
    setStatus("Reading the YouTube transcript\u2026", "busy");
    fetch(MINI_API_BASE + "/api/yt-censor", {
      method: "POST", mode: "cors",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: url }),
    })
      .then(function (res) {
        if (!res.ok) {
          if (res.status === 404) {
            throw new Error("The mini service is offline right now \u2014 grab the desktop app below.");
          }
          if (res.status === 429) {
            throw new Error("Hit the 30-per-hour cap for YouTube transcripts. Try again later or use the desktop app.");
          }
          return res.json().then(
            function (d) { throw new Error((d && d.detail) || ("HTTP " + res.status)); },
            function ()  { throw new Error("HTTP " + res.status + " from the mini service"); }
          );
        }
        return res.json();
      })
      .then(function (data) {
        renderEmbedPlayer(data);
      })
      .catch(function (err) {
        var msg = (err && err.message) || String(err);
        if (msg === "Failed to fetch" || /NetworkError/i.test(msg)) {
          msg = "Couldn\u2019t reach the mini service. Try again in 30 seconds.";
        }
        setStatus(msg, "error");
        setBusy(false);
      });
  }

  function renderEmbedPlayer(data) {
    var hero = form.closest(".hero-mini");
    if (!hero) return;
    var body = hero.querySelector(".shot-body") || hero;
    var intervals = (data && data.intervals) || [];
    var countMsg = intervals.length === 0
      ? "Nothing flagged in this video\u2019s transcript \u2014 plays normally."
      : intervals.length + " word" + (intervals.length === 1 ? "" : "s") + " will auto-mute.";
    body.innerHTML =
      "<div class=\"shot-embed\">" +
      "  <div class=\"shot-embed-eyebrow\">YouTube cleanwatch</div>" +
      "  <div class=\"shot-embed-frame\"><div id=\"yt-player\"></div></div>" +
      "  <div class=\"shot-embed-meta\">" +
      "    <span class=\"shot-embed-pill\" id=\"yt-state\">Ready</span>" +
      "    <span class=\"shot-embed-count\">" + countMsg + "</span>" +
      "  </div>" +
      "  <div class=\"shot-embed-hint\">Watching only \u2014 we never download YouTube videos. Want a saved file? <a href=\"#downloads\">Get the desktop app</a>.</div>" +
      "  <button type=\"button\" class=\"shot-embed-back\" id=\"yt-back\">\u2190 Try another URL or file</button>" +
      "</div>";

    var backBtn = document.getElementById("yt-back");
    if (backBtn) backBtn.addEventListener("click", function () { window.location.reload(); });

    loadYouTubeIframeAPI(function () {
      var player = new YT.Player("yt-player", {
        height: "100%", width: "100%",
        videoId: data.video_id,
        playerVars: { rel: 0, modestbranding: 1, playsinline: 1 },
        events: {
          onReady: function () { setStatus("", ""); setBusy(false); },
          onStateChange: function (e) {
            var stateEl = document.getElementById("yt-state");
            if (!stateEl) return;
            if (e.data === YT.PlayerState.PLAYING) { stateEl.textContent = "Playing"; startMuteScheduler(player, intervals); }
            else if (e.data === YT.PlayerState.PAUSED) { stateEl.textContent = "Paused"; stopMuteScheduler(); }
            else if (e.data === YT.PlayerState.ENDED) { stateEl.textContent = "Ended"; stopMuteScheduler(); player.unMute(); }
          },
          onError: function () { setStatus("YouTube refused to embed this video (uploader disabled embedding, or age-restricted). Try a different one.", "error"); }
        }
      });
    });
  }

  var muteTimer = null;
  function stopMuteScheduler() { if (muteTimer) { clearInterval(muteTimer); muteTimer = null; } }
  function startMuteScheduler(player, intervals) {
    stopMuteScheduler();
    if (!intervals.length) return;
    var muted = false;
    var stateEl = document.getElementById("yt-state");
    muteTimer = setInterval(function () {
      if (!player || typeof player.getCurrentTime !== "function") return;
      var t = player.getCurrentTime();
      var hit = null;
      for (var i = 0; i < intervals.length; i++) {
        if (t >= intervals[i].start && t <= intervals[i].end) { hit = intervals[i]; break; }
      }
      if (hit && !muted) {
        try { player.mute(); } catch(_){}
        muted = true;
        if (stateEl) { stateEl.textContent = "\ud83d\udd07 muted"; stateEl.classList.add("muting"); }
      } else if (!hit && muted) {
        try { player.unMute(); } catch(_){}
        muted = false;
        if (stateEl) { stateEl.textContent = "Playing"; stateEl.classList.remove("muting"); }
      }
    }, 80);
  }
})();
