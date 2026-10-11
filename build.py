#!/usr/bin/env python3
"""Builds the app's catalog index (format 2) from packs/ and meta/.

    python3 build.py                  write index/v2/banks.json, index/v2/brands.json and the
                                      hashed logo copies in index/v2/logos/, then print sizes
    python3 build.py --check          write nothing; exit 1 if the index is stale or anything
                                      below is wrong
    python3 build.py --check --base origin/main
                                      look for vanished keys against that commit's index
                                      instead of HEAD's (before a push, or in CI)

Both modes fail on: a pack missing from meta/packs.json, an unknown category, an override
for a key no pack has, shared payee copies that differ, a missing logo, a budget exceeded,
a key published in the committed index that vanished without a meta/renamed.json entry, a
name published there that is gone from its entry (a brand keeps it in n, a, o or r, a bank in
n or a), a translit brand whose names aren't one Latin name in every language, a logo copy
committed at the base that is gone, or a MIN_APP_BUILD that isn't on the app's scale. They warn
when a name override changes an en name, and when a brand that had countries at the base is
international now.

Packs stay the authoring format: pack.json and catalog.json v1 are read, never written. The
index is generated, deterministic and committed, because GitHub Pages serves the repository as
it is. Python 3 standard library only.
"""

import argparse
import datetime
import gzip
import hashlib
import hmac
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent
PACKS_DIR = ROOT / "packs"
META_DIR = ROOT / "meta"
INDEX_DIR = ROOT / "index" / "v2"
LOGO_DIR = INDEX_DIR / "logos"
INDEX_REL = "index/v2"

FORMAT = 2
# The oldest app that may read the index, counted on the version both platforms share:
# major * 1,000,000 + minor * 1,000 + patch, so 1.13.0 is 1013000 (catalogAppBuild in the app).
# 0 lets every build in. Not a store build number: 1.12.17 is 65 on Android and 49 on iOS, and
# every build that reads index v2 counts as 1012018 or more, so a 66 here would turn no one away.
MIN_APP_BUILD = 0
MIN_APP_VERSION_SCALE = 1_000_000
BASE_URL = "https://catalog.kazna.app/"
LOGO_PATH = "index/v2/logos/{h}.png"

# h = the first 16 hex digits of HMAC-SHA256(key, PNG bytes); the app checks logos with its own
# hmacSha256. rev = the same over the canonical JSON of a file without rev and updated.
LOGO_HASH_KEY = b"kazna-logo-v1"
INDEX_HASH_KEY = b"kazna-index-v1"
HASH_DIGITS = 16

# Every category is named in these languages.
CATEGORY_LANGUAGES = ("en", "ru", "de", "es", "fr")

# Raw and gzipped size limits; a warning from WARN_AT of either.
BUDGETS = {
    "banks.json": (120 * 1024, 30 * 1024),
    "brands.json": (320 * 1024, 70 * 1024),
}
WARN_AT = 0.8
GZIP_LEVEL = 6

# The app drops a logo bigger than this (MAX_LOGO_BYTES in the app's LogoBytes.kt).
MAX_LOGO_BYTES = 256 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# The files whose last commit dates the index (`updated`). sources.json and catalog.json don't
# change what the index says, so they don't move the date.
INPUT_PATHSPECS = ("packs/*/pack.json", "packs/*/logos/*", "meta")

KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
COUNTRY_PATTERN = re.compile(r"^[A-Z]{2}$")
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
COLOR_PATTERN = re.compile(r"^#[0-9A-F]{6}$")
ICON_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
LANGUAGE_PATTERN = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")

errors = []
warnings = []


def error(message):
    errors.append(message)


def warn(message):
    warnings.append(message)


# ---------------------------------------------------------------------------------------------
# JSON

def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"the key {key!r} appears twice in one object")
        result[key] = value
    return result


def parse_json(text):
    return json.loads(text, object_pairs_hook=_reject_duplicate_keys)


def load_json(path):
    """The parsed file, or None after recording why it could not be read."""
    try:
        return parse_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        error(f"{rel(path)}: file not found")
    except (ValueError, UnicodeDecodeError) as e:
        error(f"{rel(path)}: not valid JSON: {e}")
    return None


