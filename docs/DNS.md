# Pointing cmvideo.online at GitHub Pages

This file is for you (the maintainer). Users never see it. It captures
the exact DNS records to set at your registrar after the repo is pushed
and Pages is enabled.

## Prerequisites

1. Repo pushed to `github.com/<Broadcats>/CMVideo`.
2. Repo Settings -> Pages: source set to "GitHub Actions" (the
   `Deploy site` workflow handles this automatically after the first
   push to `main`).
3. The `site/CNAME` file in this repo contains `cmvideo.online`. GH
   Pages reads it and configures Let's Encrypt automatically.

## DNS records at your registrar

Set these on the `cmvideo.online` zone. The four A records are the
current published GH Pages IPs - check
<https://docs.github.com/pages/configuring-a-custom-domain-for-your-github-pages-site/managing-a-custom-domain-for-your-github-pages-site>
if any of them ever fail to resolve.

### Apex (root) `cmvideo.online`

| Type | Host  | Value             | TTL  |
|------|-------|-------------------|------|
| A    | @     | 185.199.108.153   | 3600 |
| A    | @     | 185.199.109.153   | 3600 |
| A    | @     | 185.199.110.153   | 3600 |
| A    | @     | 185.199.111.153   | 3600 |
| AAAA | @     | 2606:50c0:8000::153 | 3600 |
| AAAA | @     | 2606:50c0:8001::153 | 3600 |
| AAAA | @     | 2606:50c0:8002::153 | 3600 |
| AAAA | @     | 2606:50c0:8003::153 | 3600 |

If your registrar supports `ALIAS`/`ANAME` flattening (Cloudflare,
DNSimple, Route 53), use a single

| Type   | Host | Value                          |
|--------|------|--------------------------------|
| ALIAS  | @    | Broadcats.github.io     |

instead of the eight A/AAAA rows.

### `www.cmvideo.online`

| Type  | Host | Value                       | TTL  |
|-------|------|-----------------------------|------|
| CNAME | www  | Broadcats.github.io. | 3600 |

(The trailing dot is correct - it tells the resolver this is a fully
qualified name, not a host under your zone.)

## After propagation (1 minute - 24 hours)

1. Repo Settings -> Pages -> Custom domain shows `cmvideo.online` with
   a green check. If it shows a yellow warning, click "Check again"
   after a few minutes.
2. Tick "Enforce HTTPS" once the certificate provisions
   (usually within 15 minutes of DNS resolving).
3. Visit <https://cmvideo.online>. You should see the landing page.
4. <https://www.cmvideo.online> should 301 to the apex (GH Pages does
   this automatically).

## Sanity check from the command line

```bash
dig +short cmvideo.online       # should list the four 185.199.x.153 IPs
dig +short www.cmvideo.online   # should resolve via CNAME to *.github.io
curl -sI https://cmvideo.online | head -1   # HTTP/2 200
```

## If something's wrong

- `dig` still returns your registrar's parking IPs: DNS hasn't
  propagated yet, or you forgot to clear the registrar's default
  records.
- GH Pages "Domain's DNS record could not be retrieved": you set the
  records but the cache is stale; wait 5-10 minutes and click
  "Check again".
- Cert stuck "Pending": disable + re-enable HTTPS in repo Settings.
