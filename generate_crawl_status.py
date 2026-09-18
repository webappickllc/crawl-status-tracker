#!/usr/bin/env python3
"""
Crawl Status Tracker - Google Search Console URL Inspection API
---------------------------------------------------------------

Works with Google Search Console URL-prefix properties such as:

    https://dailyshikkha.com/

IMPORTANT:
The service-account email from your credentials JSON must be added as a
user in Google Search Console for the property you are inspecting.

Example:

    python3 generate_crawl_status.py \
        --credentials service-account.json \
        --site-url "https://dailyshikkha.com/" \
        --sitemap "https://dailyshikkha.com/sitemap.xml" \
        --output data.json \
        --limit 5

After testing, remove --limit 5 to process the full sitemap.

For a Domain property, use:

    --site-url "sc-domain:dailyshikkha.com"

For a URL-prefix property, use:

    --site-url "https://dailyshikkha.com/"
"""

import argparse
import json
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlparse

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# Google Search Console API scope
SCOPES = [
    "https://www.googleapis.com/auth/webmasters.readonly"
]

SITEMAP_NS = {
    "sm": "http://www.sitemaps.org/schemas/sitemap/0.9"
}


# ------------------------------------------------------------
# Default URL pattern -> page type
# ------------------------------------------------------------

DEFAULT_TYPE_RULES = [
    (r"^/blog/", "Blog"),
    (r"^/case-studies?/", "Case Study"),
    (r"^/(features?|product)/", "Product"),
    (r"^/(services?)/", "Service"),
    (r"^/?$", "Home"),
]


# ------------------------------------------------------------
# Fetch sitemap
# ------------------------------------------------------------

def fetch_url(url: str) -> bytes:
    """Download a URL with a simple user agent."""

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "crawl-status-tracker/1.0"
        }
    )

    with urllib.request.urlopen(req, timeout=30) as response:
        return response.read()


# ------------------------------------------------------------
# Parse sitemap / sitemap index
# ------------------------------------------------------------

def parse_sitemap(sitemap_url: str, seen=None):
    """
    Recursively parse a sitemap or sitemap index.

    Returns:
        {
            "https://example.com/page/": "2026-09-01",
            ...
        }
    """

    if seen is None:
        seen = set()

    if sitemap_url in seen:
        return {}

    seen.add(sitemap_url)

    print(f"  Reading sitemap: {sitemap_url}")

    try:
        raw = fetch_url(sitemap_url)
    except Exception as e:
        raise RuntimeError(
            f"Could not download sitemap {sitemap_url}: {e}"
        )

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise RuntimeError(
            f"Invalid XML in sitemap {sitemap_url}: {e}"
        )

    urls = {}

    # --------------------------------------------------------
    # Sitemap index
    # --------------------------------------------------------

    child_sitemaps = root.findall(
        "sm:sitemap/sm:loc",
        SITEMAP_NS
    )

    if child_sitemaps:

        for loc in child_sitemaps:

            if loc.text:
                child_url = loc.text.strip()

                urls.update(
                    parse_sitemap(child_url, seen)
                )

        return urls

    # --------------------------------------------------------
    # Normal URL sitemap
    # --------------------------------------------------------

    for entry in root.findall(
        "sm:url",
        SITEMAP_NS
    ):

        loc_el = entry.find(
            "sm:loc",
            SITEMAP_NS
        )

        if loc_el is None or not loc_el.text:
            continue

        loc = loc_el.text.strip()

        lastmod_el = entry.find(
            "sm:lastmod",
            SITEMAP_NS
        )

        if (
            lastmod_el is not None
            and lastmod_el.text
        ):
            lastmod = lastmod_el.text.strip()
        else:
            lastmod = None

        urls[loc] = lastmod

    return urls


# ------------------------------------------------------------
# Load plain URL list
# ------------------------------------------------------------

def load_url_list(path: str):
    """Load URLs from a text file, one URL per line."""

    with open(
        path,
        "r",
        encoding="utf-8"
    ) as f:

        return {
            line.strip(): None
            for line in f
            if line.strip()
        }


# ------------------------------------------------------------
# Classify URL
# ------------------------------------------------------------

def classify(url: str, rules):

    path = urlparse(url).path or "/"

    for pattern, label in rules:

        if re.search(
            pattern,
            path,
            re.IGNORECASE
        ):
            return label

    return "Page"


# ------------------------------------------------------------
# Load custom type rules
# ------------------------------------------------------------

def load_type_rules(path: str):

    if not path:
        return DEFAULT_TYPE_RULES

    with open(
        path,
        "r",
        encoding="utf-8"
    ) as f:

        raw = json.load(f)

    return [
        (item[0], item[1])
        for item in raw
    ]


# ------------------------------------------------------------
# Inspect URL using Search Console API
# ------------------------------------------------------------

