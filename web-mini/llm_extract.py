"""Tier 8: LLM-assisted media URL extractor.

When every traditional extractor in censor.extractors has given up,
this module opens the page in Playwright, captures the DOM and the
full network log, then asks an LLM "find the video stream URL on
this page" with a structured prompt. The LLM returns a single
manifest URL plus the request headers needed to fetch it; we
validate the URL (must be public IP, must point at the page's
domain or a known CDN, must speak http or https) and hand it to
ffmpeg.

This is the only tier in the chain that does something genuinely
new: every other tool in the chain ships hand-written per-site
extractors that need maintenance as sites change. The LLM tier
treats the page as evidence and reasons about it on the fly, which
is the only way to cover the long tail without writing 1,800
extractors.

Cost / latency budget:
  * one Playwright page load (~10-20s, shared with the existing
    Playwright tier when both fire)
  * one LLM call (~3-10s on Groq's free tier, longer on local
    llama.cpp or Ollama)
  * one ffmpeg capture (variable, depends on stream length)

Configuration (env vars, all required for the tier to enable):
  * CMVIDEO_LLM_BASE_URL  - OpenAI-compatible endpoint, e.g.
                            https://api.groq.com/openai/v1
  * CMVIDEO_LLM_API_KEY   - provider's API key
  * CMVIDEO_LLM_MODEL     - model id (default llama-3.3-70b-versatile)

Optional:
  * CMVIDEO_LLM_ALLOW_DOMAINS - comma-separated extra domain
                                allowlist for the LLM's chosen URL
                                (in addition to the page's own
                                domain and the built-in CDN list)
  * CMVIDEO_LLM_MAX_HTML      - cap on HTML chars sent to the LLM
                                (default 30_000)
  * CMVIDEO_LLM_TIMEOUT       - wall-clock cap for the LLM call
                                (default 25s)

If any of the three required env vars is missing the tier
silently disables itself - the dispatcher just skips it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
LLM_BASE_URL = os.environ.get("CMVIDEO_LLM_BASE_URL", "").strip().rstrip("/")
LLM_API_KEY = os.environ.get("CMVIDEO_LLM_API_KEY", "").strip()
LLM_MODEL = os.environ.get("CMVIDEO_LLM_MODEL", "llama-3.3-70b-versatile").strip()
LLM_TIMEOUT = float(os.environ.get("CMVIDEO_LLM_TIMEOUT", "25"))
LLM_MAX_HTML = int(os.environ.get("CMVIDEO_LLM_MAX_HTML", "30000"))
LLM_USER_AGENT = "CMVideo/1.0 (+https://cmvideo.online)"

# Built-in CDN allowlist. The LLM's response URL is allowed if it
# points at the page's own host, any subdomain of it, or one of these
# well-known media CDN suffixes. Anything else is rejected as SSRF
# bait. List intentionally conservative.
_BUILTIN_CDN_SUFFIXES: tuple[str, ...] = (
    "akamaized.net", "akamaihd.net", "edgesuite.net", "edgekey.net",
    "cloudfront.net",
    "fastly.net", "fastlylb.net",
    "azureedge.net", "msecnd.net",
    "googlevideo.com", "ytimg.com",  # YouTube CDN (used by some embeds)
    "vimeocdn.com",
    "cdn.jwplayer.com", "content.jwplatform.com", "ssl.p.jwpcdn.com",
    "brightcove.com", "brightcove.net", "bcovlive.io",
    "kaltura.com", "kaltura.com.kapi", "kaltura.akamaized.net",
    "vidio.id",
    "twimg.com",  # Twitter video host
    "fbcdn.net",  # Meta CDN
    "redditmedia.com", "redd.it",
    "tiktokcdn.com", "muscdn.com", "tiktokv.com",
    "phncdn.com",  # MindGeek tubes (PH / RT / YP / Tube8)
    "ypncdn.com", "rdtcdn.com", "tube8cdn.com",
    "xvideos-cdn.com", "xnxx-cdn.com", "xhcdn.com",
    "qdcdn.com",  # generic qd
    "bilivideo.com", "biliapi.net", "bilibili.com",
    "douyincdn.com", "douyinpic.com", "douyinvod.com",
    "dailymotion.com",
    "streamable.com",
    "soundcloud.com", "sndcdn.com",
)
# Allow user to extend the list at deploy time.
_user_extra = os.environ.get("CMVIDEO_LLM_ALLOW_DOMAINS", "").strip()
_USER_CDN_SUFFIXES = tuple(d.strip().lower() for d in _user_extra.split(",") if d.strip())
ALLOWED_CDN_SUFFIXES = _BUILTIN_CDN_SUFFIXES + _USER_CDN_SUFFIXES


def llm_available() -> bool:
    """True if the env is configured to allow LLM-assisted extraction.

    Note: the actual *call* may still fail at request time (auth,
    rate limit, model unavailable). This is a cheap pre-check so the
    dispatcher knows whether to even try this tier."""
    return bool(LLM_BASE_URL and LLM_API_KEY)


def llm_status() -> dict[str, Any]:
    """Diagnostic snapshot used by /api/limits."""
    return {
        "enabled": llm_available(),
        "base_url": LLM_BASE_URL or None,
        "model": LLM_MODEL if llm_available() else None,
        "max_html_chars": LLM_MAX_HTML,
        "timeout_s": LLM_TIMEOUT,
        "extra_cdn_count": len(_USER_CDN_SUFFIXES),
    }


# ---------------------------------------------------------------------------
# Page capture (Playwright wrapper, also used by the regular Playwright
# tier when both fire so we don't pay the ~15-second tab-startup cost
# twice on the same URL).
# ---------------------------------------------------------------------------
@dataclass
class NetReq:
    url: str
    method: str
    status: int
    content_type: str
    size: int = 0


@dataclass
class PageCapture:
    page_url: str
    final_url: str
    page_title: str
    page_html: str           # HEAD content, capped at LLM_MAX_HTML
    network_log: list[NetReq] = field(default_factory=list)
    media_candidates: list[tuple[str, str, int]] = field(default_factory=list)
    duration_seconds: float = 0.0


def capture_page(
    url: str,
    *,
    nav_timeout_ms: int = 25_000,
    dwell_ms: int = 5_000,
) -> PageCapture:
    """Open `url` in headless Chromium, capture HTML + network log,
    return a PageCapture. Caller is responsible for the memory guard
    + concurrency semaphore (see censor.extractors)."""
    from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]

    network_log: list[NetReq] = []
    media_candidates: list[tuple[str, str, int]] = []

    def _on_response(resp):  # type: ignore[no-untyped-def]
        try:
            r_url = resp.url or ""
            ct = (resp.headers.get("content-type") or "").lower()
            cl = resp.headers.get("content-length") or "0"
            size = int(cl) if cl.isdigit() else 0
            req = resp.request
            method = (req.method if req else "").upper() or "GET"
            status = int(getattr(resp, "status", 0) or 0)
        except Exception:  # noqa: BLE001
            return
        # Keep all network requests up to a sane cap; we'll show them
        # to the LLM. Skip noise like data: / blob:.
        if r_url.startswith(("data:", "blob:", "chrome-extension:")):
            return
        if len(network_log) < 200:
            network_log.append(NetReq(r_url, method, status, ct, size))

        # Track media-looking candidates separately for the
        # heuristic Playwright tier.
        url_lc = r_url.lower()
        media_url = any(h in url_lc for h in (".m3u8", ".mpd", ".mp4", ".webm", ".m4s", ".ts"))
        media_ct = any(h in ct for h in ("mpegurl", "dash+xml", "video/"))
        if media_url or media_ct:
            media_candidates.append((r_url, ct, size))

    t0 = time.monotonic()
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        try:
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 720},
            )
            page = ctx.new_page()
            page.on("response", _on_response)
            page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout_ms)

            # Try to trigger a play gesture - many players only fetch
            # the manifest after a click. Best-effort.
            for selector in (
                'button[aria-label*="lay" i]',
                'button[title*="lay" i]',
                'button:has-text("Play")',
                'video',
            ):
                try:
                    el = page.locator(selector).first
                    el.click(timeout=1500)
                    break
                except Exception:  # noqa: BLE001
                    continue

            page.wait_for_timeout(max(0, dwell_ms))
            html = page.content()[:LLM_MAX_HTML]
            title = page.title() or ""
            final = page.url or url
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass

    return PageCapture(
        page_url=url,
        final_url=final,
        page_title=title[:200],
        page_html=html,
        network_log=network_log,
        media_candidates=media_candidates,
        duration_seconds=time.monotonic() - t0,
    )


# ---------------------------------------------------------------------------
# LLM client (OpenAI-compatible chat/completions)
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = """You are a video-stream URL extractor.

