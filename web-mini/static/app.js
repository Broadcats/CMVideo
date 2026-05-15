// CMVideo Mini front-end: pick format, preview metadata, stream download.

(function () {
  const $ = (id) => document.getElementById(id);
  const urlInput = $("url");
  const previewBtn = $("info-btn");
  const dlBtn = $("dl-btn");
  const statusEl = $("status");
  const preview = $("preview");
  const previewThumb = $("preview-thumb");
  const previewTitle = $("preview-title");
  const previewSub = $("preview-sub");

  let busy = false;

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
    const sel = document.querySelector('input[name="fmt"]:checked');
    return sel ? sel.value : "mp4";
  }

  // Toggle pill "on" class to match the chosen radio.
  document.querySelectorAll('.pill input[name="fmt"]').forEach((input) => {
    input.addEventListener("change", () => {
      document.querySelectorAll(".pill").forEach((p) => p.classList.remove("on"));
      input.closest(".pill").classList.add("on");
    });
  });

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
      body: JSON.stringify(body),
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
    setBusy(true);
    setStatus(`Downloading ${fmt.toUpperCase()} ... this can take 10-90 seconds. Stay on this tab.`, "busy");
    try {
      const res = await fetch("/api/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, format: fmt }),
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
        `Saved ${filename}. Want full quality, more formats, or censoring? Grab the full app at cmvideo.online.`,
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
