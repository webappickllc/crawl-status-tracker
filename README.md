# Crawl Status Tracker (reusable for any site)

A filterable table showing each page's indexing status, pulled from Google
Search Console + your sitemap. Works for any site you have GSC access to —
just swap the three inputs below.

## Files

- `generate_crawl_status.py` — pulls the data, writes `data.json`
- `index.html` — the front-end table, reads `data.json`
- `data.json` — **sample data**, included so you can open `index.html` right
  now and see it working. Replace it with real output from the script.
- `.github/workflows/refresh.yml` — optional automation to regenerate and
  redeploy on a schedule

## One-time setup (per Google account, not per site)

1. Google Cloud Console → create/select a project → enable the
   **Search Console API**.
2. Create a **service account** → generate a JSON key. Keep this file safe;
   never commit it to a public repo.

## Per-site setup (repeat for each site you want to track)

1. In Search Console, open the property for that site → **Settings → Users
   and permissions → Add user** → paste the service account's email
   (found inside the JSON key file, `client_email` field) → grant
   **Restricted** access. That's all the API needs.
2. Know the property's identifier:
   - Domain property → `sc-domain:example.com`
   - URL-prefix property → `https://example.com/`
3. Know the sitemap URL, e.g. `https://example.com/sitemap.xml` — or export
   a plain `.txt` list of URLs instead.

## Running it

```bash
pip install google-api-python-client google-auth

python3 generate_crawl_status.py \
  --credentials /path/to/service-account.json \
  --site-url "sc-domain:example.com" \
  --sitemap "https://example.com/sitemap.xml" \
  --output data.json
```

Then open `index.html` in the same folder (or host both files together) —
it reads `data.json` automatically.

### Using a URL list instead of a live sitemap

```bash
python3 generate_crawl_status.py \
  --credentials service-account.json \
  --site-url "sc-domain:example.com" \
  --url-list urls.txt \
  --output data.json
```

### Custom page-type classification

By default, URLs are classified by path pattern (`/blog/` → Blog,
`/product/` → Product, `/` → Home, everything else → Page). To customize
per site, pass `--type-rules rules.json`:

```json
[
  ["^/blog/", "Blog"],
  ["^/ctx-feed", "Product"],
  ["^/challan-pro", "Product"],
  ["^/discoplugin", "Product"],
  ["^/?$", "Home"]
]
```

## Automating the refresh (optional)

1. Push this folder to a GitHub repo, enable **GitHub Pages** (serve from
   the repo root or `/docs`).
2. Add a repo secret named `GSC_SERVICE_ACCOUNT_JSON` containing the full
   contents of your service account key.
3. Edit `.github/workflows/refresh.yml` — set the correct `--site-url` and
   `--sitemap` for your site, and adjust the cron schedule if needed.
4. Each run regenerates `data.json` and commits it, so Pages redeploys with
   fresh data and an updated "generated" timestamp automatically.

## Reusing for a second site

Duplicate this folder (or the repo), point step "Per-site setup" and the
`--site-url` / `--sitemap` flags at the new property, and add the new
service account as a user on that property too if you're using a different
Google account. Everything else is unchanged.

## Notes / limits

- The URL Inspection API allows ~2,000 requests/day per property. Fine for
  a few hundred to low thousands of URLs; larger sites need to batch across
  multiple days (the script saves partial progress if quota is hit).
- This is a scheduled snapshot, not truly real-time — "live" means
  "regenerated on a schedule," same as the reference site you shared.