def rel(path):
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def canonical(value):
    """Sorted keys, no spaces, non-ASCII as is: the form `rev` is computed over."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def render(document):
    """The file text: canonical JSON with one line per item of each list of objects.

    Still valid, deterministic JSON, but a diff shows which bank or brand changed.
    """
    items = sorted(document.items())
    lines = ["{"]
    for i, (name, value) in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        label = json.dumps(name, ensure_ascii=False)
        if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            lines.append(f"{label}:[")
            last = len(value) - 1
            lines += [canonical(item) + ("," if j < last else "") for j, item in enumerate(value)]
            lines.append("]" + comma)
        else:
            lines.append(f"{label}:{canonical(value)}{comma}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def keyed_hash(key, data):
    return hmac.new(key, data, hashlib.sha256).hexdigest()[:HASH_DIGITS]


def index_rev(document):
    body = {name: value for name, value in document.items() if name not in ("rev", "updated")}
    return keyed_hash(INDEX_HASH_KEY, canonical(body).encode("utf-8"))


# ---------------------------------------------------------------------------------------------
# Small shape checks for meta/ (the files are ours, so these only catch typos)

def is_text(value):
    return isinstance(value, str) and value.strip() != ""


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def check_fields(where, value, allowed, required=()):
    if not isinstance(value, dict):
        error(f"{where}: expected an object")
        return False
    for name in sorted(set(value) - set(allowed)):
        error(f"{where}: unknown field {name!r}")
    for name in required:
        if name not in value:
            error(f"{where}: missing {name!r}")
    return all(name in value for name in required)


def check_countries(where, value):
    """True when `value` is a list of distinct ISO country codes ([] = international)."""
    if not isinstance(value, list) or not all(isinstance(c, str) and COUNTRY_PATTERN.match(c) for c in value):
        error(f"{where}: countries must be a list of ISO codes such as \"RU\", or [] for international")
        return False
    if len(set(value)) != len(value):
        error(f"{where}: a country is listed twice")
        return False
    return True


def check_names(where, value, languages=()):
    if not isinstance(value, dict) or not value:
        error(f"{where}: expected names by language")
        return False
    ok = True
    for language, text in value.items():
        if not LANGUAGE_PATTERN.match(language) or not is_text(text):
            error(f"{where}: bad name {language!r}: {text!r}")
            ok = False
    for language in languages:
        if language not in value:
            error(f"{where}: no {language!r} name")
            ok = False
    return ok


def check_aliases(where, value, field="aliases", noun="an alias"):
    if not isinstance(value, list) or not all(is_text(a) for a in value):
        error(f"{where}: {field} must be a list of non-empty strings")
        return False
    if len(set(value)) != len(value):
        error(f"{where}: {noun} is listed twice")
        return False
    return True


def check_trimmed(where, texts):
    """True when no text starts or ends with a space; names in meta/ are compared exactly."""
    padded = [text for text in texts if text != text.strip()]
    for text in padded:
        error(f"{where}: «{text}» starts or ends with a space")
    return not padded


def _is_latin_letter(ch):
    # Modifier letters (ʼ) carry no script of their own.
    return unicodedata.category(ch) == "Lm" or unicodedata.name(ch, "").startswith("LATIN ")


def is_latin_name(text):
    """A name written in Latin letters: at least one letter, and every letter Latin ("S7 Airlines", "H&M")."""
    letters = [ch for ch in text if ch.isalpha()]
    return bool(letters) and all(_is_latin_letter(ch) for ch in letters)


def has_other_script(text):
    """Whether a name has a letter outside the Latin script («Эппл», «Узум Market»)."""
    return any(ch.isalpha() and not _is_latin_letter(ch) for ch in text)


# ---------------------------------------------------------------------------------------------
# Packs and meta

class Pack:
    def __init__(self, pack_id, data):
        self.id = pack_id
        self.type = data["type"]
        self.entities = data[self.type]
        self.rank = None
        self.countries = []
        self.category = None


def load_packs():
    packs = {}
    for folder in sorted(PACKS_DIR.iterdir()):
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        path = folder / "pack.json"
        data = load_json(path)
        if data is None:
            continue
        if not isinstance(data, dict) or data.get("type") not in ("payees", "banks") \
                or not isinstance(data.get(data.get("type")), list):
            error(f"{rel(path)}: needs a type of payees or banks and a list of them")
            continue
        if data.get("id") != folder.name:
            error(f"{rel(path)}: id is {data.get('id')!r} but the folder is {folder.name!r}")
            continue
        pack = Pack(folder.name, data)
        seen = set()
        for i, entity in enumerate(pack.entities):
            key = entity.get("key") if isinstance(entity, dict) else None
            if not isinstance(key, str) or not KEY_PATTERN.match(key):
                error(f"{rel(path)}: entry {i} has no usable key")
                continue
            if key in seen:
                error(f"{rel(path)}: key {key!r} appears twice")
            seen.add(key)
            if not check_names(f"{rel(path)}: {key}", entity.get("name"), ("en",)):
                continue
            # Optional entity fields of the v2 contract; meta/entities.json overrides them.
            if "aliases" in entity:
                check_aliases(f"{rel(path)}: {key}", entity["aliases"])
            if "countries" in entity and entity["countries"] is not None:
                check_countries(f"{rel(path)}: {key}", entity["countries"])
            if "match" in entity and entity["match"] != "pick":
                error(f"{rel(path)}: {key}: match must be \"pick\"")
        packs[pack.id] = pack
    return packs


def load_categories():
    data = load_json(META_DIR / "categories.json")
    categories = {}
    if data is None:
        return categories
    if not isinstance(data, list):
        error("meta/categories.json: expected a list of categories")
        return categories
    ranks = {}
    for i, item in enumerate(data):
        where = f"meta/categories.json[{i}]"
        if not check_fields(where, item, ("id", "rank", "icon", "color", "n"), ("id", "rank", "icon", "color", "n")):
            continue
        cid = item["id"]
        where = f"meta/categories.json: {cid}"
        ok = True
        if not isinstance(cid, str) or not SLUG_PATTERN.match(cid):
            error(f"{where}: id must be kebab-case")
            ok = False
        elif cid in categories:
            error(f"{where}: listed twice")
            ok = False
        if not is_int(item["rank"]) or item["rank"] < 1:
            error(f"{where}: rank must be a whole number from 1")
            ok = False
        elif item["rank"] in ranks:
            error(f"{where}: rank {item['rank']} is also {ranks[item['rank']]}'s")
            ok = False
        if not isinstance(item["icon"], str) or not ICON_PATTERN.match(item["icon"]):
            error(f"{where}: icon must be a short id such as \"cart\"")
            ok = False
        if not isinstance(item["color"], str) or not COLOR_PATTERN.match(item["color"]):
            error(f"{where}: color must be #RRGGBB in capitals")
            ok = False
        ok = check_names(where, item["n"], CATEGORY_LANGUAGES) and ok
        if ok:
            ranks[item["rank"]] = cid
            categories[cid] = item
    return categories


def load_pack_meta(packs, categories):
    data = load_json(META_DIR / "packs.json")
    if data is None:
        return False
    if not isinstance(data, dict):
        error("meta/packs.json: expected an object of packs by id")
        return False
    complete = True
    ranks = {}
    for pack_id, item in data.items():
        where = f"meta/packs.json: {pack_id}"
        pack = packs.get(pack_id)
        if pack is None:
            error(f"{where}: there is no packs/{pack_id}/pack.json")
            continue
        if not check_fields(where, item, ("rank", "countries", "category"), ("rank", "countries")):
            complete = False
            continue
        if not is_int(item["rank"]) or item["rank"] < 1:
            error(f"{where}: rank must be a whole number from 1")
            complete = False
        elif item["rank"] in ranks:
            error(f"{where}: rank {item['rank']} is also {ranks[item['rank']]}'s")
            complete = False
        else:
            ranks[item["rank"]] = pack_id
            pack.rank = item["rank"]
        if check_countries(where, item["countries"]):
            pack.countries = item["countries"]
        category = item.get("category")
        if pack.type == "payees":
            if category is None:
                error(f"{where}: a payee pack needs a category, its entries' default")
                complete = False
            elif category not in categories:
                error(f"{where}: unknown category {category!r}")
                complete = False
            else:
                pack.category = category
        elif category is not None:
            error(f"{where}: a bank pack has no category")
    for pack_id in sorted(set(packs) - set(data)):
        error(f"meta/packs.json: pack {pack_id!r} is missing; every pack needs a rank and countries")
        complete = False
    return complete


# name: names by language, merged over the pack's in the index only (pack.json, and so every build
# that reads packs, keeps its own). former: the names a brand went by before a rebrand (`o`).
# translit: true marks a name override as a Latinisation of names written in another script.
ENTITY_FIELDS = ("category", "countries", "aliases", "pick", "full_bleed", "name", "former", "translit")


def load_entity_meta(banks_by_key, brand_keys, categories):
    data = load_json(META_DIR / "entities.json")
    overrides = {}
    if data is None:
        return overrides
    if not isinstance(data, dict):
        error("meta/entities.json: expected an object of overrides by key")
        return overrides
    for key, item in data.items():
        where = f"meta/entities.json: {key}"
        is_bank, is_brand = key in banks_by_key, key in brand_keys
        if not is_bank and not is_brand:
            error(f"{where}: no pack has this key")
            continue
        if not check_fields(where, item, ENTITY_FIELDS):
            continue
        ok = True
        if "category" in item:
            if not is_brand:
                error(f"{where}: category is for brands, and this key is a bank")
                ok = False
            elif item["category"] not in categories:
                error(f"{where}: unknown category {item['category']!r}")
                ok = False
        if "countries" in item:
            ok = check_countries(where, item["countries"]) and ok
        if "aliases" in item:
            ok = check_aliases(where, item["aliases"]) and ok
        if "name" in item:
            if check_names(f"{where}: name", item["name"]):
                ok = check_trimmed(f"{where}: name", item["name"].values()) and ok
            else:
                ok = False
        if "former" in item:
            if not is_brand:
                error(f"{where}: former is for brands, and this key is a bank; "
                      "a bank's earlier names go in its aliases (banks.json has no o)")
                ok = False
            elif check_aliases(where, item["former"], "former", "a former name"):
                ok = check_trimmed(f"{where}: former", item["former"]) and ok
            else:
                ok = False
        for flag in ("pick", "full_bleed", "translit"):
            if flag in item and item[flag] is not True:
                error(f"{where}: {flag} is either true or left out")
                ok = False
        if "pick" in item and not is_brand:
            error(f"{where}: pick is for brands, and this key is a bank")
            ok = False
        if "translit" in item:
            if not is_brand:
                error(f"{where}: translit is for brands, and this key is a bank")
                ok = False
            if "name" not in item:
                error(f"{where}: translit: true marks a name override as a Latinisation, and there is no name")
                ok = False
        if is_bank and is_brand and set(item) & {"countries", "aliases", "full_bleed", "name"}:
            warn(f"{where}: the key is both a bank and a brand, so this applies to both")
        if ok:
            overrides[key] = item
    return overrides


def load_regions():
    data = load_json(META_DIR / "regions.json")
    if data is None:
        return None
    where = "meta/regions.json"
    if not check_fields(where, data, ("reserve", "regions", "country_names"), ("reserve", "regions", "country_names")):
        return None
    ok = True
    reserve = data["reserve"]
    if not isinstance(reserve, list) or not all(isinstance(c, str) and CURRENCY_PATTERN.match(c) for c in reserve):
        error(f"{where}: reserve must be a list of currency codes")
        ok = False
    regions = data["regions"]
    if not isinstance(regions, dict) or not all(
            CURRENCY_PATTERN.match(currency) and isinstance(country, str) and COUNTRY_PATTERN.match(country)
            for currency, country in regions.items()):
        error(f"{where}: regions maps a currency code to a country code")
        ok = False
    names = data["country_names"]
    if not isinstance(names, dict):
        error(f"{where}: country_names maps a country code to names by language")
        ok = False
    else:
        for country, by_language in names.items():
            if not COUNTRY_PATTERN.match(country):
                error(f"{where}: {country!r} is not a country code")
                ok = False
            ok = check_names(f"{where}: {country}", by_language) and ok
    return data if ok else None


def load_renamed():
    data = load_json(META_DIR / "renamed.json")
    if data is None:
        return {}
    if not isinstance(data, dict) or not all(
            isinstance(new, str) and KEY_PATTERN.match(old) and KEY_PATTERN.match(new) for old, new in data.items()):
        error("meta/renamed.json: expected an object of old key -> new key")
        return {}
    return data


# ---------------------------------------------------------------------------------------------
# Logos

class Logos:
    """Every logo the index references, by hash."""

    def __init__(self):
        self.files = {}  # h -> bytes
        self.sources = {}  # h -> first source path

    def add(self, where, url):
        """The hash of the logo at `url`, or None after recording why it can't be used."""
        if not isinstance(url, str) or not url.startswith(BASE_URL):
            error(f"{where}: logo {url!r} is not under {BASE_URL}")
            return None
        path = (ROOT / url[len(BASE_URL):]).resolve()
        if PACKS_DIR.resolve() not in path.parents:
            error(f"{where}: logo {url} is not a pack file")
            return None
        try:
            data = path.read_bytes()
        except OSError:
            error(f"{where}: logo file {rel(path)} does not exist")
            return None
        if not data.startswith(PNG_SIGNATURE):
            error(f"{where}: {rel(path)} is not a PNG")
            return None
        if len(data) > MAX_LOGO_BYTES:
            error(f"{where}: {rel(path)} is {len(data) // 1024} KB; the app drops logos over {MAX_LOGO_BYTES // 1024} KB")
            return None
        h = keyed_hash(LOGO_HASH_KEY, data)
        known = self.files.get(h)
        if known is not None and known != data:
            error(f"{where}: {rel(path)} and {self.sources[h]} differ but share the hash {h}")
            return None
        self.files[h] = data
        self.sources.setdefault(h, rel(path))
        return h


