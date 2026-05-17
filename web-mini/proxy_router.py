"""Per-domain residential-proxy routing for the mini-app.

Datacenter IPs (the HF Space's outbound) get throttled hard by
Instagram, Facebook, Threads, TikTok, X, and the MindGeek tubes.
Routing those specific outbound fetches through a residential
proxy provider (e.g. IPRoyal pay-as-you-go) makes our requests
look like home users on real ISPs, which clears the throttle
floor without the device-ban risks of running a burner-account
cookie pile.

Configuration:

  CMVIDEO_RESIDENTIAL_PROXY
      Full HTTP proxy URL with embedded credentials, e.g.:
        http://USERNAME:PASSWORD@geo.iproyal.com:12321
      Unset = proxy disabled, every fetch goes direct
      (current behaviour, preserved as a safe default).

  CMVIDEO_PROXY_EXTRA_DOMAINS
      Optional CSV of additional hostnames (or hostname suffixes)
      to route through the proxy on top of the built-in list.

The "decide if a URL should be proxied" check runs against the
*destination URL's hostname*, not the original page URL. That way
when Playwright captures a cdninstagram.com media URL on an
instagram.com page, we proxy the cdninstagram.com fetch as well.

Cost discipline:

We deliberately do NOT route every outbound through the proxy.
Most extractor traffic (YouTube, Reddit, Vimeo, Twitch, plain
direct-MP4 URLs) works fine on a datacenter IP and proxying it
just burns paid GB for no benefit. The PROXY_DOMAINS allowlist
below is the set of hostnames where datacenter IPs measurably
hurt success rate.
"""

from __future__ import annotations

import logging
import os
from typing import Optional
from urllib.parse import urlparse

log = logging.getLogger("proxy_router")


# Hostnames (and hostname suffixes) where proxying through a
# residential IP measurably improves success rate. Each entry is a
# suffix - "instagram.com" matches "www.instagram.com",
# "scontent-iad3-2.cdninstagram.com" matches "cdninstagram.com",
# etc.
#
# Categorised below for readability; flat-set membership at runtime.
_PROXY_DOMAIN_TUPLES: tuple[tuple[str, str], ...] = (
    # --- Meta family (Instagram + Facebook + Threads share infra) ---
    ("instagram.com", "Instagram page domain"),
    ("cdninstagram.com", "Instagram CDN (scontent-*-*.cdninstagram.com)"),
    ("facebook.com", "Facebook page domain"),
    ("fb.watch", "Facebook short-link"),
    ("fbcdn.net", "Meta CDN (FB + IG video segments)"),
    ("threads.net", "Threads page domain"),

    # --- TikTok ---
    ("tiktok.com", "TikTok page domain"),
    ("tiktokcdn.com", "TikTok CDN"),
    ("muscdn.com", "TikTok music / video CDN"),
    ("tiktokv.com", "TikTok video domain"),

    # --- X / Twitter ---
    ("x.com", "X page domain"),
    ("twitter.com", "Legacy Twitter domain"),
    ("twimg.com", "Twitter / X media CDN"),

    # --- MindGeek tubes (extremely datacenter-hostile) ---
    ("pornhub.com", "Pornhub"),
    ("redtube.com", "RedTube"),
    ("youporn.com", "YouPorn"),
    ("tube8.com", "Tube8"),
    ("phncdn.com", "MindGeek shared CDN"),
    ("ypncdn.com", "YouPorn CDN"),
    ("rdtcdn.com", "RedTube CDN"),
    ("tube8cdn.com", "Tube8 CDN"),

    # --- Other tube sites with datacenter hostility ---
    ("xvideos.com", "xVideos"),
    ("xvideos-cdn.com", "xVideos CDN"),
    ("xnxx.com", "xnxx"),
    ("xnxx-cdn.com", "xnxx CDN"),
    ("xhamster.com", "xHamster"),
    ("xhcdn.com", "xHamster CDN"),
    ("spankbang.com", "Spankbang"),
    ("thisvid.com", "ThisVid"),
    ("ttcache.com", "ThisVid / similar tube CDN"),
    ("eporner.com", "Eporner"),
    ("epornercdn.com", "Eporner CDN"),
    ("redgifs.com", "RedGifs"),
    ("rdgcdn.com", "RedGifs CDN"),

    # --- YouTube ---
    # YouTube actively datacenter-throttles + does TLS fingerprint
    # checks; residential IP alone doesn't unlock everything (some
    # videos need cookies for PoToken / age-gated content). With
    # proxy enabled, success rate jumps from ~5% to maybe ~60-70%
    # for short / popular / non-restricted videos. Without proxy
    # YT is broken from the HF Space entirely, hence why we used
    # to short-circuit YT to "use the desktop app". Now the user
    # can at least try via the mini and only fall back to desktop
    # when it actually fails.
    ("youtube.com", "YouTube page domain"),
    ("youtu.be", "YouTube short-link"),
    ("googlevideo.com", "YouTube media CDN (video / audio segments)"),
    ("ytimg.com", "YouTube thumbnail / metadata CDN"),

    # --- East-Asian video portals (often geo + datacenter-hostile) ---
    ("bilibili.com", "Bilibili page + variants"),
    ("biliapi.net", "Bilibili API"),
    ("bilivideo.com", "Bilibili video CDN"),
    ("douyin.com", "Douyin"),
    ("douyinpic.com", "Douyin image"),
    ("douyinvod.com", "Douyin video CDN"),
    ("iqiyi.com", "iQiyi"),
    ("youku.com", "Youku"),
    ("weibo.com", "Weibo"),
    ("weibo.cn", "Weibo mobile"),
    ("nicovideo.jp", "Niconico (geo-restricted to JP)"),
)


