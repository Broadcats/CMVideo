// CMVideo Mini front-end: pick format, preview metadata, stream download.

(function () {
  const $ = (id) => document.getElementById(id);
  const urlInput = $("url");
  const fmtSelect = $("fmt-select");
  const qualitySelect = $("quality-select");
  const qualityRow = $("quality-row");
  const previewBtn = $("info-btn");
  const dlBtn = $("dl-btn");
  const statusEl = $("status");
  const preview = $("preview");
  const previewThumb = $("preview-thumb");
  const previewTitle = $("preview-title");
  const previewSub = $("preview-sub");

  let busy = false;

  // Audio formats keep the quality select disabled - height has no
  // meaning when we're stripping the video stream. We keep the
  // value preserved so flipping back to MP4 restores the user's
  // last quality pick.
  function syncQualityRow() {
    if (!fmtSelect || !qualityRow || !qualitySelect) return;
    const isVideo = fmtSelect.value === "mp4";
    qualitySelect.disabled = !isVideo;
    qualityRow.classList.toggle("disabled", !isVideo);
  }
  if (fmtSelect) {
    fmtSelect.addEventListener("change", syncQualityRow);
    syncQualityRow();
  }

  // YouTube cookie upload flow is intentionally disabled in the mini
  // web UI; keep request bodies unchanged.
  function withYtSession(body) {
    return body;
  }

  function setStatus(text, kind) {
    statusEl.textContent = text || "";
    statusEl.classList.remove("error", "ok", "busy");
    if (kind) statusEl.classList.add(kind);
  }

  function setBusy(b) {
    busy = b;
    previewBtn.disabled = b;
    dlBtn.disabled = b;
  }

  function getFormat() {
    return (fmtSelect && fmtSelect.value) || "mp4";
  }

  function getQuality() {
    return (qualitySelect && qualitySelect.value) || "720p";
  }

  function fmtDuration(seconds) {
    if (!seconds && seconds !== 0) return "unknown length";
    const s = Math.floor(seconds);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const ss = String(s % 60).padStart(2, "0");
    if (h) return `${h}h ${String(m).padStart(2, "0")}m ${ss}s`;
    return `${m}:${ss}`;
  }

  async function callJson(path, body) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(withYtSession(body)),
    });
    if (!res.ok) {
      let msg = `HTTP ${res.status}`;
      try {
        const data = await res.json();
        if (data && data.detail) msg = data.detail;
      } catch (_) {}
      throw new Error(msg);
    }
    return res.json();
  }

  previewBtn.addEventListener("click", async () => {
    const url = urlInput.value.trim();
    if (!url) {
      setStatus("Paste a URL first.", "error");
      return;
    }
    setBusy(true);
    setStatus("Looking up the source...", "busy");
    preview.classList.add("hidden");
    try {
      const info = await callJson("/api/info", { url });
      previewTitle.textContent = info.title || "Untitled";
      const parts = [];
      if (info.uploader) parts.push(info.uploader);
      if (info.duration != null) parts.push(fmtDuration(info.duration));
      if (info.extractor) parts.push(info.extractor);
      previewSub.textContent = parts.join(" \u00B7 ") || "";
      previewSub.classList.toggle("over", !!info.over_cap);
      if (info.thumbnail) {
        previewThumb.src = info.thumbnail;
        previewThumb.style.display = "";
      } else {
        previewThumb.removeAttribute("src");
        previewThumb.style.display = "none";
      }
      preview.classList.remove("hidden");
      if (info.over_cap) {
        setStatus(
          "Heads up - this clip is longer than the mini-version cap. The download will be rejected. Use the full desktop app for anything longer.",
          "error",
        );
      } else {
        setStatus("Looks good. Hit Download.", "ok");
      }
    } catch (e) {
      setStatus(e.message || String(e), "error");
    } finally {
      setBusy(false);
    }
  });

  dlBtn.addEventListener("click", async () => {
    if (busy) return;
    const url = urlInput.value.trim();
    if (!url) {
      setStatus("Paste a URL first.", "error");
      return;
    }
    const fmt = getFormat();
    const quality = getQuality();
    setBusy(true);

    try {
      // FAST PATH: direct-stream pass-through.
      // Mini server resolves the URL, browser downloads from a
      // one-shot proxy endpoint. No server-side disk, no caps.
      // MP4 only - audio formats always need ffmpeg post-processing
      // on the server, so they skip this branch.
      if (fmt === "mp4") {
        setStatus("Resolving direct stream...", "busy");
        const initRes = await fetch("/api/stream-download", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(withYtSession({ url, format: fmt, quality })),
        });
        if (initRes.ok) {
          const init = await initRes.json();
          const a = document.createElement("a");
          a.href = init.stream_url;
          if (init.filename) a.download = init.filename;
          a.style.display = "none";
          document.body.appendChild(a);
          a.click();
          setTimeout(() => a.remove(), 1000);
          const sizeStr = init.filesize
            ? ` (~${Math.round(init.filesize / 1024 / 1024)} MB)`
            : "";
          setStatus(
            `Streaming ${init.filename}${sizeStr} directly. Browser is saving it now.`,
            "ok",
          );
          return;
        }
        // 422 = "this URL needs server-side processing"; fall
        // through to SLOW path. Anything else is a real error -
        // surface it instead of pretending the slow path will
        // help.
        if (initRes.status !== 422) {
          let msg = `HTTP ${initRes.status}`;
          try {
            const data = await initRes.json();
            if (data && data.detail) msg = data.detail;
          } catch (_) {}
          throw new Error(msg);
        }
      }

      // SLOW PATH: server-pull via /api/download (the JSON shim
      // for /api/process). Used for audio formats and for
      // fast-path 422s. Lossless audio (WAV/FLAC) gets a longer
      // status hint because ffmpeg encoding takes meaningfully
      // longer than passing through a streamed mp4.
      const isAudio = fmt !== "mp4";
      const isLossless = fmt === "wav" || fmt === "flac";
      const fmtLabel = fmt.toUpperCase();
      const hint = isLossless
        ? "this can take 30-180 seconds for lossless audio"
        : isAudio
          ? "this can take 10-60 seconds"
          : "this can take 10-90 seconds";
      setStatus(`Downloading ${fmtLabel} ... ${hint}. Stay on this tab.`, "busy");
      const res = await fetch("/api/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(withYtSession({ url, format: fmt, quality })),
      });
      if (!res.ok) {
        let msg = `HTTP ${res.status}`;
        try {
          const data = await res.json();
          if (data && data.detail) msg = data.detail;
        } catch (_) {}
        throw new Error(msg);
      }

      // Pull the suggested filename out of Content-Disposition.
      let filename = `cmvideo-mini.${fmt}`;
      const cd = res.headers.get("Content-Disposition") || "";
      const m = /filename\*=UTF-8''([^;]+)|filename="?([^";]+)"?/i.exec(cd);
      if (m) filename = decodeURIComponent(m[1] || m[2]);

      const blob = await res.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 60_000);

      setStatus(
        `Saved ${filename}. Need censoring or batch processing? Grab the full app at cmvideo.online.`,
        "ok",
      );
    } catch (e) {
      setStatus(e.message || String(e), "error");
    } finally {
      setBusy(false);
    }
  });

  urlInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      dlBtn.click();
    }
  });
})();