# ---------------------------------------------------------------------------------------------
# Building

def website(url):
    """"https://www.tbank.ru/insurance/" -> "www.tbank.ru/insurance": no scheme, no end slash."""
    return re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", url).rstrip("/")


def solid_color(background):
    if isinstance(background, str) and background.startswith("solid:"):
        return background[len("solid:"):].upper()
    return None


def merge_names(key, noun, names, override):
    """The pack's `names` with a meta/entities.json `name` override merged over them, by language,
    and the pack names the override replaced that are none of the merged names: the brand's `r`.

    The index is all that changes: pack.json, and every build that reads packs, keeps its names.
    """
    if not override:
        return names, []
    merged = dict(names)
    merged.update(override)
    replaced = []
    for language, text in names.items():
        if merged[language] != text and text not in merged.values() and text not in replaced:
            replaced.append(text)
    where = f"meta/entities.json: {key}"
    if merged == names:
        warn(f"{where}: the name override changes none of the {noun}'s names; drop it")
    elif merged.get("en") != names.get("en"):
        warn(f"{where}: the name override changes the {noun}'s en name, «{names.get('en')}» -> «{merged.get('en')}». "
             "en is the name every language without its own shows, and builds that read packs keep the old one; "
             "make sure that is meant")
    return merged, replaced