def inspect_url(
    service,
    site_url: str,
    inspection_url: str
):
    """
    Inspect a single URL.

    IMPORTANT:
    site_url must exactly match the Search Console property.

    URL-prefix property:
        https://dailyshikkha.com/

    Domain property:
        sc-domain:dailyshikkha.com
    """

    body = {
        "inspectionUrl": inspection_url,
        "siteUrl": site_url
    }

    response = (
        service
        .urlInspection()
        .index()
        .inspect(body=body)
        .execute()
    )

    result = response.get(
        "inspectionResult",
        {}
    )

    index_status = result.get(
        "indexStatusResult",
        {}
    )

    coverage = index_status.get(
        "coverageState",
        "Unknown"
    )

    last_crawl = index_status.get(
        "lastCrawlTime"
    )

    return coverage, last_crawl


# ------------------------------------------------------------
# Convert Google's status to simple status
# ------------------------------------------------------------

def normalize_status(
    coverage_state: str
) -> str:

    c = (
        coverage_state or ""
    ).lower()

    if (
        "submitted and indexed" in c
        or c == "indexed"
    ):
        return "Indexed"

    if (
        "crawled" in c
        and "not indexed" in c
    ):
        return "Crawled, Not Indexed"

    if (
        "discovered" in c
        and "not indexed" in c
    ):
        return "Not Crawled"

    if "unknown to google" in c:
        return "Not Crawled"

    if "redirect" in c:
        return "Not Crawled"

    if (
        "404" in c
        or "not found" in c
    ):
        return "Not Crawled"

    if "blocked" in c:
        return "Not Crawled"

    if not coverage_state:
        return "Unknown"

    return "Not Crawled"


# ------------------------------------------------------------
# Get service account email
# ------------------------------------------------------------