You are given a web page and the network requests its browser made
while loading. Your job is to identify the single best URL that an
ffmpeg-style downloader can use to capture the page's primary video.

Return JSON ONLY, no markdown, no commentary, matching this schema:

{
  "video_url": "<the URL ffmpeg should fetch>",
  "content_type": "<HLS | DASH | MP4 | WEBM | other>",
  "headers": {"<header>": "<value>", ...},
  "confidence": <float 0..1>,
  "reasoning": "<one short sentence>"
}

Rules:
* Prefer HLS (.m3u8) or DASH (.mpd) manifest URLs over individual
  segment URLs.
* Prefer the highest-bitrate manifest if multiple exist.
* If the page has multiple unrelated videos, pick the one most
  likely to be the page's main subject.
* Headers should include any Referer / User-Agent / Cookie that
  appear necessary for the URL to work.
* If you cannot identify a video URL, return confidence: 0 and
  video_url: "".
* Never invent a URL. The video_url MUST appear verbatim in the
  network requests provided.
"""


def _http_post_json(
    url: str,
    payload: dict,
    *,
    api_key: str,
    timeout: float,
) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": LLM_USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _build_user_prompt(capture: PageCapture) -> str:
    lines = [
        f"Page URL: {capture.page_url}",
        f"Final URL: {capture.final_url}",
        f"Page title: {capture.page_title}",
        "",
        "Network requests (method, status, content-type, size, url):",
    ]
    # Truncate the network log so the prompt stays sub-100k tokens.
    # Sort: media-looking first (so the LLM never misses them), then
    # everything else.
    def _is_media(req: NetReq) -> bool:
        u = req.url.lower()
        ct = req.content_type.lower()
        return (
            any(h in u for h in (".m3u8", ".mpd", ".mp4", ".webm", ".m4s", ".ts"))
            or any(h in ct for h in ("mpegurl", "dash+xml", "video/"))
        )
    sorted_log = sorted(capture.network_log, key=lambda r: (not _is_media(r), -r.size))
    for r in sorted_log[:120]:
        lines.append(
            f"  [{r.method} {r.status}] {r.content_type[:40]:40s} {r.size:>10d}  {r.url[:240]}"
        )
    lines.append("")
    lines.append("Page HTML (head + body, truncated):")
    lines.append(capture.page_html)
    return "\n".join(lines)


@dataclass
class LLMDecision:
    video_url: str
    content_type: str
    headers: dict[str, str]
    confidence: float
    reasoning: str


def call_llm(capture: PageCapture, *, base_url: str | None = None, api_key: str | None = None, model: str | None = None) -> LLMDecision:
    """Send the page capture to the configured LLM and parse its
    structured response. Raises RuntimeError on transport failures
    or unparseable output."""
    base = (base_url or LLM_BASE_URL).rstrip("/")
    key = api_key or LLM_API_KEY
    mdl = model or LLM_MODEL
    if not (base and key):
        raise RuntimeError("LLM tier not configured (set CMVIDEO_LLM_BASE_URL + CMVIDEO_LLM_API_KEY)")

    payload = {
        "model": mdl,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(capture)},
        ],
        "temperature": 0.0,
        "max_tokens": 600,
        # OpenAI-compatible JSON mode. Groq, Together, OpenAI all
        # honour it; providers that don't will simply ignore the
        # field and return prose, which we still parse with a regex.
        "response_format": {"type": "json_object"},
    }
    resp = _http_post_json(f"{base}/chat/completions", payload, api_key=key, timeout=LLM_TIMEOUT)
    try:
        choices = resp["choices"]
        content = choices[0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"LLM response shape unexpected: {resp}") from exc
    return _parse_llm_content(content)


def _parse_llm_content(content: str) -> LLMDecision:
    """Best-effort JSON parser for the LLM's response. Falls back to
    a regex if the model returned the JSON inside a markdown fence."""
    raw = content.strip()
    # Strip ```json ... ``` fences if present
    if raw.startswith("```"):
        raw = re.sub(r"^```(json)?", "", raw, count=1).strip()
        if raw.endswith("```"):
            raw = raw[:-3].strip()
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        # Last-ditch: pull the first {...} block.
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            raise RuntimeError(f"LLM did not return parseable JSON: {raw[:300]}")
        try:
            d = json.loads(m.group(0))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"LLM JSON inside fence is malformed: {raw[:300]}") from exc
    return LLMDecision(
        video_url=str(d.get("video_url") or "").strip(),
        content_type=str(d.get("content_type") or "").strip(),
        headers={str(k): str(v) for k, v in (d.get("headers") or {}).items()},
        confidence=float(d.get("confidence") or 0.0),
        reasoning=str(d.get("reasoning") or "")[:300],
    )


# ---------------------------------------------------------------------------
# URL safety: no internal IPs, page-domain or known CDN only
# ---------------------------------------------------------------------------
def _hostname_endswith_any(host: str, suffixes: tuple[str, ...]) -> bool:
    h = host.lower()
    for s in suffixes:
        s_l = s.lower().lstrip(".")
        if h == s_l or h.endswith("." + s_l):
            return True
    return False


def validate_decision(decision: LLMDecision, capture: PageCapture) -> str:
    """Returns the validated URL or raises RuntimeError. Rejects:
      * missing / empty URL
      * non-http(s) schemes
      * private / loopback / link-local IPs (SSRF guard)
      * URLs that don't point at the page's own host or a known
        media CDN (anti-hallucination + anti-data-exfil)
      * URLs that don't appear verbatim in the network log
        (anti-hallucination)
    """
    if not decision.video_url:
        raise RuntimeError(f"LLM declined: {decision.reasoning or '(no reason)'}")
    if decision.confidence < 0.4:
        raise RuntimeError(
            f"LLM confidence too low ({decision.confidence:.2f}): {decision.reasoning}"
        )

    parsed = urllib.parse.urlparse(decision.video_url)
    if parsed.scheme not in ("http", "https"):
        raise RuntimeError(f"LLM returned non-http URL: {parsed.scheme}")

    host = (parsed.hostname or "").lower()
    if not host:
        raise RuntimeError(f"LLM URL missing host: {decision.video_url}")

    # SSRF guard: refuse private / loopback / link-local literal IPs.
    import ipaddress
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            raise RuntimeError(f"LLM URL points at internal IP: {host}")
    except ValueError:
        pass  # not a literal IP, that's the normal case

    # Domain allowlist: page's own host or one of the CDN suffixes.
    page_host = (urllib.parse.urlparse(capture.page_url).hostname or "").lower()
    final_host = (urllib.parse.urlparse(capture.final_url).hostname or "").lower()
    page_root = ".".join(page_host.split(".")[-2:]) if page_host else ""
    final_root = ".".join(final_host.split(".")[-2:]) if final_host else ""
    on_page_domain = (
        bool(page_root) and _hostname_endswith_any(host, (page_root,))
    ) or (
        bool(final_root) and _hostname_endswith_any(host, (final_root,))
    )
    on_known_cdn = _hostname_endswith_any(host, ALLOWED_CDN_SUFFIXES)
    if not (on_page_domain or on_known_cdn):
        raise RuntimeError(
            f"LLM URL host {host} is neither on the page's domain "
            f"({page_root}) nor on the known CDN allowlist. Refusing "
            f"to fetch (anti-hallucination + anti-SSRF)."
        )

    # Anti-hallucination: the URL the LLM picked must actually have
    # appeared in the network log (or at least be a prefix of one).
    seen_urls = {req.url for req in capture.network_log}
    if decision.video_url not in seen_urls:
        # Soft check: maybe the LLM replaced query params. Try a
        # path-prefix match.
        target_pathonly = parsed._replace(query="", fragment="").geturl()
        if not any(u.startswith(target_pathonly) for u in seen_urls):
            raise RuntimeError(
                f"LLM URL did not appear in the network log "
                f"(possible hallucination): {decision.video_url}"
            )

    return decision.video_url


# ---------------------------------------------------------------------------
# Public adapter
# ---------------------------------------------------------------------------
def llm_extract(
    url: str,
    dst_dir: Path,
    *,
    fmt: str = "mp4",
    capture: PageCapture | None = None,
    timeout: int = 240,
) -> "tuple[Path, LLMDecision]":
    """Run the full LLM-assisted extraction pipeline. Returns the
    (saved file path, LLMDecision) tuple. Raises RuntimeError on
    failure - the dispatcher converts these to ExtractionErrors.

    Caller is responsible for the Playwright memory guard + the
    Tier-8 semaphore (we share the Playwright semaphore with the
    regular Playwright tier so we never have two Chromium instances
    fighting for RAM)."""
    if not llm_available():
        raise RuntimeError(
            "LLM tier disabled (set CMVIDEO_LLM_BASE_URL + CMVIDEO_LLM_API_KEY)"
        )
    if capture is None:
        capture = capture_page(url)

    decision = call_llm(capture)
    log.info(
        "LLM picked: %s (conf=%.2f, ct=%s, %d headers) - %s",
        decision.video_url[:120], decision.confidence, decision.content_type,
        len(decision.headers), decision.reasoning,
    )
    safe_url = validate_decision(decision, capture)

    out_file = dst_dir / f"llm-extract.{fmt}"
    ff_cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    # Forward LLM-suggested headers if any (Referer is the common
    # one).
    if decision.headers:
        hdr_str = "\\r\\n".join(f"{k}: {v}" for k, v in decision.headers.items())
        ff_cmd += ["-headers", f"{hdr_str}\\r\\n"]
    ff_cmd += [
        "-i", safe_url,
        "-t", str(max(60, timeout - 30)),
        "-c", "copy",
        str(out_file),
    ]
    try:
        proc = subprocess.run(
            ff_cmd, check=False, capture_output=True, text=True,
            timeout=max(60, timeout),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("ffmpeg capture timed out on LLM-picked URL") from exc

    if proc.returncode != 0 or not out_file.exists() or out_file.stat().st_size == 0:
        err = (proc.stderr or proc.stdout or "").strip()[-300:]
        raise RuntimeError(f"ffmpeg refused the LLM-picked URL: {err}")

    return out_file, decision