def build_banks(bank_packs, overrides, logos):
    banks = []
    for pack in bank_packs:
        for entity in pack.entities:
            key = entity["key"]
            override = overrides.get(key, {})
            # Banks have no `r`: a name the override replaces stays searchable only as an alias,
            # which the vanished-name check asks for once the old name has been published.
            names, _ = merge_names(key, "bank", entity["name"], override.get("name"))
            entry = {"k": key, "p": pack.id, "n": names}
            group = entity.get("brand")
            if group is not None and group != key:
                entry["b"] = group
            aliases = override.get("aliases", entity.get("aliases"))
            if aliases:
                entry["a"] = aliases
            if "countries" in override:
                countries = override["countries"]
            elif "countries" in entity:
                countries = entity["countries"] or []
            else:
                countries = pack.countries
            if countries:
                entry["c"] = countries
            if "logo" in entity:
                h = logos.add(f"packs/{pack.id}: {key}", entity["logo"])
                if h:
                    entry["h"] = h
            if override.get("full_bleed"):
                entry["f"] = 1
            background = solid_color(entity.get("card_background"))
            if background:
                entry["bg"] = background
            if entity.get("accent_color"):
                entry["ac"] = entity["accent_color"].upper()
            if entity.get("closed"):
                entry["x"] = 1
            banks.append(entry)
    return banks


def check_bank_groups(banks):
    """A group is its main entry (key = group) plus the entries whose b names it."""
    by_key = {bank["k"]: bank for bank in banks}
    members = {}
    for bank in banks:
        if "b" in bank:
            members.setdefault(bank["b"], []).append(bank["k"])
    for group, keys in sorted(members.items()):
        main = by_key.get(group)
        if main is not None and "b" in main:
            error(f"bank group {group!r}: {group} is the group's main entry but itself belongs to {main['b']!r}")
        if main is None and len(keys) < 2:
            warn(f"bank group {group!r}: only {keys[0]} is in it, and no bank has the key {group!r}")