def get_service_account_email(
    credentials_path: str
):
    """
    Read the service account email from the JSON credentials file.

    This is printed so you know which email must be added to
    Google Search Console.
    """

    try:

        with open(
            credentials_path,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

        return data.get("client_email")

    except Exception:
        return None


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

def main():

    ap = argparse.ArgumentParser(
        description=(
            "Generate crawl-status data.json "
            "using Google Search Console URL Inspection API."
        )
    )

    ap.add_argument(
        "--credentials",
        required=True,
        help="Path to service-account JSON key"
    )

    ap.add_argument(
        "--site-url",
        required=True,
        help=(
            "GSC property. "
            "URL-prefix: https://example.com/ "
            "Domain: sc-domain:example.com"
        )
    )

    ap.add_argument(
        "--sitemap",
        help="Sitemap URL to fetch and parse"
    )

    ap.add_argument(
        "--url-list",
        help="Path to a plain text file of URLs"
    )

    ap.add_argument(
        "--type-rules",
        help="Path to JSON file of [pattern, label] rules"
    )

    ap.add_argument(
        "--output",
        default="data.json",
        help="Output JSON path"
    )

    ap.add_argument(
        "--sleep",
        type=float,
        default=1.0,
        help="Seconds between API calls"
    )

    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of URLs to inspect"
    )

    args = ap.parse_args()

    # --------------------------------------------------------
    # Validate URL source
    # --------------------------------------------------------

    if not args.sitemap and not args.url_list:

        sys.exit(
            "ERROR: Provide either --sitemap or --url-list."
        )

    if args.sitemap and args.url_list:

        sys.exit(
            "ERROR: Use either --sitemap OR --url-list, not both."
        )

    # --------------------------------------------------------
    # Display property information
    # --------------------------------------------------------

    print()
    print("=" * 70)
    print("GOOGLE SEARCH CONSOLE CRAWL STATUS TRACKER")
    print("=" * 70)

    print()
    print("Search Console property:")
    print(f"  {args.site_url}")

    # --------------------------------------------------------
    # Validate property format
    # --------------------------------------------------------

    if args.site_url.startswith("sc-domain:"):

        print()
        print("Property type: Domain property")

    elif args.site_url.startswith("https://"):

        print()
        print("Property type: URL-prefix property")

    elif args.site_url.startswith("http://"):

        print()
        print("Property type: URL-prefix property")

    else:

        print()
        print(
            "WARNING: The --site-url format does not look like "
            "a normal Search Console property."
        )

    # --------------------------------------------------------
    # Show service account
    # --------------------------------------------------------

    service_account_email = get_service_account_email(
        args.credentials
    )

    print()
    print("Service account:")

    if service_account_email:
        print(f"  {service_account_email}")

        print()
        print(
            "IMPORTANT: This email must have access to the "
            "Search Console property above."
        )

    else:
        print(
            "  Could not read client_email from credentials JSON."
        )

    # --------------------------------------------------------
    # Load URL inventory
    # --------------------------------------------------------

    print()
    print("Loading URL inventory...")

    try:

        if args.sitemap:

            url_map = parse_sitemap(
                args.sitemap
            )

            sitemap_urls = set(
                url_map.keys()
            )

        else:

            url_map = load_url_list(
                args.url_list
            )

            sitemap_urls = set()

    except Exception as e:

        sys.exit(
            f"\nERROR loading URL inventory:\n{e}"
        )

    # --------------------------------------------------------
    # Apply limit
    # --------------------------------------------------------

    if args.limit:

        url_map = dict(
            list(url_map.items())[
                :args.limit
            ]
        )

    print()
    print(
        f"  {len(url_map)} URLs loaded."
    )

    if not url_map:

        sys.exit(
            "ERROR: No URLs were found."
        )

    # --------------------------------------------------------
    # Load type rules
    # --------------------------------------------------------

    try:

        type_rules = load_type_rules(
            args.type_rules
        )

    except Exception as e:

        sys.exit(
            f"\nERROR loading type rules:\n{e}"
        )

    # --------------------------------------------------------
    # Authenticate
    # --------------------------------------------------------

    print()
    print("Authenticating with Google Search Console API...")

    try:

        creds = (
            service_account
            .Credentials
            .from_service_account_file(
                args.credentials,
                scopes=SCOPES
            )
        )

        service = build(
            "searchconsole",
            "v1",
            credentials=creds,
            cache_discovery=False
        )

    except Exception as e:

        sys.exit(
            "\nERROR: Could not authenticate with Google.\n"
            f"{e}"
        )

    print("  Authentication successful.")

    # --------------------------------------------------------
    # Inspect URLs
    # --------------------------------------------------------

    rows = []

    total = len(url_map)

    print()
    print("Starting URL inspection...")
    print()

    for i, (url, lastmod) in enumerate(
        url_map.items(),
        start=1
    ):

        print(
            f"  [{i}/{total}] {url}"
        )

        try:

            coverage, last_crawl = inspect_url(
                service,
                args.site_url,
                url
            )

        except HttpError as e:

            status_code = getattr(
                e.resp,
                "status",
                None
            )

            # ------------------------------------------------
            # 403 = permission/property problem
            # ------------------------------------------------

            if status_code == 403:

                print()
                print("=" * 70)
                print("GOOGLE SEARCH CONSOLE PERMISSION ERROR")
                print("=" * 70)

                print()
                print(
                    "Google returned HTTP 403."
                )

                print()
                print(
                    "The authenticated service account does not "
                    "have access to this Search Console property:"
                )

                print()
                print(
                    f"  {args.site_url}"
                )

                if service_account_email:

                    print()
                    print(
                        "Add this service-account email to "
                        "Google Search Console:"
                    )

                    print()
                    print(
                        f"  {service_account_email}"
                    )

                print()
                print(
                    "Search Console → Settings → Users and "
                    "permissions → Add user"
                )

                print()
                print(
                    "IMPORTANT:"
                )

                print(
                    "If your property is exactly "
                    "'https://dailyshikkha.com/', use:"
                )

                print(
                    '  --site-url "https://dailyshikkha.com/"'
                )

                print()
                print(
                    "Do NOT use sc-domain:dailyshikkha.com "
                    "unless you have a Domain property."
                )

                print()
                print(
                    "Stopping now so the script does not repeat "
                    "the same 403 error for every URL."
                )

                print("=" * 70)

                break

            # ------------------------------------------------
            # 429 = quota
            # ------------------------------------------------

            if status_code == 429:

                print()
                print(
                    "Daily API quota/rate limit reached."
                )

                print(
                    "Saving the URLs processed so far."
                )

                break

            # ------------------------------------------------
            # Other HTTP errors
            # ------------------------------------------------

            print()
            print(
                f"  Google API error "
                f"(HTTP {status_code}): {e}"
            )

            coverage = "Unknown"
            last_crawl = None

        except Exception as e:

            print()
            print(
                f"  Unexpected error: {e}"
            )

            coverage = "Unknown"
            last_crawl = None

        # ----------------------------------------------------
        # Save result
        # ----------------------------------------------------

        rows.append(
            {
                "url": url,

                "type": classify(
                    url,
                    type_rules
                ),

                "lastUpdated": lastmod,

                "lastCrawled": last_crawl,

                "inSitemap": (
                    url in sitemap_urls
                    if sitemap_urls
                    else None
                ),

                "status": normalize_status(
                    coverage
                ),

                "rawStatus": coverage
            }
        )

        # ----------------------------------------------------
        # Sleep between requests
        # ----------------------------------------------------

        time.sleep(
            max(0, args.sleep)
        )

    # --------------------------------------------------------
    # Create output
    # --------------------------------------------------------

    output = {

        "site": args.site_url,

        "generatedAt": (
            datetime.now(timezone.utc)
            .strftime("%Y-%m-%d %H:%M UTC")
        ),

        "totalPages": len(rows),

        "rows": rows
    }

    # --------------------------------------------------------
    # Write data.json
    # --------------------------------------------------------

    try:

        with open(
            args.output,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                output,
                f,
                indent=2,
                ensure_ascii=False
            )

    except Exception as e:

        sys.exit(
            f"\nERROR writing output file:\n{e}"
        )

    # --------------------------------------------------------
    # Finished
    # --------------------------------------------------------

    print()
    print("=" * 70)
    print("DONE")
    print("=" * 70)

    print()
    print(
        f"Wrote {len(rows)} rows to:"
    )

    print(
        f"  {args.output}"
    )

    if len(rows) < total:

        print()
        print(
            f"Note: {total - len(rows)} URL(s) were not processed."
        )

    print()


if __name__ == "__main__":
    main()
