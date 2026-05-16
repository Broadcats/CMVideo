# Changelog

All notable changes to CMVideo are recorded here. The project follows
[Semantic Versioning](https://semver.org/) once it leaves the alpha series.

## [0.4.16.4-alpha] - 2026-05-16

Rate-limit ease-up + env-var override.

### Changed

- **Default per-IP submit cap raised from `5/hour` to `20/hour`**
  (4x). The original 5/hour was too tight for power users -
  one full-length playlist could burn the whole budget. With
  the owner-IP allowlist retired (the operator removed
  `CMVIDEO_OWNER_IPS` from HF secrets), the public default now
  has to actually be usable for the operator themselves, not
  just defensive against worst-case abuse.
- **`RATE_LIMIT_PER_HOUR` is now env-var driven**
  (`CMVIDEO_RATE_LIMIT_PER_HOUR`). Format is whatever slowapi's
  `Limiter.limit()` accepts: `<int>/<unit>` where unit is
  `hour | minute | second | day`. Validated implicitly at
  decorator parse time on app boot, so a malformed value
  fails loud rather than silently. The HF Space operator can
  now tune up or down without a redeploy.
- The `;2/minute` burst guard layered on at the route
  decorators is unchanged. So the per-hour cap can't be spent
  in a single 30-second window even with the higher hourly
  budget.

### Frontend

- `site/app.js mapHttpError` 429 message updated to be
  number-agnostic ("Hit the per-hour mini-app cap. Try again
  later, or grab the desktop app below for unlimited jobs.")
  so future tuning of the env var doesn't drift the user-
  visible cap message out of sync. The live cap value is
  still advertised on `/api/limits.rate_limit` if a future UI
  wants to surface it.
- `site/index.html` "Mini caps" eyebrow text updated
  cosmetically to `20 jobs/hour`.

### Notes

- Abuse mitigation hierarchy still intact: `RATE_LIMIT_PER_HOUR`
  (slow signal) -> `;2/minute` burst guard (fast signal) ->
  `JOB_MAX_PER_IP=1` (concurrency cap) -> `JOB_MAX_INFLIGHT=3`
  (global box budget) -> `FAILURE_THRESHOLD=5 in 600s ->
  300s cooldown` (failure-loop guard). All of these layer
  independently; a misuser hitting any one gets stopped.
- If 20/hour turns out to be too generous in the wild, drop
  it via `CMVIDEO_RATE_LIMIT_PER_HOUR=10/hour` on the Space
  - no redeploy needed.

## [0.4.16.3-alpha] - 2026-05-16

Mobile-fix hotfix on top of v0.4.16.2-alpha.
**The "mini service is offline" message users were seeing on
mobile after submitting a job was a misdiagnosis caused by the
backend's `_get_job` IP-scope check.** Fixed: the check is
gone (it was breaking legit users on every CGNAT carrier on
the planet, while contributing zero real security on top of
the 32-byte unpredictable token that's already the access
boundary).

### Fixed

- **Mobile users got "mini service is offline" mid-flow.**
  Reproduced on the user's iPhone with a thisvid URL: submit
  succeeded, polling failed with 404, frontend mapped 404 to
  the "offline" message. Diagnosed the actual path:
    1. `POST /api/process?async=1` from mobile cellular ->
       succeeds, job stored with `_client_ip = <egress_A>`.
    2. `GET /api/jobs/{id}` 700ms later -> mobile network
       has shifted egress (CGNAT pool rotation, LTE/5G
       handoff, NAT timeout, carrier load-balancing - all
       normal on mobile, can happen between successive
       requests on the same TCP socket).
    3. `_get_job` saw `<egress_B> != <egress_A>` and
       returned 404 with deceptive `"That job has expired
       or never existed."` text - same response as a real
       missing job_id, by design (so an attacker who knew a
       job_id couldn't tell whether it had ever been valid).
    4. Frontend `mapHttpError` mapped 404 -> "mini service
       is offline" -> red error text.

  None of /healthz, the routes, or replica routing were
  actually broken. The user's offline message was the result
  of a security check that was paranoid about an attack
  scenario (someone guessing a 32-byte unpredictable token,
  ~10**77 possibilities) while breaking real users every
  ~700ms on mobile.

  **`_get_job` no longer enforces IP equality.** The token IS
  the access boundary. `client_ip` is still accepted by the
  function for forensics-only use - mismatches are logged at
  INFO level so any genuine leaked-token incident shows up in
  ops logs - but the request is allowed to proceed. Verified
  against 4 in-process unit cases (same IP, shifted IP, empty
  creator IP, missing job).

- **`site/app.js mapHttpError` 404 message updated.** "Mini
  service is offline" was misleading even before this fix
  because /healthz is independent and the page-load
  `probeService` would have already shown the offline panel
  if the service were truly down. The message now says "That
  download seems to have expired - click Download again to
  retry" which matches what a real 404 from the now-narrowed
  set of 404 sources actually means.

### Notes

- This makes `_get_job(client_ip=...)` purely advisory. The
  parameter is kept on the call sites and the function
  signature so a future ownership boundary (signed URLs,
  cookies, HMAC nonces) can layer on cleanly without churn.
  Don't bring back IP-equality - it's broken by design on
  CGNAT and will silently re-introduce this bug on every
  mobile user.
- `JOB_TTL_SECONDS = 30 min` and the in-memory `_jobs` dict
  are unchanged. A real 404 on poll/fetch now means the job
  truly evaporated (deploy / TTL / typo). Those are rarer
  and the new message handles them honestly.

## [0.4.16.2-alpha] - 2026-05-16

Pre-deploy bug audit on top of the v0.4.16.x security work.
Three real bugs caught and fixed before redeploying. Also folds
in a couple of low-severity consistency fixes from the same
audit pass. **Supersedes v0.4.16.1-alpha as the deploy target -
v0.4.16.1's mini-app should never go live; deploy v0.4.16.2
directly.**

### Fixed

- **`/api/download` JSON shim returned 422 in production**, had
  for many releases. The route's docstring claimed it was a
  back-compat **JSON** shim for old clients. The signature said
  `format: str = Form(...)`, `url: str = Form(...)`. The bundled
  HF Space page (`web-mini/static/app.js`) sent JSON. Result:
  every Download click on the standalone HF page got a 422 from
  FastAPI's body validator before reaching any handler code.
  Verified live against the deployed mini before patching:
    ```
    $ curl -X POST .../api/download -H content-type:application/json \
        -d '{"url":"...","format":"mp4"}'
    HTTP 422 {"detail":[{"type":"missing","loc":["body","format"]...
    ```
  Replaced with a Pydantic body model so the on-wire shape
  matches the docstring and the live caller. The website widget
  on cmvideo.online wasn't affected (it uses
  `/api/process?async=1` with FormData), which is why this bug
  hid for so long.
- **Latent crash in the same handler**: when the JSON shim was
  fixed the call delegating to `api_process` left `fps` and
  `quality` defaulted, but FastAPI's `Form(...)` defaults are
  sentinel objects (not strings) when the route function is
  called directly (i.e. not parsed through a request). So the
  very first line of `api_process` did
  `(fps or "source").lower()` on a `Form` instance and got
  `AttributeError: 'Form' object has no attribute 'lower'`.
  Caught by the smoke test, not the audit. Now passes
  `fps="source"`, `quality=DEFAULT_QUALITY` explicitly.
- **HD download cap mismatch (1080p tier silently rejected at
  the final gate)**. `_run_pipeline_async` (line ~1320) and
  sync `api_process` (line ~1777) both raise the `max_size`
  passed into `_do_download` to `QUALITY_DOWNLOAD_CAPS["hd"]`
  (1500 MB) when the user picks the HD tier. But the **final
  filesize gate** at lines ~1447 and ~1819 was still using
  `MAX_DOWNLOAD_FILESIZE_BYTES`, which is the *standard* tier
  (800 MB). So a legitimate 1080p job that pulled a real ~1.2
  GB file completed the download, then **failed at the last
  size check** with `"Output exceeds the 800 MB cap"` -
  misleading because the user explicitly selected HD. Both
  gates now compute `active_cap` from the same per-quality
  `QUALITY_DOWNLOAD_CAPS` lookup that sized `max_size`.

### Hardened

- **XFF parser docstring now honest about its limitation.**
  v0.4.16.1's docstring implied the fix made header forgery
  "impossible at the edge HF controls". That's true ONLY if
  the chain is at least `_TRUSTED_PROXY_HOPS` entries long,
  which on HF Spaces is automatic (Caddy always appends its
  peer), but is NOT automatic on a bare-metal direct deploy
  with no proxy. Added a worked example, called out the
  limitation explicitly, and changed the `len(chain) < hops`
  fallback from `chain[0]` to the socket peer - small
  defense-in-depth improvement on misconfigured deploys, no-op
  on HF.
- **`web-mini/extractors.py` `streamlink_download` now resolves
  ffmpeg through `_resolve_tool`** instead of a bare `"ffmpeg"`
  literal. Mirrors the desktop fix shipped in v0.4.16-alpha;
  keeps mini-app + desktop binary-resolution behaviour
  symmetric, so a bundled ffmpeg in either tree wins over a
  stale system PATH entry.
- **`site/app.js` YouTube URL detection now matches
  `youtube-nocookie.com`** in addition to youtube.com / youtu.be.
  Previously censor-mode `youtube-nocookie.com` URLs slipped
  past the YT short-circuit and got dispatched to the generic
  pipeline, which then errored out. Also rewrote the misleading
  block comment that still described an in-browser iframe
  player flow that was removed in v0.4.16-alpha.

### Verified

A 10-case FastAPI TestClient smoke run gates this release:
- `/api/download` with JSON body: not 422, not 500.
- `/api/limits` exposes `quality_download_caps_mb.hd >= 1500`.
- HD cap source-pattern audit: 4 occurrences of the per-quality
  lookup (2 for `max_size`, 2 for `active_cap`).
- XFF spoofing matrix: forged + appended -> blocked; padded
  forgery -> blocked; legitimate single-entry HF-shape -> picks
  user IP; no XFF -> socket peer.
- `/api/limits` body grepped for `api.cerebras.ai`,
  `api.openai.com`, `api.groq.com`, `cobalt.tools`, `fly.dev`,
  `onrender.com` - all clean (v0.4.16's redaction holds).
- Python `py_compile` + import smoke for desktop and mini.
- `node --check site/app.js` - JS syntax OK.

## [0.4.16.1-alpha] - 2026-05-16

Hotfix on top of v0.4.16-alpha. The XFF parsing fix that
v0.4.16 introduced as the **headline HIGH-severity item** had
an off-by-one that left the original spoofing vulnerability
in place under a slightly different shape. v0.4.16-alpha
should be treated as **not actually fixing** the
`X-Forwarded-For` issue; v0.4.16.1-alpha is the working fix.

### Fixed

- **`_client_ip` correctly picks the
  `_TRUSTED_PROXY_HOPS`-th-from-rightmost XFF entry now**, not
  the (n+1)th. Working example with `hops=1` (HF Spaces edge):
  attacker sends `X-Forwarded-For: <victim>`. HF Spaces appends
  the attacker's real IP. Backend sees the chain
  `<victim>, attacker_ip`. The fixed code returns
  `chain[-1] = attacker_ip` (the real client). v0.4.16's broken
  code did `len-1-hops = 0` and returned `chain[0] = <victim>`
  (the spoofed value) - i.e. the exact bug the v0.4.16 entry
  claimed to close.
- Verified against a 7-case attack matrix:
  * Legitimate single-hop (`real_client`) -> `real_client`.
  * 1 forged entry (`victim, attacker`) -> `attacker`.
  * 3 forged entries (`a, b, c, attacker`) -> `attacker`.
  * Empty XFF -> socket peer fallback.
  * Cloudflare + HF (`real, cf`) -> `real`.
  * Cloudflare + HF with 1 forged -> `attacker` (not `victim`).
  * Cloudflare + HF with 5 forged -> `attacker`.
- Documentation now spells out the worked example so any future
  refactor has the test cases inline as code comments, not
  just in this changelog entry.

### Notes

- `CMVIDEO_TRUSTED_PROXY_HOPS` minimum is now clamped at 1
  (was 0). Setting it to 0 would mean "no trusted proxies in
  front" which on a public HF Space is wrong by definition,
  and would silently re-introduce the leftmost-trust pattern
  this entire fix was designed to remove.
- Recommend redeploying the mini-app immediately so the
  v0.4.16-alpha image (which has the broken formula) is
  replaced. Owner-IP allowlist users on v0.4.16 should treat
  their bypass as effectively bypassable until that's done.

## [0.4.16-alpha] - 2026-05-16

Security maintenance pass across the mini-app, desktop config
store, and static site. Findings discovered during a structured
audit; nothing here is in-the-wild exploited, but each item moves
the codebase a measurable step forward in the threat model.

### Mini-app (high)

- **`X-Forwarded-For` parsing now resists header forgery.** The
  previous implementation always took the leftmost entry, so any
  caller could send `X-Forwarded-For: <victim>` and inherit the
  victim's rate-limit bucket. With the owner-IP allowlist
  granting unlimited jobs to listed IPs, that turned a single
  HTTP header into an unauthenticated "skip every gate" key.
  `_client_ip` now picks the (n+1)th-from-rightmost entry, where
  n = `CMVIDEO_TRUSTED_PROXY_HOPS` (default 1 for HF Spaces' edge
  alone). Forged left-side hops are dropped, the real client IP
  is what reaches the rate limiter, and the owner-IP bypass is
  spoof-proof at the edge HF controls. Bump the env var to 2 if
  you ever front the Space with a second trusted proxy
  (Cloudflare etc).

### Mini-app (medium)

- **`/api/limits` no longer leaks the LLM endpoint or Cobalt API
  base URL.** `tool_versions()` previously returned
  `versions["cobalt"] = COBALT_API_BASE` and
  `versions["llm"] = f"{model} @ {LLM_BASE_URL}"`. For
  self-hosted instances those URLs can be internal hostnames or
  fingerprint the provider (Groq vs Cerebras vs OpenAI); both
  are now redacted to `"configured"`. `llm_status()` drops
  `base_url` from its payload entirely.
- **Cobalt media-URL fetch now enforces a streaming byte cap.**
  Previously `cobalt_download` did `urllib.urlopen` +
  `shutil.copyfileobj` with no upper bound, so a malicious or
  compromised Cobalt instance could hand back a `media_url`
  pointing at an arbitrarily large response and watch the HF
  Space exhaust ephemeral disk. New code threads `max_filesize`
  from the dispatcher (defaults to the per-quality-tier cap),
  rejects up-front when `Content-Length` exceeds the cap, and
  aborts mid-stream the moment the running counter crosses it.
  Cleans up the partial file on abort.
- **CORS allowlist now production-only.** `localhost:8080` /
  `localhost:5500` etc. are gated behind
  `CMVIDEO_DEV_CORS=1` so the deployed Space doesn't volunteer
  dev origins to the browser. No exploit path on its own (browser
  SOP still applies), just a tighter live response.

### Mini-app (low)

- **lux `-stream` and you-get `--format` IDs now validated
  against `^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$`.** Defence-in-depth
  against argv-injection. The values come from parsing the
  tool's own enumeration output and *should* always be tame
  alphanumerics, but if upstream ever changed format and
  surfaced something starting with `-`, the next argv slot
  would treat it as a fresh CLI flag (`--cookie=...`,
  `-i ...`, etc.). Anything that fails the regex falls through
  to the tool's default behaviour with a warning log.

### Desktop (medium)

- **`streamlink_download` resolves `ffmpeg` through `_resolve_tool`
  instead of using a bare `"ffmpeg"` string.** In bundled
  AppImage / Windows builds `_bootstrap_bundle_paths()` prepends
  the bundled tool dir to `PATH`, so the actual binary picked
  was already correct - this change just makes resolution
  explicit and consistent with every other tool in the chain.
  Reduces the "what did `subprocess.Popen` find on `PATH` at
  exec time" surprise on dev installs.

### Desktop (low)

- **`config.json` now gets owner-only ACLs on Windows.** POSIX
  has had `0o600` since 0.4.x; Windows used to inherit whatever
  ACL `%APPDATA%\\CMVideo` had, which on a fresh user profile
  is "Users group readable through inheritance". `save_config`
  now applies an `icacls /inheritance:r /grant <user>:(F)`
  pattern equivalent to POSIX 0o600. Best-effort: if `icacls`
  is missing or fails, the save still succeeds (the file is
  still under `%APPDATA%`, not `C:\\`). Matters for the
  ElevenLabs API key stored in plaintext in this file.

### Site (low)

- **Removed dead YouTube iframe-API code** (`loadYouTubeIframeAPI`,
  `runYouTubeCensorFlow`, `renderEmbedPlayer`, mute scheduler).
  The iframe path was never wired into the submit flow - YT
  censor mode redirects to the desktop app and YT download mode
  goes through the regular `/api/process` pipeline. Keeping the
  unused code in the bundle expanded the attack surface for no
  benefit (a third-party script source, global `YT.Player`
  scope, an intervalled timer left running). The
  `https://www.youtube.com/iframe_api` script is no longer
  loaded from the homepage. If the iframe path is ever revived,
  restore from git history at v0.4.15.3-alpha. The CSP allowlist
  is unchanged (still includes youtube.com for the embedded
  hero player).

### Audit notes

Two findings were investigated and confirmed as **false positives**
- not shipping fixes:

- The `response: Response = ...` annotation in
  `security_headers` middleware was flagged as a runtime
  `NameError`. Function-local annotations are NOT evaluated at
  runtime in CPython (PEP 526 only stores them at module / class
  level). The annotation is parsed but never looked up, so the
  middleware works correctly. Type-checker concern only.
- `SlowAPIMiddleware` was flagged as missing. It's only required
  when you want `default_limits` to apply globally without
  decorators. With `app.state.limiter = limiter`, the
  `RateLimitExceeded` exception handler registered, and per-
  endpoint `@limiter.limit(...)` decorators on every public
  endpoint, the rate-limit enforcement path is fully wired and
  the middleware is unnecessary.

## [0.4.15.3-alpha] - 2026-05-16

Mini-app: stop hard-blocking YouTube downloads; route YT through
the residential proxy now that 0.4.15 made that available.

### Changed

- **YouTube + download mode no longer short-circuits to the
  desktop-app section.** Previous behaviour was a frontend block
  that returned before any backend call - introduced when YT
  was 100% broken from the HF Space's datacenter IP. With the
  residential proxy now active, YT downloads have a real chance
  (success rate roughly 5% direct -> 60-70% proxied for
  unrestricted videos), so the mini lets the request through
  and the backend does the actual attempt.
- **YouTube + censor mode (silence / beep) still redirects to
  the desktop app.** Censor needs the transcript API, which is
  more fragile than the video download even with a proxy, and
  the in-browser iframe alternative isn't fully wired yet. The
  redirect message now explicitly tells the user "plain MP4 /
  MP3 download from YouTube is supported - just switch the mode"
  so they know download mode does work.
- **`youtube.com`, `youtu.be`, `googlevideo.com`, `ytimg.com`
  added to `PROXY_DOMAINS`.** All four domains now route through
  the residential proxy when `CMVIDEO_RESIDENTIAL_PROXY` is
  configured, just like the existing IG / TT / FB entries.

## [0.4.15.2-alpha] - 2026-05-16

Site: fix the recurring "version reverted to 0.4.7" bug + automate
version-stamp bumping.

### Fixed

- **Bumped 7 hardcoded `0.4.7-alpha` references in `site/index.html`
  to `0.4.13.5-alpha`** (the latest GitHub Release with desktop app
  binaries). Asset names match across releases so the per-OS
  download URLs work cleanly.

### Added

- **`sync-gh-pages.sh` auto-rewrites version stamps at deploy time.**
  Resolves the latest release tag via
  `gh release list --limit 1 -R Broadcats/CMVideo` and rewrites
  every `vX.Y.Z-alpha` reference in the gh-pages copy of
  `index.html` to match. Source `site/index.html` is unchanged -
  the substitution lives only in the published artefact. Falls
  back gracefully when the `gh` CLI is unavailable (rather than
  breaking the deploy). This stops the recurring "site shows
  ancient version" bug for good - the website will always match
  the latest published desktop release on every gh-pages sync.

## [0.4.15.1-alpha] - 2026-05-16

Hotfix: `proxy_router.py` wasn't being copied into the container.

### Fixed

- **Dockerfile**: added `proxy_router.py` to the explicit COPY list
  on line 62. The 0.4.15 deploy uploaded the file to the HF repo
  but the build's COPY was only enumerating the original four
  Python files (`app.py`, `mini_censor.py`, `extractors.py`,
  `llm_extract.py`), so the runtime container was missing the
  module and crashed at uvicorn startup with
  `ModuleNotFoundError: No module named 'proxy_router'`.
  Pure ops fix; no functional change.

## [0.4.15-alpha] - 2026-05-16

Mini-app: per-domain residential-proxy routing for sites that
throttle datacenter IPs (Meta family, TikTok, X, the MindGeek
tubes, East-Asian video portals).

### Added

- **New `web-mini/proxy_router.py` module.** Decides per-URL
  whether a given outbound fetch should be routed through a
  residential proxy. Built-in `PROXY_DOMAINS` allowlist covers
  Instagram + Facebook + Threads (`cdninstagram.com`, `fbcdn.net`,
  `instagram.com`, `facebook.com`, `fb.watch`, `threads.net`),
  TikTok (`tiktok.com`, `tiktokcdn.com`, `muscdn.com`,
  `tiktokv.com`), X / Twitter (`x.com`, `twitter.com`,
  `twimg.com`), MindGeek tubes (`pornhub.com`, `redtube.com`,
  `youporn.com`, `tube8.com`, `phncdn.com`, `ypncdn.com`,
  `rdtcdn.com`, `tube8cdn.com`), other tube sites (xVideos,
  xnxx, xHamster, Spankbang) and East-Asian portals (Bilibili,
  Douyin, iQiyi, Youku, Weibo, Niconico). YouTube / Reddit /
  Vimeo / Twitch / direct media URLs are deliberately NOT on the
  allowlist - they work fine on datacenter IPs and proxying them
  would burn paid GB for no benefit.
- **`CMVIDEO_RESIDENTIAL_PROXY` env var.** Full HTTP proxy URL
  with embedded credentials, e.g.
  `http://USERNAME:PASSWORD@geo.iproyal.com:12321`.
  Unset = proxy disabled, every fetch goes direct (current
  behaviour, preserved as the safe default).
- **`CMVIDEO_PROXY_EXTRA_DOMAINS` env var.** Optional CSV of
  additional hostname suffixes to route through the proxy on top
  of the built-in allowlist, for operator-side experimentation.
- **Wired into all four extraction tiers:** yt-dlp (`proxy` opt),
  Playwright (`launch(proxy=...)` parsed into the
  server / username / password format Playwright expects),
  ffmpeg (`http_proxy` / `https_proxy` env on subprocess), and
  the HLS master playlist resolver (`urllib.request.ProxyHandler`).
  All four gate on the same `should_proxy(url)` check, so the
  routing decision is consistent across the chain.
- **`/api/limits` -> `hardening.residential_proxy`** with
  `{active, builtin_domains, extra_domains}`. Active is a
  boolean, the others are counts. **The proxy URL itself is
  never exposed** because it contains credentials.

### Hardening notes

- **Per-URL decision uses the destination URL's hostname**, not
  the original page URL. So when Playwright captures a
  `cdninstagram.com` media URL on an `instagram.com` page, the
  ffmpeg fetch of the cdninstagram.com URL is also proxied -
  which is exactly when proxying matters most (CDN segment
  fetches are where IG actually rate-limits).
- **Random IP rotation per request** (configured at the IPRoyal
  / Webshare side) means a hostile site can't quota-stack our
  requests onto one residential IP. Each yt-dlp run sees a
  fresh IP.
- **Cost discipline:** at hobby scale (~50 downloads/day,
  ~20% of which hit proxy domains), expected proxy bandwidth
  is ~5-8 GB/month, comfortably inside IPRoyal's $7 starter pack.

## [0.4.14.4-alpha] - 2026-05-16

Site widget: switch mini-app endpoint from
`dandyfeet-cmvideo-mini.hf.space` to the custom domain
`mini.cmvideo.online`.

### Changed

- **`MINI_API_BASE` -> `https://mini.cmvideo.online`** (was the
  raw HF Spaces subdomain). The custom domain is a CNAME pointing
  at `hf.space` provisioned via HF Pro tier. Net effect: cleaner
  URL in browser network panel, no functional change.
- **CSP `connect-src` widened to allow both hosts.** The new
  custom domain is now the active call target; the old
  `dandyfeet-cmvideo-mini.hf.space` is kept on the allowlist as a
  safety hatch - if Pro lapses or the custom domain ever breaks,
  flipping `MINI_API_BASE` back is a one-line change with no CSP
  redeploy chain.

## [0.4.14.3-alpha] - 2026-05-16

Site widget: per-URL submit cooldown to stop users accidentally
inducing rate-limit failures on throttle-happy sites (IG / FB /
TT / X / Threads).

### Added

- **Same-URL cooldown in `site/app.js`.** The mini-app shares one
  outbound IP across every visitor; sites like Instagram start
  throttling after a handful of same-URL hits. The canonical bug:
  a user pastes a reel and tests 720p -> 1080p -> MP3 in 60s, the
  first call succeeds, the rest hit "rate-limit reached". Quality
  has nothing to do with it; the source site simply blocked our
  IP for that URL. New `checkSameUrlCooldown()` refuses same-URL
  resubmits inside a small in-memory cooldown window:
  * **15s** for the long tail of supported sites.
  * **60s** for known throttle-heavy domains: instagram.com,
    facebook.com / fb.watch, threads.net, tiktok.com,
    x.com / twitter.com.
  Lives in plain JS state (not localStorage) so a hard refresh
  resets it - this is a guardrail, not a punishment. URL-only
  flow; file uploads bypass entirely.
- **Friendly cooldown message.** Tells the user *why* they're
  being throttled by their own browser (so they understand why
  the source site is throttling us) rather than just "wait":
  > "You just submitted this URL. Wait Ns before retrying -
  >  rapid resubmits trigger the source site's anti-scraping
  >  limits and make every following attempt fail."
- **URL normalization for cooldown matching.** Strips query
  string + fragment + trailing slash + lowercases the host before
  the lookup, so `instagram.com/reel/abc/?utm_source=ig_web` and
  `https://www.instagram.com/reel/abc` count as the same URL.

## [0.4.14.2-alpha] - 2026-05-16

Mini-app: Instagram CDN allowlisting + readable ffmpeg error
messages. Follow-up to 0.4.14.1's Instagram fast-path.

### Fixed

- **`cdninstagram.com` (and friends) added to the LLM tier's CDN
  allowlist.** Previously the LLM extractor's anti-SSRF guard
  refused to fetch `scontent-iad3-2.cdninstagram.com` (and every
  other `scontent-*-*.cdninstagram.com` Meta CDN host) because
  none of them suffix-matched the existing allowlist. Adding
  `cdninstagram.com` and `instagram.com` to `_BUILTIN_CDN_SUFFIXES`
  lets the LLM tier follow IG media URLs Playwright captures.
- **ffmpeg error message extraction.** Playwright failures used
  to surface as
  `ffmpeg refused the captured manifest: fcyl6MjEsImJpdHJh...&ccb=17-1`
  - i.e. just the truncated tail of whichever Instagram CDN URL
  was being copied at the failure point. New
  `_summarize_ffmpeg_error()` helper walks the stderr looking for
  known diagnostic patterns (`HTTP error 403`,
  `Connection refused`, `Server returned 403 Forbidden`, etc.)
  and surfaces the first match. Falls back to the last short line
  if nothing matches, never just the URL-spam tail.
- **Hostile-domain user-facing error.** When every tier fails on
  Instagram / Facebook / Threads, the user now sees:
  > "Instagram blocked the download. Public reels / videos
  > sometimes work, but logged-in-only, private-account, or newer
  > posts won't extract without session cookies."
  Instead of the raw stack-trace-tier last error, which is more
  honest about *why* it failed (it's IG, not a CMVideo bug).

## [0.4.14.1-alpha] - 2026-05-16

Mini-app: fix the "stuck at 5%" stall on Instagram / Facebook /
Threads URLs.

### Fixed

- **Auth-hostile domain fast-path.** Instagram, Facebook, and
  Threads block unauthenticated scraping for most reel/video URLs.
  The dispatcher used to walk the full fallback chain
  (yt-dlp -> gallery-dl -> cobalt -> lux -> you-get -> playwright)
  on these sites, with each tool failing in turn after its own
  timeout. gallery-dl alone could sit on an IG reel for 180s
  before giving up. Total user-visible wait: ~3-4 minutes parked
  at "5%". New `AUTH_HOSTILE_DOMAINS` set short-circuits IG / FB /
  Threads from yt-dlp directly to Playwright (which actually loads
  the page in a real browser, so public reels work). Wait time
  drops to ~30-60s and the dispatcher emits a clear log line
  `dispatcher: hostile domain ...; skipping cli fallbacks` so the
  reasoning is visible in failure traces.
- **Forward-stepping progress bar during fallback.** The
  per-attempt callback used to *reset* the pct to `max(5, pct//2)`
  when a tool failed and we moved to the next one - which on a
  doomed-to-fail chain looked like "5%" parked motionless for
  minutes. Now each fallback steps the bar forward by 5-8 points
  (capped at 60) so the user sees steady upward motion that
  signals "the system is still doing things".

## [0.4.14-alpha] - 2026-05-16

Mini-app: owner-IP allowlist for unlimited maintainer testing on
the deployed Hugging Face Space, without weakening abuse gates for
anyone else.

### Added

- **`CMVIDEO_OWNER_IPS` env var.** Comma-separated list of IPs and
  /CIDR ranges (mixed v4 + v6 fine) that bypass the per-IP rate
  limits, the per-IP in-flight job cap, and the per-IP failure
  cooldown. Empty / unset = nobody is privileged (default).
  Example: `CMVIDEO_OWNER_IPS="203.0.113.42, 2001:db8::/64"`.
- **`/api/limits` -> `hardening.owner_allowlist_size`** (count
  only - never echoes the entries) and
  **`hardening.owner_bypass_active`** (true if the caller's IP
  matches). Hit `/api/limits` from your IP to confirm it's working.

### Hardening notes

- Owner IPs **still respect**:
  * `JOB_MAX_INFLIGHT` (the global cap of 3) - so even maintainer
    testing can't melt the box for everyone else.
  * The kill-switch (`CMVIDEO_MINI_DISABLED`) - so you can still
    panic-shutdown the service.
  * The User-Agent gate - no point bypassing.
  * Duration / size / quality caps - same: protects the box, and
    protects you from accidentally queueing a 10 GB pull.
- Slowapi rate limits are bypassed via a custom `key_func` that
  hands each owner request a fresh random key, so the lookup
  always finds an empty bucket. Costs ~microseconds per request.
- Owner failures are not recorded into the cooldown deque (saves
  memory and avoids the gate ever firing if `_is_owner_ip` later
  returns false for any reason).

## [0.4.13.5-alpha] - 2026-05-16

Mini-app: bump MP3 to top quality + per-candidate diagnostic
logging in the Playwright tier (groundwork for chasing the
remaining thisvid edge case).

### Changed

- **MP3 bitrate raised from 192 kbps -> 320 kbps.** 320 kbps is
  the highest standard MP3 bitrate; the file size goes up
  proportionally (~1.6x at the same duration) but the audio is
  effectively transparent. Affects the chip label
  (`MP3 · 320k`), `/api/info` / `/api/limits` payloads, and the
  caps blurb on the homepage. The standalone HF page picks up
  the change automatically through its template variable.

### Diagnostics

- **`_log_candidates`**: every captured Playwright candidate is
  now logged at INFO with its parsed height hint, container, and
  response size. Two log lines per candidate (one before ranking,
  the picked one after) so the HF Space logs leave a clear trail
  for any "still low quality" report. Previously we only logged
  the winner, which made it impossible to tell whether the
  scorer was picking wrong or whether Playwright never saw
  higher-resolution variants in the first place.
- **HLS variant trail**: when `_resolve_hls_variant` parses a
  master playlist, it now logs every `(height, bandwidth, url)`
  triple it saw before announcing the pick. Same purpose - if a
  thisvid pull gives 360p, the logs now tell us whether the
  master only listed 360p (Playwright didn't catch the higher
  variant) vs the parser picked badly.

### Note for the thisvid follow-up

The user reported that pasting thisvid's *embed* URL works while
the page URL still gives low quality. That's a hint Playwright's
default page load isn't seeing the same `<video>` source the
embed iframe does. Next investigation: check whether the embed
URL surfaces a master HLS that the page URL doesn't, or whether
mobile UA detection is steering the page toward a low-res
fallback. The new diagnostic lines should make that question
trivially answerable from the HF logs.

## [0.4.13.4-alpha] - 2026-05-16

Mini-app: close the resolution-cap gap on the remaining four
extractor tiers (cobalt, lux, you-get, gallery-dl). Combined with
v0.4.13.2 (yt-dlp tier) and v0.4.13.3 (Playwright tier), every
extractor in the chain now respects the user's 720p / 1080p
choice instead of returning whatever the tool's server-side
default happens to be.

### Added

- **Cobalt** now sends `videoQuality` matching the requested cap
  (720 / 1080) instead of a hardcoded "720". Maps the cap to
  Cobalt's nearest accepted bucket
  (144/240/360/480/720/1080/1440/2160).
- **Lux** runs `lux -i <url>` first to enumerate streams, parses
  the markdown-ish output for `(stream_id, height)` pairs, and
  passes `-stream <id>` for the highest variant at-or-below the
  cap. Gracefully falls back to lux's default if enumeration
  output isn't parseable (it varies between versions and sites).
- **you-get** runs `you-get -i <url>` first and passes
  `--format <tag>` for the highest variant at-or-below the cap.
  Same graceful-fallback pattern as lux.
- **gallery-dl** sends per-extractor `-o` overrides:
  `extractor.twitter.video-quality=high`,
  `extractor.bilibili.quality=720`, etc. Image-only extractors
  silently ignore unknown keys, so the spam is safe.

### Why now

v0.4.13.3 fixed Playwright (which catches thisvid and friends),
but the chain has six other tiers that fire for niche sites. The
gap was small in practice but real - someone pulling a Bilibili
clip via lux would silently get 480p regardless of what the
mini's chip said. Closing it makes the height chip a contract,
not a hint.

### Tests

cobalt bucket mapping verified for all six edge cases (cap=1
clamps to 144, cap=4000 clamps to 2160, all standard heights map
through cleanly). All four tier helpers now expose
`target_height` in their signature.

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
