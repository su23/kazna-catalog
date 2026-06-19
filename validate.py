#!/usr/bin/env python3
"""Validates catalog.json cross-file consistency — run before every push."""

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
errors = []


def load(path):
    with open(path) as f:
        return json.load(f)


catalog = load(ROOT / "catalog.json")
base_url = catalog["base_url"].rstrip("/")

for entry in catalog["packs"]:
    pack_url = entry["pack_url"]
    # Derive local path from absolute URL
    if pack_url.startswith(base_url + "/"):
        rel = pack_url[len(base_url) + 1:]
    else:
        errors.append(f"{entry['id']}: pack_url '{pack_url}' does not start with base_url '{base_url}/'")
        continue

    pack_path = ROOT / rel
    if not pack_path.exists():
        errors.append(f"{entry['id']}: pack file not found at {pack_path}")
        continue

    pack = load(pack_path)

    # id must match
    if pack.get("id") != entry["id"]:
        errors.append(f"{entry['id']}: id mismatch — catalog '{entry['id']}' vs pack '{pack.get('id')}'")

    # type must match
    if pack.get("type") != entry["type"]:
        errors.append(f"{entry['id']}: type mismatch — catalog '{entry['type']}' vs pack '{pack.get('type')}'")

    # version must match
    if pack.get("version") != entry["version"]:
        errors.append(
            f"{entry['id']}: version mismatch — catalog {entry['version']} vs pack {pack.get('version')}"
        )

    # name['en'] must match
    if pack.get("name", {}).get("en") != entry.get("name", {}).get("en"):
        errors.append(
            f"{entry['id']}: name.en mismatch — catalog '{entry.get('name', {}).get('en')}'"
            f" vs pack '{pack.get('name', {}).get('en')}'"
        )

    # entity_count must match actual array length
    pack_type = pack.get("type")
    array_key = pack_type if pack_type in ("payees", "banks", "categories") else None
    if array_key:
        actual = len(pack.get(array_key, []))
        declared = entry.get("entity_count")
        if actual != declared:
            errors.append(
                f"{entry['id']}: entity_count mismatch — catalog says {declared}, "
                f"pack has {actual} {array_key}"
            )

    # preview_logos must resolve to existing files
    for logo_url in entry.get("preview_logos", []):
        if logo_url.startswith(base_url + "/"):
            logo_path = ROOT / logo_url[len(base_url) + 1:]
            if not logo_path.exists():
                errors.append(f"{entry['id']}: preview logo not found: {logo_path}")
        else:
            errors.append(f"{entry['id']}: preview logo URL '{logo_url}' does not start with base_url")

    # pack logos must resolve to existing files
    for item in pack.get(array_key or "", []):
        logo_url = item.get("logo", "")
        if logo_url.startswith(base_url + "/"):
            logo_path = ROOT / logo_url[len(base_url) + 1:]
            if not logo_path.exists():
                errors.append(f"{entry['id']}/{item.get('key')}: logo not found: {logo_path}")
        else:
            errors.append(
                f"{entry['id']}/{item.get('key')}: logo URL '{logo_url}' does not start with base_url"
            )


if errors:
    print("Validation FAILED:")
    for e in errors:
        print(f"  ✗ {e}")
    sys.exit(1)
else:
    print(f"Validation passed — {len(catalog['packs'])} pack(s) OK")