def _read_extra_domains() -> tuple[str, ...]:
    """Parse CMVIDEO_PROXY_EXTRA_DOMAINS (CSV) for operator-supplied
    additions. Empty / unset = no extras."""
    raw = (os.environ.get("CMVIDEO_PROXY_EXTRA_DOMAINS") or "").strip()
    if not raw:
        return ()
    out: list[str] = []
    for entry in raw.split(","):
        entry = entry.strip().lower().lstrip("*.")
        if entry:
            out.append(entry)
    return tuple(out)


_EXTRA_DOMAINS: tuple[str, ...] = _read_extra_domains()


def proxy_url() -> Optional[str]:
    """Return the configured residential proxy URL, or None if the
    operator hasn't set CMVIDEO_RESIDENTIAL_PROXY. Reading at call
    time (not module load) lets HF restart-on-secret-change apply
    without needing a code redeploy."""
    val = (os.environ.get("CMVIDEO_RESIDENTIAL_PROXY") or "").strip()
    return val or None


def is_configured() -> bool:
    """True if a proxy URL is set."""
    return proxy_url() is not None


def _hostname_of(url: str) -> str:
    if not url:
        return ""
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def _hostname_matches(host: str, suffixes: tuple[str, ...]) -> bool:
    """True if `host` equals or ends with any suffix (matching at a
    label boundary so "instagram.com" doesn't accidentally match
    "fakeinstagram.com.evil.tld"). Matches both `host == suffix`
    and `host.endswith("." + suffix)`."""
    if not host:
        return False
    for suffix in suffixes:
        if host == suffix or host.endswith("." + suffix):
            return True
    return False


_BUILTIN_DOMAINS: tuple[str, ...] = tuple(d for d, _ in _PROXY_DOMAIN_TUPLES)


def should_proxy(url: str) -> bool:
    """Return True if `url`'s hostname should be routed through the
    residential proxy. False on misconfiguration / unset, so callers
    can use this as a single gate without separate is_configured()
    plumbing."""
    if not is_configured():
        return False
    host = _hostname_of(url)
    if not host:
        return False
    if _hostname_matches(host, _BUILTIN_DOMAINS):
        return True
    if _EXTRA_DOMAINS and _hostname_matches(host, _EXTRA_DOMAINS):
        return True
    return False


def proxy_for_url(url: str) -> Optional[str]:
    """Return the proxy URL if this URL should be proxied, else None.
    Convenience wrapper - callers that need a yes/no decision and the
    URL together can do `p = proxy_for_url(u); if p: ...` instead of
    two separate calls."""
    if should_proxy(url):
        return proxy_url()
    return None


def status() -> dict:
    """Diagnostic snapshot for /api/limits. NEVER includes the proxy
    URL itself (which contains credentials) - just whether it's
    configured and how many domains the allowlist covers."""
    return {
        "active": is_configured(),
        "builtin_domains": len(_BUILTIN_DOMAINS),
        "extra_domains": len(_EXTRA_DOMAINS),
    }


def playwright_proxy_for_url(url: str) -> Optional[dict]:
    """Return a Playwright `ProxySettings` dict for `url`, or None if
    the URL doesn't qualify or no proxy is configured.

    Playwright's `launch(proxy=...)` expects:
        {"server": "http://host:port", "username": "...", "password": "..."}

    Splitting credentials out of the embedded URL is required because
    Playwright deprecated `http://user:pass@host:port` in the
    `server` field. We parse here so callers don't have to."""
    raw = proxy_for_url(url)
    if not raw:
        return None
    parsed = urlparse(raw)
    if not parsed.hostname:
        return None
    port = parsed.port
    scheme = parsed.scheme or "http"
    server = f"{scheme}://{parsed.hostname}"
    if port:
        server = f"{server}:{port}"
    out: dict = {"server": server}
    if parsed.username:
        out["username"] = parsed.username
    if parsed.password:
        out["password"] = parsed.password
    return out


def urllib_opener_for_url(url: str):
    """Return a `urllib.request.OpenerDirector` configured to route
    through the residential proxy if `url` qualifies, or `None` if
    direct connection is fine. Use:

        opener = proxy_router.urllib_opener_for_url(url)
        if opener:
            with opener.open(req, timeout=10) as resp: ...
        else:
            with urllib.request.urlopen(req, timeout=10) as resp: ...

    Returning None when no proxy is needed lets callers stay on the
    cheap default urlopen path - we don't pay the cost of building
    an opener for direct fetches."""
    proxy = proxy_for_url(url)
    if not proxy:
        return None
    import urllib.request
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    return urllib.request.build_opener(handler)


def subprocess_env_for_url(url: str, base_env: Optional[dict] = None) -> dict:
    """Build an environment dict for `subprocess.run(..., env=...)`
    that adds `http_proxy` / `https_proxy` / `HTTP_PROXY` /
    `HTTPS_PROXY` if the URL should be proxied. Returns a shallow
    copy of `base_env` (or os.environ) either way - safe to pass
    directly into subprocess.

    Used for ffmpeg and any other CLI tool that respects the
    standard proxy env vars (curl, wget, gallery-dl, lux, etc.)."""
    env = dict(base_env if base_env is not None else os.environ)
    proxy = proxy_for_url(url)
    if proxy:
        # Lowercase + uppercase both - different tools check different
        # capitalizations and there's no harm setting both.
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
    return env
