/* CMVideo landing site behaviour:
 *   1. OS detection swaps the primary download button + highlights
 *      the matching tile in the downloads grid. Pure progressive
 *      enhancement; if JS is off the user still sees every download.
 *   2. The hero mini widget posts the URL + format to the CMVideo Mini
 *      backend (a free Hugging Face Space) and streams the result back
 *      to the user as a file download. Capped server-side - the form
 *      below the input nudges visitors toward the desktop app.
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
    if (combined.indexOf("linux") !== -1 || combined.indexOf("x11") !== -1) {
      return "linux";
    }
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
    primary.querySelector(".btn-label").textContent =
      "Download for " + labels[os].name;
    primaryMeta.textContent = labels[os].meta;
    match.classList.add("recommended");
  } else if (primary && primaryMeta) {
    primary.setAttribute("href", "#downloads");
    primaryMeta.textContent = "pick your OS below";
  }

  /* ---------- (2) Mini widget ---------- */

  // The backend lives on a free Hugging Face Space; the page calls it
  // cross-origin. CORS is allow-listed on the Space side.
  var MINI_API_BASE = "https://broadcats-cmvideo-mini.hf.space";

  var form = document.getElementById("mini-form");
  if (!form) return;

  var urlInput  = document.getElementById("mini-url");
  var btn       = document.getElementById("mini-btn");
  var statusEl  = document.getElementById("mini-status");
  var defaultBtnText = btn ? btn.textContent : "Download";

  function setStatus(text, kind) {
    if (!statusEl) return;
    statusEl.textContent = text || "";
    statusEl.classList.remove("error", "ok", "busy");
    if (kind) statusEl.classList.add(kind);
  }

  function setBusy(b) {
    if (!btn) return;
    btn.disabled = b;
    btn.textContent = b ? "Working\u2026" : defaultBtnText;
  }

  function getFormat() {
    var sel = form.querySelector('input[name="mini-fmt"]:checked');
    return sel ? sel.value : "mp4";
  }

  // Pill "on" class follows the chosen radio.
  Array.prototype.forEach.call(
    form.querySelectorAll('input[name="mini-fmt"]'),
    function (input) {
      input.addEventListener("change", function () {
        Array.prototype.forEach.call(
          form.querySelectorAll(".shot-fmt-row .pill"),
          function (p) { p.classList.remove("on"); }
        );
        var parent = input.closest(".pill");
        if (parent) parent.classList.add("on");
      });
    }
  );

  function downloadBlob(blob, name) {
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name || "cmvideo-mini";
    document.body.appendChild(a);
    a.click();
    a.remove();
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
    if (!urlInput) return;

    var url = (urlInput.value || "").trim();
    if (!url) {
      setStatus("Paste a video URL first.", "error");
      urlInput.focus();
      return;
    }

    var fmt = getFormat();
    setBusy(true);
    setStatus("Downloading " + fmt.toUpperCase() + "\u2026 this can take 10\u201390 seconds. Stay on this tab.", "busy");

    fetch(MINI_API_BASE + "/api/download", {
      method: "POST",
      mode: "cors",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: url, format: fmt })
    })
      .then(function (res) {
        if (!res.ok) {
          return res.json().then(
            function (data) { throw new Error((data && data.detail) || ("HTTP " + res.status)); },
            function ()    { throw new Error("HTTP " + res.status); }
          );
        }
        var name = parseFilename(res.headers, "cmvideo-mini." + fmt);
        return res.blob().then(function (blob) { return { blob: blob, name: name }; });
      })
      .then(function (out) {
        downloadBlob(out.blob, out.name);
        setStatus(
          "Saved " + out.name + ". Want no caps and actual censoring? Grab the full app below.",
          "ok"
        );
      })
      .catch(function (err) {
        var msg = (err && err.message) || String(err);
        if (msg === "Failed to fetch" || msg === "NetworkError when attempting to fetch resource.") {
          msg = "Couldn't reach the mini-app service. It might be cold-booting - retry in 30 seconds, or grab the desktop app below.";
        }
        setStatus(msg, "error");
      })
      .then(function () { setBusy(false); });
  });
})();
