#!/usr/bin/env python3
"""
Crawl Status Tracker - data generator
--------------------------------------
Works for ANY site, not just one property. Point it at:
  1. A Google service-account credentials JSON (with access to the GSC property)
  2. A sitemap.xml (or a plain .txt file of URLs, one per line)
  3. The GSC property identifier for that site

...and it produces data.json, which the included index.html renders as a
filterable, sortable crawl-status table.

USAGE
  python3 generate_crawl_status.py \
      --credentials service-account.json \
      --site-url "https://dailyshikkha.com/" \
      --sitemap https://dailyshikkha.com/sitemap.xml \
      --output data.json

  # Or from a plain URL list instead of a live sitemap:
  python3 generate_crawl_status.py \
      --credentials service-account.json \
      --site-url "sc-domain:dailyshikkha.com" \
      --url-list urls.txt \
      --output data.json

NOTES ON site-url
  - Domain properties (recommended, covers http/https/www/non-www):
        sc-domain:example.com
  - URL-prefix properties:
        https://example.com/
  Use whichever matches how the property is set up in Search Console.

RATE LIMITS
  The URL Inspection API is capped at ~2,000 queries/day per property.
  This script sleeps briefly between calls and will stop cleanly if the
  quota is hit, saving whatever it has completed so far.
"""

import argparse
import json
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

# Default URL-pattern -> page-type classifier. Override per-site with --type-rules.
DEFAULT_TYPE_RULES = [
    (r"^/blog/", "Blog"),
    (r"^/case-studies?/", "Case Study"),
    (r"^/(features?|product)/", "Product"),
    (r"^/(services?)/", "Service"),
    (r"^/?$", "Home"),
]


def fetch_url(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "crawl-status-tracker/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def parse_sitemap(sitemap_url: str, seen=None):
    """Recursively parse a sitemap or sitemap index, returns {url: lastmod_or_None}."""
    if seen is None:
        seen = set()
    if sitemap_url in seen:
        return {}
    seen.add(sitemap_url)

    urls = {}
    raw = fetch_url(sitemap_url)
    root = ET.fromstring(raw)

    # Sitemap index: contains <sitemap><loc> entries pointing to child sitemaps
    child_sitemaps = root.findall("sm:sitemap/sm:loc", SITEMAP_NS)
    if child_sitemaps:
        for loc in child_sitemaps:
            urls.update(parse_sitemap(loc.text.strip(), seen))
        return urls

    # Regular sitemap: contains <url><loc> + optional <lastmod>
    for entry in root.findall("sm:url", SITEMAP_NS):
        loc_el = entry.find("sm:loc", SITEMAP_NS)
        if loc_el is None or not loc_el.text:
            continue
        loc = loc_el.text.strip()
        lastmod_el = entry.find("sm:lastmod", SITEMAP_NS)
        lastmod = lastmod_el.text.strip() if lastmod_el is not None and lastmod_el.text else None
        urls[loc] = lastmod

    return urls


def load_url_list(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return {line.strip(): None for line in f if line.strip()}


def classify(url: str, rules):
    from urllib.parse import urlparse
    path = urlparse(url).path or "/"
    for pattern, label in rules:
        if re.search(pattern, path, re.IGNORECASE):
            return label
    return "Page"


def load_type_rules(path: str):
    if not path:
        return DEFAULT_TYPE_RULES
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    # expects [["pattern", "Label"], ...]
    return [(item[0], item[1]) for item in raw]


def inspect_url(service, site_url: str, inspection_url: str):
    body = {"inspectionUrl": inspection_url, "siteUrl": site_url}
    resp = service.urlInspection().index().inspect(body=body).execute()
    result = resp.get("inspectionResult", {})
    index_status = result.get("indexStatusResult", {})
    coverage = index_status.get("coverageState", "Unknown")
    last_crawl = index_status.get("lastCrawlTime")
    return coverage, last_crawl


def normalize_status(coverage_state: str) -> str:
    c = (coverage_state or "").lower()
    if "submitted and indexed" in c or c == "indexed":
        return "Indexed"
    if "crawled" in c and "not indexed" in c:
        return "Crawled, Not Indexed"
    if "discovered" in c and "not indexed" in c:
        return "Not Crawled"
    if "unknown to google" in c:
        return "Not Crawled"
    if "redirect" in c:
        return "Not Crawled"
    if "404" in c or "not found" in c:
        return "Not Crawled"
    if "blocked" in c:
        return "Not Crawled"
    if not coverage_state:
        return "Unknown"
    return "Not Crawled"


def main():
    ap = argparse.ArgumentParser(description="Generate crawl-status data.json for any site.")
    ap.add_argument("--credentials", required=True, help="Path to service account JSON key")
    ap.add_argument("--site-url", required=True, help="GSC property, e.g. sc-domain:example.com")
    ap.add_argument("--sitemap", help="Sitemap URL to fetch and parse")
    ap.add_argument("--url-list", help="Path to a plain text file of URLs (one per line)")
    ap.add_argument("--type-rules", help="Path to a JSON file of [pattern, label] classification rules")
    ap.add_argument("--output", default="data.json", help="Output JSON path")
    ap.add_argument("--sleep", type=float, default=1.0, help="Seconds to sleep between API calls")
    ap.add_argument("--limit", type=int, default=None, help="Optional cap on number of URLs to inspect (testing)")
    args = ap.parse_args()

    if not args.sitemap and not args.url_list:
        sys.exit("Provide either --sitemap or --url-list.")

    print("Loading URL inventory...")
    url_map = parse_sitemap(args.sitemap) if args.sitemap else load_url_list(args.url_list)
    sitemap_urls = set(url_map.keys()) if args.sitemap else set()
    if args.limit:
        url_map = dict(list(url_map.items())[: args.limit])
    print(f"  {len(url_map)} URLs loaded.")

    type_rules = load_type_rules(args.type_rules)

    print("Authenticating with Google Search Console API...")
    creds = service_account.Credentials.from_service_account_file(args.credentials, scopes=SCOPES)
    service = build("searchconsole", "v1", credentials=creds)

    rows = []
    total = len(url_map)
    for i, (url, lastmod) in enumerate(url_map.items(), start=1):
        print(f"  [{i}/{total}] {url}")
        try:
            coverage, last_crawl = inspect_url(service, args.site_url, url)
        except HttpError as e:
            status_code = getattr(e.resp, "status", None)
            if status_code == 429:
                print("  Daily quota hit - stopping early and saving progress.")
                break
            print(f"  Error inspecting {url}: {e}")
            coverage, last_crawl = "Unknown", None

        rows.append(
            {
                "url": url,
                "type": classify(url, type_rules),
                "lastUpdated": lastmod,
                "lastCrawled": last_crawl,
                "inSitemap": url in sitemap_urls if sitemap_urls else None,
                "status": normalize_status(coverage),
                "rawStatus": coverage,  # Google's actual coverageState, for debugging
            }
        )
        time.sleep(args.sleep)

    output = {
        "site": args.site_url,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "totalPages": len(rows),
        "rows": rows,
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    print(f"\nDone. Wrote {len(rows)} rows to {args.output}")


if __name__ == "__main__":
    main()