def build_brands(payee_packs, overrides, categories, logos, international_why=None):
    """The brand entries in index order. `international_why`, when given, gets each international
    brand's key mapped to what made it international, for the lost-countries warning."""
    copies = {}
    for pack in payee_packs:
        for position, entity in enumerate(pack.entities):
            copies.setdefault(entity["key"], []).append((pack, position, entity))

    brands = []
    for key, found in copies.items():
        first_pack, position, first = found[0]
        first_logo = logos.add(f"packs/{first_pack.id}: {key}", first["logo"]) if "logo" in first else None
        for pack, _, entity in found[1:]:
            fields = sorted(
                field for field in set(first) | set(entity)
                if field != "logo" and canonical(first.get(field)) != canonical(entity.get(field))
            )
            other_logo = logos.add(f"packs/{pack.id}: {key}", entity["logo"]) if "logo" in entity else None
            if other_logo != first_logo:
                fields.append("logo file")
            if fields:
                error(f"payee {key!r} differs between {first_pack.id} and {pack.id}: {', '.join(fields)}; "
                      "a shared key must be identical in every pack")

        override = overrides.get(key, {})
        category = override.get("category") or first.get("category") or first_pack.category
        if category not in categories:
            error(f"packs/{first_pack.id}: {key}: unknown category {category!r}")
            continue
        defaults = sorted({pack.category for pack, _, _ in found})
        if len(defaults) > 1 and "category" not in override and "category" not in first:
            warn(f"payee {key!r} is in packs with different categories ({', '.join(defaults)}); "
                 f"it takes {category!r} from {first_pack.id}. Set it in meta/entities.json to be sure.")

        if "countries" in override:
            countries = override["countries"]
            why = "because meta/entities.json gives it countries []"
        elif "countries" in first:
            countries = first["countries"] or []
            why = f"because packs/{first_pack.id} gives it countries {canonical(first['countries'])}"
        else:
            # The union over the packs; an international pack makes the brand international.
            countries = []
            why = None
            for pack, _, _ in found:
                if not pack.countries:
                    countries = []
                    why = (f"because {pack.id}, an international pack, lists it: one international pack makes "
                           "the brand international (the union rule)")
                    break
                countries += [c for c in pack.countries if c not in countries]
        if not countries and international_why is not None:
            international_why[key] = why

        names, replaced = merge_names(key, "brand", first["name"], override.get("name"))
        entry = {"k": key, "p": first_pack.id, "n": names, "g": category}
        if len(found) > 1:
            entry["ps"] = [pack.id for pack, _, _ in found]
        aliases = override.get("aliases", first.get("aliases"))
        if aliases:
            entry["a"] = aliases
        former = override.get("former")
        if former:
            current = [text for text in former if text in names.values()]
            if current:
                error(f"meta/entities.json: {key}: «{current[0]}» is one of its names now, so it can't be a former one")
            entry["o"] = former
        if replaced:
            entry["r"] = replaced
        if override.get("translit"):
            shown = set(names.values())
            if len(shown) != 1 or not is_latin_name(next(iter(shown))):
                named = ", ".join(f"{language} «{text}»" for language, text in sorted(names.items()))
                # When the override itself is one Latin name, point at the pack's names that differ.
                targets = set(override["name"].values())
                target = next(iter(targets)) if len(targets) == 1 else None
                from_pack = [language for language in sorted(names)
                             if language not in override["name"] and names[language] != target] \
                    if target is not None and is_latin_name(target) else []
                hint = (f" ({', '.join(from_pack)} from the pack: override {'it' if len(from_pack) == 1 else 'them'}"
                        " too)" if from_pack else "")
                error(f"meta/entities.json: {key}: translit: true needs one Latin name, the same in every "
                      f"language, and its names are {named}{hint}")
            # The transliterations: replaced names written in another script. A Latin name it
            # replaced (an en change, a spelling fix) stays in `r` only, so a rename never reads it.
            transliterations = [text for text in replaced if has_other_script(text)]
            if transliterations:
                entry["t"] = transliterations
        if countries:
            entry["c"] = countries
        if first.get("mcc"):
            entry["m"] = first["mcc"]
        if first.get("website"):
            entry["w"] = website(first["website"])
        if first_logo:
            entry["h"] = first_logo
        if override.get("full_bleed"):
            entry["f"] = 1
        if override.get("pick") or first.get("match") == "pick":
            entry["pick"] = 1
        brands.append(((categories[category]["rank"], first_pack.rank, position), entry))

    brands.sort(key=lambda item: item[0])
    return [entry for _, entry in brands]


def split_renamed(renamed, bank_keys, brand_keys):
    for_banks, for_brands = {}, {}
    for old, new in sorted(renamed.items()):
        where = f"meta/renamed.json: {old}"
        if old == new:
            error(f"{where}: renamed to itself")
        elif old in bank_keys or old in brand_keys:
            error(f"{where}: {old!r} is still a key in the packs, so it can't be renamed")
        elif new not in bank_keys and new not in brand_keys:
            error(f"{where}: {new!r} is in no pack; point it at the key that replaced it")
        else:
            if new in bank_keys:
                for_banks[old] = new
            if new in brand_keys:
                for_brands[old] = new
    return for_banks, for_brands


# ---------------------------------------------------------------------------------------------
# git

def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True)


def last_input_date():
    """The date of the last commit that touched the packs or meta/, or today with changes pending."""
    today = datetime.date.today().isoformat()
    try:
        pending = git("status", "--porcelain", "--untracked-files=all", "--", *INPUT_PATHSPECS)
        if pending.returncode == 0 and pending.stdout.strip():
            return today, "packs/ or meta/ have uncommitted changes, so updated is today"
        log = git("log", "-1", "--format=%cs", "--", *INPUT_PATHSPECS)
        date = log.stdout.decode().strip() if log.returncode == 0 else ""
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            return date, None
    except OSError:
        pass
    warn("git could not date the last change to packs/ or meta/, so updated is today")
    return today, None


