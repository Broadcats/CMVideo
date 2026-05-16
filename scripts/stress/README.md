# Multi-extractor stress test

Tests the full Tier 1-7 fallback chain against a list of 100+ video
sites covering mainstream tube/streaming, news, Asian portals,
adult tubes, social media, and live streams.

## Run

```bash
# from repo root, in a venv with all extractors installed
python scripts/stress/stress_extractors.py
```

Two phases:

1. **Phase 1**: yt-dlp metadata probe on every URL, 8 workers in
   parallel. Fast (~3 minutes for 100 URLs). Tells us which sites
   yt-dlp itself covers without help.
2. **Phase 2**: Full fallback chain (yt-dlp → gallery-dl → cobalt →
   lux → you-get → streamlink → playwright) on the first 30 URLs
   that Phase 1 couldn't resolve. Slow (~3 minutes per URL worst
   case, mostly Playwright). Tells us how much extra coverage the
   non-yt-dlp tiers buy us.

LLM tier (Tier 8) is intentionally skipped here because it
requires API credentials. Run separately with:

```bash
CMVIDEO_LLM_BASE_URL=https://api.groq.com/openai/v1 \
CMVIDEO_LLM_API_KEY=$GROQ_KEY \
CMVIDEO_LLM_MODEL=llama-3.3-70b-versatile \
python scripts/stress/stress_extractors.py
```

## Reading the results

`results.json` is the full JSON dump (raw per-URL probe results +
chain results). The summary banner at the end of the run is the
short version.

### Latest snapshot (107 sites, May 2026)

```
=== Combined coverage estimate ===
  Direct yt-dlp success:             32/107 (30%)
  Sample-tested chain recovery:      6/30  (20%) of yt-dlp failures
  Extrapolated recoveries:           ~15 sites if chain ran on all 75 failures
  Estimated total combined coverage: ~47/107 (44%)
```

Phase 1 failure breakdown (75 URLs):

| Reason | Count |
|--------|-------|
| Stale test URL — host returns 404 | 29 |
| Other (yt-dlp extractor bug, host changed shape) | 27 |
| yt-dlp extractor bug / outdated parser | 4 |
| yt-dlp doesn't know the site | 3 |
| Cloudflare anti-bot 403 | 3 |
| Live stream offline at test time | 3 |
| Site requires login / cookies | 2 |
| DRM-protected content (refuses) | 2 |
| Video deleted on host | 1 |
| GFW / regional block | 1 |

The 30% raw yt-dlp number understates yt-dlp's real coverage
because the test URL list is heavy on yt-dlp's own test suite,
which was last refreshed years ago. Many "failures" are link rot
(host returns 404 for old IDs), DRM, login-walls, or live streams
that aren't currently live. With fresh URLs the number would be
much higher.

The interesting number is the **chain recovery rate**: when yt-dlp
legitimately can't deliver, the rest of the chain wins ~20% of
the time. Tier-by-tier wins from the latest run:

| Tier | Sites recovered |
|------|-----------------|
| playwright | 5 (SpankBang, Drtuber, Beeg, Rumble, Snapchat) |
| gallery-dl | 1 (Tumblr) |
| cobalt / lux / you-get / streamlink | 0* |

\* In this run. They DO win on certain niche sites but those
weren't in the yt-dlp-failure subset Phase 2 sampled.

### What Tier 8 (LLM) adds

Phase 2 still leaves ~80% of yt-dlp's failures uncovered. Most of
those are Cloudflare 403s and "Playwright saw no media manifests
on this page" — i.e. pages where the player loads the manifest
only after a complex user gesture, or hides it behind obfuscated
JavaScript. The LLM tier is designed for exactly that scenario:
it sees the same HTML + network log Playwright captured, and
reasons over them like a human inspecting DevTools.

Smoke-tested locally with a fake LLM server: succeeds end-to-end
on w3schools' HTML5 video page. Real-world coverage gain on the
remaining ~80% requires a paid/free API key and is left for live
testing.
