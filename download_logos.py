#!/usr/bin/env python3
"""Download logos for all payees using the Clearbit Logo API.

Skips files that already exist and are larger than the 67-byte stub.
Usage: python3 download_logos.py [--force]
"""

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
STUB_SIZE = 200  # anything under 200 bytes is a placeholder stub
FORCE = "--force" in sys.argv


def load(path):
    with open(path) as f:
        return json.load(f)


def domain_from_url(url):
    return urllib.parse.urlparse(url).netloc.removeprefix("www.")


def candidate_urls(domain):
    """Return logo source URLs to try in order."""
    # Clearbit works best with .com — try both the original domain and a .com fallback
    com_domain = domain.replace(".ru", ".com") if domain.endswith(".ru") else domain
    sources = [
        f"https://logo.clearbit.com/{domain}?size=256",
    ]
    if com_domain != domain:
        sources.append(f"https://logo.clearbit.com/{com_domain}?size=256")
    # DuckDuckGo favicon as last resort (smaller but always exists)
    sources.append(f"https://icons.duckduckgo.com/ip3/{domain}.ico")
    return sources


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "kazna-catalog/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                data = resp.read()
                if len(data) > STUB_SIZE:
                    dest.write_bytes(data)
                    return True
    except Exception as e:
        print(f"    error: {e}")
    return False


catalog = load(ROOT / "catalog.json")
base_url = catalog["base_url"].rstrip("/")

ok = skipped = failed = 0

for entry in catalog["packs"]:
    pack_url = entry["pack_url"]
    rel = pack_url[len(base_url) + 1:]
    pack = load(ROOT / rel)
    array_key = pack["type"] if pack["type"] in ("payees", "banks") else None
    if not array_key:
        continue

    print(f"\n{entry['id']}")
    for item in pack.get(array_key, []):
        logo_url = item.get("logo", "")
        if not logo_url.startswith(base_url + "/"):
            print(f"  {item['key']}: skipping — logo URL not relative to base_url")
            continue

        logo_path = ROOT / logo_url[len(base_url) + 1:]
        exists = logo_path.exists()
        is_stub = exists and logo_path.stat().st_size <= STUB_SIZE

        if exists and not is_stub and not FORCE:
            print(f"  {item['key']}: already have real logo ({logo_path.stat().st_size} bytes)")
            skipped += 1
            continue

        website = item.get("website", "")
        if not website:
            print(f"  {item['key']}: no website, skipping")
            failed += 1
            continue

        domain = domain_from_url(website)
        succeeded = False
        for url in candidate_urls(domain):
            print(f"  {item['key']}: trying {url} ...", end=" ", flush=True)
            if download(url, logo_path):
                size = logo_path.stat().st_size
                print(f"OK ({size} bytes)")
                succeeded = True
                ok += 1
                break
            else:
                print("failed")
            time.sleep(0.3)
        if not succeeded:
            failed += 1

print(f"\n{ok} downloaded, {skipped} skipped, {failed} failed")