def committed_index(base, name):
    """The index file `name` as committed at `base`, None when there is none, or False when git failed."""
    try:
        found = git("rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    except OSError:
        return False
    if found.returncode != 0:
        return False
    shown = git("show", f"{base}:{INDEX_REL}/{name}")
    if shown.returncode != 0:
        return None
    try:
        return parse_json(shown.stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        warn(f"{INDEX_REL}/{name} at {base} is not valid JSON, so vanished keys were not checked against it")
        return None


# The names an entry published that must not just disappear from it, and the fields that keep
# them. A brand keeps its former (o) and replaced (r) names for good, so a renamed brand is still
# found by its old name, and a pack's payee still titled the old way still reads as the pack's.
# Banks have no o or r: a bank's old name stays one of its aliases, which also keeps a bank of
# the user's titled that way a shadow of the catalog's bank.
PUBLISHED_NAMES = {"banks": ("n",), "brands": ("n", "o", "r")}
KEPT_NAMES = {"banks": ("n", "a"), "brands": ("n", "a", "o", "r")}


def entry_names(entry, fields):
    """The texts of an index entry's name fields (`n` by language, the others lists), in order, each once."""
    found = []
    for field in fields:
        value = entry.get(field)
        texts = value.values() if isinstance(value, dict) else value if isinstance(value, list) else []
        found += [text for text in texts if isinstance(text, str) and text not in found]
    return found


def listed(texts):
    return ", ".join(f"«{text}»" for text in texts)


def check_published(base, documents, international_why):
    """Compared with the index committed at `base`: a key never just disappears, nor does a renamed
    entry or a name; and a brand that had countries there and is international now is pointed out.

    A key renamed since is followed to its new key, whose entry must keep the old key's names.
    """
    notes = []
    for name, (document, list_name) in documents.items():
        old = committed_index(base, name)
        if old is False:
            warn(f"{base!r} is not a commit here (or git is missing), so vanished keys and names were not checked")
            return notes
        if old is None:
            notes.append(f"{name}: no committed index at {base} yet, so no published keys or names to keep")
            continue
        by_key = {entry["k"]: entry for entry in document[list_name]}
        renamed = document["renamed"]
        old_list = old.get(list_name)
        old_entries = [entry for entry in (old_list if isinstance(old_list, list) else [])
                       if isinstance(entry, dict) and isinstance(entry.get("k"), str)]
        for key in sorted({entry["k"] for entry in old_entries} - set(by_key)):
            if key not in renamed:
                error(f"{INDEX_REL}/{name}: {key!r} was published at {base} and is gone; "
                      "add it to meta/renamed.json with the key that replaced it")
        old_renamed = old.get("renamed", {})
        for key in sorted(set(old_renamed) - set(renamed)):
            error(f"{INDEX_REL}/{name}: the renamed entry {key!r} -> {old_renamed[key]!r} was published at {base} "
                  "and is gone; keep it in meta/renamed.json")

        for entry in old_entries:
            key = entry["k"]
            now = by_key.get(key) or by_key.get(renamed.get(key))
            if now is None:
                continue  # a vanished key, an error of its own above
            label = key if now["k"] == key else f"{key} (now {now['k']})"
            kept = set(entry_names(now, KEPT_NAMES[list_name]))
            gone = [text for text in entry_names(entry, PUBLISHED_NAMES[list_name]) if text not in kept]
            if gone:
                said = (f"the name {listed(gone)} published at {base} is gone" if len(gone) == 1
                        else f"the names {listed(gone)} published at {base} are gone")
                if list_name == "brands":
                    advice = ("a brand keeps every name it published in its n, a, o or r. A name override in "
                              "meta/entities.json puts the pack names it replaces in r by itself; otherwise add the "
                              "old name to the brand's aliases there, or to its former names for a rebrand")
                else:
                    advice = ("a bank keeps every name it published in its n or a (banks.json has no o or r): "
                              "add the old name to the bank's aliases in meta/entities.json")
                error(f"{INDEX_REL}/{name}: {label}: {said}; {advice}")
            if list_name == "banks":
                lost = [text for text in entry_names(entry, ("a",)) if text not in kept]
                if lost:
                    said = (f"the alias {listed(lost)} published at {base} is gone" if len(lost) == 1
                            else f"the aliases {listed(lost)} published at {base} are gone")
                    warn(f"{INDEX_REL}/{name}: {label}: {said}. If it was the bank's name once, keep it: "
                         "a bank of the user's titled so stops counting as the catalog bank's shadow")
            had = entry.get("c")
            if list_name == "brands" and isinstance(had, list) and had and not now.get("c"):
                why = international_why.get(now["k"]) or "as it has no countries"
                warn(f"{INDEX_REL}/{name}: {label} had countries ({', '.join(map(str, had))}) at {base} and is "
                     f"international now, {why}. If that isn't meant, give it its countries in meta/entities.json")
    return notes


def check_published_logos(base):
    """A logo copy committed at `base` is still here, referenced or not.

    An older index, or the snapshot bundled in an app already shipped, may still point at it, and
    a copy deleted from the repository is a 404 there and a letter plate in place of the logo. A
    clean-up of unreferenced copies, or a `rm -rf index/v2` before a build, would otherwise pass.
    When git can't tell, check_published_keys has warned already.
    """
    try:
        listed = git("ls-tree", "-r", "-z", "--name-only", f"{base}^{{commit}}", "--", f"{INDEX_REL}/logos")
    except OSError:
        return
    if listed.returncode != 0:
        return
    names = [name for name in listed.stdout.decode("utf-8", "surrogateescape").split("\0") if name]
    gone = [name for name in names if not (ROOT / name).exists()]
    if gone:
        shown = ", ".join(PurePosixPath(name).name for name in gone[:4]) + (", ..." if len(gone) > 4 else "")
        counted = (f"1 logo copy published at {base} is" if len(gone) == 1
                   else f"{len(gone)} logo copies published at {base} are")
        error(f"{INDEX_REL}/logos: {counted} gone ({shown}); copies are never deleted. "
              f"Bring them back with git checkout {base} -- {INDEX_REL}/logos")


def check_min_app_build():
    """MIN_APP_BUILD is 0 or on the app's own scale, where every build that reads index v2 is 1012018 or more."""
    if not is_int(MIN_APP_BUILD) or not (MIN_APP_BUILD == 0 or MIN_APP_BUILD >= MIN_APP_VERSION_SCALE):
        error(f"MIN_APP_BUILD is {MIN_APP_BUILD!r}: it is 0, or the oldest app version as "
              "major * 1,000,000 + minor * 1,000 + patch (1.13.0 = 1013000), never a store build number")


# ---------------------------------------------------------------------------------------------
# Output

def gzip_size(data):
    return len(gzip.compress(data, compresslevel=GZIP_LEVEL, mtime=0))


def check_budget(name, raw, packed):
    raw_limit, gzip_limit = BUDGETS[name]
    for label, size, limit in (("raw", raw, raw_limit), ("gzipped", packed, gzip_limit)):
        if size > limit:
            error(f"{INDEX_REL}/{name}: {kb(size)} {label}, over its budget of {kb(limit)}")
        elif size > limit * WARN_AT:
            warn(f"{INDEX_REL}/{name}: {kb(size)} {label}, over {round(WARN_AT * 100)}% of its budget of {kb(limit)}")


def kb(size):
    return f"{size / 1024:.1f} KB"


def size_line(name, raw, packed, what):
    raw_limit, gzip_limit = BUDGETS[name]
    return (f"  {INDEX_REL}/{name:<12} {what:<36} {kb(raw):>9} raw ({raw / raw_limit:4.0%} of {kb(raw_limit)}), "
            f"{kb(packed):>8} gzipped ({packed / gzip_limit:4.0%} of {kb(gzip_limit)})")


def report_and_exit(check, lines):
    for line in lines:
        print(line)
    if warnings:
        print(f"\nWarnings ({len(warnings)}):")
        for message in warnings:
            print(f"  ! {message}")
    if errors:
        print(f"\nErrors ({len(errors)}):")
        for message in errors:
            print(f"  ✗ {message}")
        print(f"\n{'Check' if check else 'Build'} FAILED: {len(errors)} error(s), {len(warnings)} warning(s)"
              + ("" if check else "; nothing was written"))
        sys.exit(1)
    print(f"\n{'Check passed' if check else 'Built'}: {len(warnings)} warning(s)")
    sys.exit(0)


class Resolved:
    """What packs/ and meta/ say, resolved into the index's bank and brand entries."""

    def __init__(self, **fields):
        self.__dict__.update(fields)


def resolve():
    """Reads packs/ and meta/ and builds the index entries, writing nothing and asking git nothing.

    Returns (Resolved, None), or (None, why) when the errors are such that building on would only
    bury them under follow-on complaints. Every problem found is in `errors` and `warnings`.
    validate.py calls this too, so both scripts see the same entries.
    """
    packs = load_packs()
    categories = load_categories()
    meta_complete = load_pack_meta(packs, categories)
    regions = load_regions()
    renamed = load_renamed()
    if errors or not meta_complete or regions is None:
        return None, "Packs or meta/ have errors, so the index was not built."

    ordered = sorted(packs.values(), key=lambda pack: pack.rank)
    bank_packs = [pack for pack in ordered if pack.type == "banks"]
    payee_packs = [pack for pack in ordered if pack.type == "payees"]

    banks_by_key = {}
    for pack in bank_packs:
        for entity in pack.entities:
            if entity["key"] in banks_by_key:
                error(f"bank key {entity['key']!r} is in {banks_by_key[entity['key']]} and {pack.id}; "
                      "a bank key must be in one pack only")
            banks_by_key.setdefault(entity["key"], pack.id)
    brand_keys = {entity["key"] for pack in payee_packs for entity in pack.entities}
    overrides = load_entity_meta(banks_by_key, brand_keys, categories)
    if errors:
        return None, "meta/entities.json or the bank keys have errors, so the index was not built."

    logos = Logos()
    banks = build_banks(bank_packs, overrides, logos)
    check_bank_groups(banks)
    international_why = {}
    brands = build_brands(payee_packs, overrides, categories, logos, international_why)
    renamed_banks, renamed_brands = split_renamed(renamed, set(banks_by_key), brand_keys)

    used = {brand["g"] for brand in brands}
    for cid in sorted(set(categories) - used):
        warn(f"meta/categories.json: no brand is in {cid!r}")

    return Resolved(
        categories=categories, regions=regions, bank_packs=bank_packs, payee_packs=payee_packs,
        logos=logos, banks=banks, brands=brands, renamed_banks=renamed_banks, renamed_brands=renamed_brands,
        international_why=international_why,
    ), None


def main():
    parser = argparse.ArgumentParser(description="Build the v2 catalog index from packs/ and meta/.")
    parser.add_argument("--check", action="store_true",
                        help="write nothing; fail if the committed index is stale or anything is wrong")
    parser.add_argument("--base", default="HEAD",
                        help="commit whose index the published keys are compared with (default: HEAD)")
    args = parser.parse_args()

    resolved, problem = resolve()
    if resolved is None:
        report_and_exit(args.check, [problem])
    categories, regions, logos = resolved.categories, resolved.regions, resolved.logos
    bank_packs, payee_packs = resolved.bank_packs, resolved.payee_packs
    banks, brands = resolved.banks, resolved.brands
    renamed_banks, renamed_brands = resolved.renamed_banks, resolved.renamed_brands

    updated, updated_note = last_input_date()
    common = {
        "format": FORMAT,
        "updated": updated,
        "min_app_build": MIN_APP_BUILD,
        "base_url": BASE_URL,
        "logo_path": LOGO_PATH,
    }
    banks_doc = dict(common, reserve=regions["reserve"], regions=regions["regions"],
                     country_names=regions["country_names"], renamed=renamed_banks, banks=banks)
    category_list = [
        {"id": cid, "rank": item["rank"], "icon": item["icon"], "color": item["color"], "n": item["n"]}
        for cid, item in sorted(categories.items(), key=lambda pair: pair[1]["rank"])
    ]
    brands_doc = dict(common, categories=category_list, renamed=renamed_brands, brands=brands)
    for document in (banks_doc, brands_doc):
        document["rev"] = index_rev(document)

    documents = {"banks.json": (banks_doc, "banks"), "brands.json": (brands_doc, "brands")}
    notes = check_published(args.base, documents, resolved.international_why)
    check_published_logos(args.base)
    check_min_app_build()

    texts = {name: render(document).encode("utf-8") for name, (document, _) in documents.items()}
    sizes = {name: (len(data), gzip_size(data)) for name, data in texts.items()}
    for name, (raw, packed) in sizes.items():
        check_budget(name, raw, packed)

    # Logo copies are immutable: never rewritten, never deleted, only added.
    missing_logos = []
    for h, data in sorted(logos.files.items()):
        path = LOGO_DIR / f"{h}.png"
        if not path.exists():
            missing_logos.append(h)
        elif path.read_bytes() != data:
            error(f"{rel(path)}: its bytes are not the logo with that hash; logo copies are never rewritten")
    unreferenced = 0
    if LOGO_DIR.is_dir():
        for path in sorted(LOGO_DIR.iterdir()):
            if path.name.startswith("."):
                continue
            if not re.fullmatch(r"[0-9a-f]{%d}\.png" % HASH_DIGITS, path.name):
                warn(f"{rel(path)}: not a hashed logo name")
            elif path.stem not in logos.files:
                unreferenced += 1
                if keyed_hash(LOGO_HASH_KEY, path.read_bytes()) != path.stem:
                    error(f"{rel(path)}: its bytes don't match its name")

    stale = []
    if args.check:
        for name, data in texts.items():
            path = INDEX_DIR / name
            if not path.exists() or path.read_bytes() != data:
                stale.append(f"{INDEX_REL}/{name}")
        if missing_logos:
            stale.append(f"{len(missing_logos)} logo(s) in {INDEX_REL}/logos/")
        if stale:
            error(f"the index is stale ({', '.join(stale)}); run python3 build.py and commit the result")

    if not errors and not args.check:
        INDEX_DIR.mkdir(parents=True, exist_ok=True)
        LOGO_DIR.mkdir(parents=True, exist_ok=True)
        for name, data in texts.items():
            path = INDEX_DIR / name
            if not path.exists() or path.read_bytes() != data:
                path.write_bytes(data)
        for h in missing_logos:
            (LOGO_DIR / f"{h}.png").write_bytes(logos.files[h])

    shared = sum(1 for brand in brands if "ps" in brand)
    by_category = {}
    for brand in brands:
        by_category[brand["g"]] = by_category.get(brand["g"], 0) + 1
    logo_bytes = sum(len(data) for data in logos.files.values())
    lines = [
        f"Index v2: format {FORMAT}, updated {updated}" + (f" ({updated_note})" if updated_note else ""),
        f"  rev: banks {banks_doc['rev']}, brands {brands_doc['rev']}",
        size_line("banks.json", *sizes["banks.json"],
                  f"{len(banks)} banks from {len(bank_packs)} packs"),
        size_line("brands.json", *sizes["brands.json"],
                  f"{len(brands)} brands from {len(payee_packs)} packs"),
        f"  {INDEX_REL}/logos/     {len(logos.files)} logos referenced, {logo_bytes / 1024 / 1024:.1f} MB; "
        + (f"{len(missing_logos)} to add" if args.check else f"{len(missing_logos)} added")
        + f", {unreferenced} older copies kept",
        f"  {shared} brand keys are shared between packs; "
        f"renamed: {len(renamed_banks)} bank(s), {len(renamed_brands)} brand(s)",
        "  Brands with replaced names (r): {}, with transliterations (t): {}, with former names (o): {}".format(
            *(sum(1 for brand in brands if field in brand) for field in ("r", "t", "o"))),
        "  Brands by category: " + ", ".join(
            f"{cid} {by_category.get(cid, 0)}"
            for cid, _ in sorted(categories.items(), key=lambda pair: pair[1]["rank"])),
    ] + [f"  {note}" for note in notes]
    report_and_exit(args.check, lines)


if __name__ == "__main__":
    main()
