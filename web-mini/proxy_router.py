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

    # --- News + media sites that 403 datacenter IPs ---
    # Stress test (May 2026) showed yt-dlp's metadata extract
    # returning HTTP 403 from HF Space without proxy on these.
    # They use Akamai / Cloudflare / Fastly bot-fingerprint
    # rules that flag any non-residential IP. Routing through
    # the residential proxy clears the gate.
    ("bloomberg.com", "Bloomberg"),
    ("bwbx.io", "Bloomberg media CDN"),
    ("newgrounds.com", "Newgrounds (Cloudflare 403 from datacenter)"),
    ("ngfiles.com", "Newgrounds media CDN"),
    ("reddit.com", "Reddit (JSON API 403/429s from datacenter)"),
    ("redd.it", "Reddit short-link / media domain"),
    ("v.redd.it", "Reddit video CDN (HLS playlists)"),
    ("i.redd.it", "Reddit image CDN"),

    # --- Coub / 9GAG / other social-meme platforms ---
    ("coub.com", "Coub (HTTP 403 from datacenter)"),
    ("9gag.com", "9GAG (HTTP 403 from datacenter)"),
    ("9cache.com", "9GAG media CDN"),
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


def _normalize_proxy_url(raw: str) -> str:
    """Normalize the configured proxy URL to standard
    `<scheme>://<user>:<pass>@<host>:<port>` form.

    Why this matters:
        IPRoyal's dashboard displays creds in a compact
        `host:port:user:pass` format. Operators frequently
        paste that form straight into the env var with `http://`
        prepended, producing `http://host:port:user:pass`. yt-dlp
        cannot parse that (only one `:` is allowed between scheme
        and host), and its error message echoes the WHOLE URL
        VERBATIM - which historically leaked the proxy
        credentials into user-facing extraction errors.

        Detect this format and rewrite to the standard URL form
        BEFORE handing it to any downstream tool. Operators don't
        have to remember the right separator.

    Accepted shapes (case-insensitive on the scheme):
        host:port:user:pass             -> http://user:pass@host:port
        http://host:port:user:pass      -> http://user:pass@host:port
        http://user:pass@host:port      -> unchanged
        socks5://...                    -> unchanged
        (anything with `@`)             -> unchanged

    The rewrite is purely lexical - we don't validate the host
    or port. The downstream tool (yt-dlp / requests) will reject
    obvious garbage with a normal "couldn't connect" error
    instead of echoing the credentials in a parse error.
    """
    s = raw.strip()
    if not s:
        return s
    # Already-correct standard form: anything with '@' is assumed
    # to be `scheme://user:pass@host` and left alone. yt-dlp /
    # requests both accept this.
    if "@" in s:
        return s
    # Optional scheme prefix - handle both "scheme://body" and
    # bare body. We only normalize HTTP-ish schemes; leave SOCKS
    # alone since IPRoyal's compact format is HTTP-only.
    scheme = "http"
    body = s
    if "://" in s:
        head, body = s.split("://", 1)
        head_lower = head.lower()
        if head_lower not in ("http", "https"):
            return s  # SOCKS or unknown scheme: hands off
        scheme = head_lower
    parts = body.split(":")
    # iProyal compact form is exactly 4 colon-separated parts:
    # host:port:user:pass. Anything else (3 parts = host:port:user
    # with no password, 5+ = colon in password we can't safely
    # disambiguate) we leave alone and let downstream complain.
    if len(parts) != 4:
        return s
    host, port, user, pwd = parts
    if not host or not port.isdigit() or not user or not pwd:
        return s
    return f"{scheme}://{user}:{pwd}@{host}:{port}"


def proxy_url() -> Optional[str]:
    """Return the configured residential proxy URL, normalized to
    standard `scheme://user:pass@host:port` form, or None if the
    operator hasn't set CMVIDEO_RESIDENTIAL_PROXY. Reading at call
    time (not module load) lets HF restart-on-secret-change apply
    without needing a code redeploy."""
    val = _normalize_proxy_url(os.environ.get("CMVIDEO_RESIDENTIAL_PROXY") or "")
    return val or None


# Pattern matches the auth segment of a URL-shaped substring:
#   <scheme>://<user>:<pass>@<host>...
# Used by `redact_secrets()`. Public regex compiled once.
_STD_AUTH_RE = __import__("re").compile(r'(://)[^:@/\s]+:[^@/\s]+@')


def redact_secrets(text: str) -> str:
    """Scrub residential-proxy credentials from `text`. Apply at
    EVERY boundary where extractor / yt-dlp / playwright error
    messages flow into a log line OR a user-facing response.

    Defense-in-depth: this function applies THREE passes because
    different downstream tools mangle the proxy URL into different
    shapes when they reject it.

    Pass 1 - URL-shape regex:
        `http://user:pass@host:port` -> `http://***:***@host:port`
        Catches anything that's still in standard URL form.

    Pass 2 - literal env-var value:
        Replaces the whole `CMVIDEO_RESIDENTIAL_PROXY` value with
        `***` whenever it appears verbatim in `text`. Catches the
        case where some tool echoes the original (un-normalized)
        env-var form, e.g. iProyal's `host:port:user:pass`
        compact format that yt-dlp can't parse.

    Pass 3 - cred fragments:
        Replaces just the user and password substrings (extracted
        from the normalized URL) with `***` independently. Catches
        partial echoes like "auth failed for user XXX" or "503
        from upstream YYY:port" that wouldn't match either of the
        first two passes.

    Cost: three regex/string passes per call, each O(n) on the
    input. Negligible for log lines and error messages."""
    if not text:
        return text
    out = _STD_AUTH_RE.sub(r'\1***:***@', text)
    raw = (os.environ.get("CMVIDEO_RESIDENTIAL_PROXY") or "").strip()
    if raw and raw in out:
        out = out.replace(raw, "***")
    normalized = _normalize_proxy_url(raw)
    if normalized and normalized in out:
        out = out.replace(normalized, "***")
    if normalized and "@" in normalized and "://" in normalized:
        try:
            after_scheme = normalized.split("://", 1)[1]
            auth = after_scheme.split("@", 1)[0]
            if ":" in auth:
                user, pwd = auth.split(":", 1)
                # Replace the password first - it's the longer,
                # higher-entropy token and more dangerous to leak.
                # Guard against accidentally matching short common
                # words by requiring at least 6 characters; iProyal
                # passwords are always longer than that. Ditto for
                # usernames (their format is alnum+, 12-20 chars).
                if pwd and len(pwd) >= 6:
                    out = out.replace(pwd, "***")
                if user and len(user) >= 6:
                    out = out.replace(user, "***")
        except Exception:  # noqa: BLE001
            pass
    return out


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
