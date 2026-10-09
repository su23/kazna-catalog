#!/usr/bin/env python3
"""Checks the packs and catalog.json before every commit and push.

    python3 validate.py                       compare the packs with the last commit (HEAD)
    python3 validate.py --base origin/main    or with another commit, such as before a push

Checks the schemas, logos, keys and sources; that a pack changed in what builds up to 1.12.17
read has a higher version; that a published key never just disappears; and warns about two
banks or two brands search can't tell apart. meta/ and the generated index/v2/ are checked by
`python3 build.py --check`, which has to pass as well.

Exits with status 1 when there are errors. Warnings are listed separately and don't fail
the run. Uses the Python standard library only, so it runs anywhere without a venv.
"""

import argparse
import datetime
import itertools
import json
import re
import struct
import subprocess
import sys
import unicodedata
import urllib.parse
import zlib
from collections import defaultdict
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent

# build.py, next to this file, resolves the packs and meta/ into the index entries the name check
# compares. Importing it must leave no __pycache__ in the repository.
sys.dont_write_bytecode = True
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import build  # noqa: E402

# The app drops a logo bigger than this (MAX_LOGO_BYTES in the app's LogoBytes.kt).
MAX_LOGO_BYTES = 256 * 1024
LOGO_SIZE = 128

errors = []
warnings = []


def error(message):
    errors.append(message)


def warn(message):
    warnings.append(message)


def rel(path):
    return path.relative_to(ROOT).as_posix()


# ---------------------------------------------------------------------------------------------
# Reading JSON

def _reject_duplicate_keys(pairs):
    # json.load keeps the last of two equal keys without a word, so a second "ru" name or a
    # repeated "logo" would silently replace the first.
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


# ---------------------------------------------------------------------------------------------
# JSON Schema
#
# A small subset of JSON Schema 2020-12: exactly the keywords the files in schema/ use. Any
# other keyword stops the run, so a schema edit can't quietly stop being checked.

class SchemaError(Exception):
    pass


SCHEMA_ANNOTATIONS = {"$schema", "$id", "$defs", "$comment", "title", "description"}
SCHEMA_KEYWORDS = {
    "$ref", "type", "const", "enum", "pattern", "minLength", "format", "minimum", "maximum",
    "required", "properties", "additionalProperties", "propertyNames",
    "items", "minItems", "maxItems", "uniqueItems", "allOf", "anyOf", "if", "then",
}

TYPE_TESTS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    # 1.0 is not an integer here: the app reads these fields as Int and fails on it.
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def is_url(text):
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc) and not re.search(r"\s", text)


def is_date(text):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return False
    try:
        datetime.date.fromisoformat(text)
    except ValueError:
        return False
    return True


FORMAT_TESTS = {"uri": is_url, "date": is_date}


def type_name(value):
    for name, test in TYPE_TESTS.items():
        if test(value):
            return name
    return "number"


def show(value):
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 60 else text[:57] + "..."


def same_json(a, b):
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def child_path(path, name):
    return f"{path}.{name}" if path else name


def item_path(path, index, item):
    # Name array items by their key or id where they have one: "banks[sberbank]" says more
    # than "banks[17]".
    label = index
    if isinstance(item, dict):
        for field in ("key", "id"):
            if isinstance(item.get(field), str):
                label = item[field]
                break
    return f"{path}[{label}]"


def resolve_ref(root, ref):
    if not ref.startswith("#/"):
        raise SchemaError(f"$ref {ref!r}: only references inside the same file are supported")
    node = root
    for part in ref[2:].split("/"):
        if not isinstance(node, dict) or part not in node:
            raise SchemaError(f"$ref {ref!r} points at nothing")
        node = node[part]
    return node


def schema_errors(value, schema, root, path=""):
    """Every way `value` breaks `schema`, as "where: what" messages."""
    where = path or "(top level)"
    if schema is True:
        return []
    if schema is False:
        return [f"{where}: not allowed here"]
    unknown = set(schema) - SCHEMA_KEYWORDS - SCHEMA_ANNOTATIONS
    if unknown:
        raise SchemaError(f"unsupported schema keyword(s): {', '.join(sorted(unknown))}")

    found = []
    if "$ref" in schema:
        found += schema_errors(value, resolve_ref(root, schema["$ref"]), root, path)
    if "type" in schema:
        allowed = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        for name in allowed:
            if name not in TYPE_TESTS:
                raise SchemaError(f"unsupported type {name!r}")
        if not any(TYPE_TESTS[name](value) for name in allowed):
            # The remaining keywords assume the right type, so they would only add noise.
            return found + [f"{where}: expected {' or '.join(allowed)}, found {type_name(value)}"]
    if "const" in schema and not same_json(value, schema["const"]):
        found.append(f"{where}: must be {show(schema['const'])}, not {show(value)}")
    if "enum" in schema and not any(same_json(value, option) for option in schema["enum"]):
        options = ", ".join(show(option) for option in schema["enum"])
        found.append(f"{where}: {show(value)} is not one of {options}")

    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            found.append(f"{where}: {show(value)} does not match {schema['pattern']}")
        if "minLength" in schema and len(value) < schema["minLength"]:
            found.append(f"{where}: shorter than {schema['minLength']} character(s)")
        if "format" in schema:
            test = FORMAT_TESTS.get(schema["format"])
            if test is None:
                raise SchemaError(f"unsupported format {schema['format']!r}")
            if not test(value):
                found.append(f"{where}: {show(value)} is not a valid {schema['format']}")

    if TYPE_TESTS["integer"](value):
        if "minimum" in schema and value < schema["minimum"]:
            found.append(f"{where}: {value} is below the minimum of {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            found.append(f"{where}: {value} is above the maximum of {schema['maximum']}")

    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                found.append(f"{where}: missing {name!r}")
        properties = schema.get("properties", {})
        for name, item in value.items():
            here = child_path(path, name)
            if "propertyNames" in schema:
                found += schema_errors(name, schema["propertyNames"], root, here)
            if name in properties:
                found += schema_errors(item, properties[name], root, here)
            elif schema.get("additionalProperties") is False:
                found.append(f"{where}: unknown field {name!r}")
            elif "additionalProperties" in schema:
                found += schema_errors(item, schema["additionalProperties"], root, here)

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            found.append(f"{where}: {len(value)} item(s), at least {schema['minItems']} needed")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            found.append(f"{where}: {len(value)} items, at most {schema['maxItems']} allowed")
        if schema.get("uniqueItems"):
            seen = set()
            for item in value:
                text = json.dumps(item, sort_keys=True)
                if text in seen:
                    found.append(f"{where}: {show(item)} is listed twice")
                seen.add(text)
        if "items" in schema:
            for index, item in enumerate(value):
                found += schema_errors(item, schema["items"], root, item_path(path, index, item))

    for part in schema.get("allOf", []):
        found += schema_errors(value, part, root, path)
    if "anyOf" in schema:
        attempts = [schema_errors(value, part, root, path) for part in schema["anyOf"]]
        if all(attempts):
            reasons = "; or ".join(attempt[0].partition(": ")[2] for attempt in attempts)
            found.append(f"{where}: fits none of the allowed forms ({reasons})")
    if "if" in schema and not schema_errors(value, schema["if"], root, path):
        found += schema_errors(value, schema.get("then", True), root, path)
    return found


def check_schema(value, schema, label):
    """True when `value` fits `schema`; otherwise records each problem under `label`."""
    problems = schema_errors(value, schema, schema)
    for problem in problems:
        error(f"{label}: {problem}")
    return not problems


# ---------------------------------------------------------------------------------------------
# PNG

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Bit depths PNG allows for each colour type, and the samples in a pixel of each.
PNG_DEPTHS = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
PNG_SAMPLES = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
# The seven Adam7 passes of an interlaced image: first column, first row, column step, row step.
ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
# Far beyond any logo (a 2048 px RGBA square); stops a broken header from asking for gigabytes.
MAX_RAW_BYTES = 2048 * 2048 * 4 + 2048


def png_size(data):
    """(width, height) of a PNG that decodes, or raises ValueError saying what is wrong.

    Walks the chunks with their CRCs and inflates the image data, checking that it holds
    exactly the rows the header promises, each with a known filter. This is what a decoder
    would trip over, read with struct and zlib so the script needs no Pillow.
    """
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("no PNG signature")
    pos = len(PNG_SIGNATURE)
    header = None
    has_palette = False
    image_data = []
    image_data_ended = False
    while True:
        if pos + 12 > len(data):
            raise ValueError("ends before its IEND chunk")
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        if not kind.isalpha():
            raise ValueError(f"unreadable chunk at byte {pos}")
        name = kind.decode("ascii")
        end = pos + 12 + length
        if end > len(data):
            raise ValueError(f"its {name} chunk is cut short")
        body = data[pos + 8:end - 4]
        (crc,) = struct.unpack(">I", data[end - 4:end])
        if zlib.crc32(kind + body) & 0xFFFFFFFF != crc:
            raise ValueError(f"bad checksum in its {name} chunk")
        if header is None and kind != b"IHDR":
            raise ValueError("does not start with an IHDR chunk")

        if kind == b"IHDR":
            if header is not None:
                raise ValueError("has two IHDR chunks")
            if length != 13:
                raise ValueError("its IHDR chunk has the wrong length")
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"PLTE":
            has_palette = True
        elif kind == b"IDAT":
            if image_data_ended:
                raise ValueError("its IDAT chunks are not consecutive")
            image_data.append(body)
        elif kind == b"IEND":
            if end != len(data):
                raise ValueError("has data after its IEND chunk")
            break
        if image_data and kind != b"IDAT":
            image_data_ended = True
        pos = end

    width, height, depth, colour, compression, filter_method, interlace = header
    if width == 0 or height == 0:
        raise ValueError("has no pixels")
    if depth not in PNG_DEPTHS.get(colour, ()):
        raise ValueError(f"bit depth {depth} with colour type {colour} is not a PNG format")
    if compression != 0 or filter_method != 0 or interlace not in (0, 1):
        raise ValueError("unknown compression, filter or interlace method")
    if colour == 3 and not has_palette:
        raise ValueError("palette image without a PLTE chunk")
    if not image_data:
        raise ValueError("has no IDAT chunk")

    bits_per_pixel = depth * PNG_SAMPLES[colour]
    scanlines = []  # (rows, bytes in a row without its filter byte) of each pass
    for x0, y0, dx, dy in (ADAM7 if interlace else ((0, 0, 1, 1),)):
        pass_width = (width - x0 + dx - 1) // dx if width > x0 else 0
        pass_height = (height - y0 + dy - 1) // dy if height > y0 else 0
        if pass_width and pass_height:
            scanlines.append((pass_height, (pass_width * bits_per_pixel + 7) // 8))
    expected = sum(rows * (1 + size) for rows, size in scanlines)
    if expected > MAX_RAW_BYTES:
        raise ValueError(f"is {width}x{height}, far too large for a logo")

    inflater = zlib.decompressobj()
    try:
        raw = inflater.decompress(b"".join(image_data), expected + 1)
    except zlib.error as e:
        raise ValueError(f"its image data does not inflate ({e})") from None
    if len(raw) > expected:
        raise ValueError("its image data is longer than its size allows")
    if len(raw) < expected or not inflater.eof:
        raise ValueError("its image data is cut short")
    offset = 0
    for rows, size in scanlines:
        for _ in range(rows):
            if raw[offset] > 4:
                raise ValueError(f"unknown row filter {raw[offset]}")
            offset += 1 + size
    return width, height


def check_logo_file(path):
    """Records whether the logo at `path` is a PNG the app keeps, of the right size."""
    data = path.read_bytes()
    if len(data) > MAX_LOGO_BYTES:
        error(f"{rel(path)}: {len(data) // 1024} KB; the app drops logos over {MAX_LOGO_BYTES // 1024} KB")
        return
    try:
        width, height = png_size(data)
    except ValueError as e:
        error(f"{rel(path)}: not a valid PNG: {e}")
        return
    if (width, height) != (LOGO_SIZE, LOGO_SIZE):
        warn(f"{rel(path)}: {width}x{height}, not {LOGO_SIZE}x{LOGO_SIZE}")


# ---------------------------------------------------------------------------------------------
# Packs

class Pack:
    def __init__(self, pack_id, data):
        self.id = pack_id
        self.data = data
        self.type = data["type"]
        self.entities = data[self.type]
        self.dir = ROOT / "packs" / pack_id


def local_file(url, base_url):
    """The file a catalog URL is served from, or None when the URL is not under base_url."""
    prefix = base_url + "/"
    if not url.startswith(prefix):
        return None
    path = (ROOT / url[len(prefix):]).resolve()
    return path if ROOT in path.parents else None


def load_packs(pack_schema):
    """Packs whose pack.json fits the schema, by id, and the folders that don't, by name, with
    what their pack.json holds (None when it isn't JSON)."""
    packs = {}
    broken = {}
    for folder in sorted((ROOT / "packs").iterdir()):
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        data = load_json(folder / "pack.json")
        if data is None or not check_schema(data, pack_schema, rel(folder / "pack.json")):
            broken[folder.name] = data
            continue
        if data["id"] != folder.name:
            error(f"{rel(folder / 'pack.json')}: id is {data['id']!r} but the folder is {folder.name!r}")
            broken[folder.name] = data
            continue
        packs[folder.name] = Pack(folder.name, data)
    return packs, broken


def check_index(catalog, packs, broken, base_url):
    """catalog.json lists each pack once, as its pack.json describes it."""
    listed = set()
    for entry in catalog["packs"]:
        pack_id = entry["id"]
        where = f"catalog.json: {pack_id}"
        if pack_id in listed:
            error(f"{where}: listed twice")
            continue
        listed.add(pack_id)

        expected_url = f"{base_url}/packs/{pack_id}/pack.json"
        if entry["pack_url"] != expected_url:
            error(f"{where}: pack_url is {entry['pack_url']}, but the pack lives at {expected_url}")
        if pack_id in broken:
            continue  # its own errors are already listed
        pack = packs.get(pack_id)
        if pack is None:
            error(f"{where}: packs/{pack_id}/pack.json does not exist")
            continue

        for field in ("type", "version", "name"):
            if not same_json(entry[field], pack.data[field]):
                error(f"{where}: {field} is {show(entry[field])} here but {show(pack.data[field])} in pack.json")
        if entry["entity_count"] != len(pack.entities):
            error(f"{where}: entity_count is {entry['entity_count']} but the pack has {len(pack.entities)} {pack.type}")

        # Previews stand for the pack before it's installed, so each must be one of its own
        # logos, which also means check_logos covers the file.
        logos = {entity.get("logo") for entity in pack.entities}
        for url in entry.get("preview_logos", []):
            if url not in logos:
                error(f"{where}: preview logo {url} is not the logo of any of its {pack.type}")

    for pack_id in sorted(set(packs) - listed):
        warn(f"packs/{pack_id}: not listed in catalog.json, so the app never offers it")


def check_keys(packs, base_url):
    """Keys are unique where the app needs them to be, and shared payees are true copies."""
    bank_packs = defaultdict(list)
    payee_copies = defaultdict(list)
    for pack in packs.values():
        seen = set()
        for entity in pack.entities:
            key = entity["key"]
            if key in seen:
                error(f"packs/{pack.id}: key {key!r} appears twice")
                continue
            seen.add(key)
            if pack.type == "banks":
                bank_packs[key].append(pack.id)
            else:
                payee_copies[key].append((pack, entity))

    # catalog_bank.bank_key is UNIQUE in the app: installing the second pack moves the bank to
    # it, and removing either pack deletes the bank for both.
    for key, owners in sorted(bank_packs.items()):
        if len(owners) > 1:
            error(f"bank key {key!r} is in {len(owners)} packs ({', '.join(owners)}); a bank key must be in one pack only")

    # A payee key may sit in several packs (app DB 301), but the app stores one row per key, so
    # whichever pack is installed last would win any difference. The logo URLs differ by pack
    # folder, so the files are compared instead.
    for key, copies in sorted(payee_copies.items()):
        first_pack, first = copies[0]
        first_logo = logo_bytes(first, base_url)
        for pack, entity in copies[1:]:
            fields = sorted(
                field for field in set(first) | set(entity)
                if field != "logo" and not same_json(first.get(field), entity.get(field))
            )
            if base_url and logo_bytes(entity, base_url) != first_logo:
                fields.append("logo file")
            if fields:
                error(f"payee {key!r} differs between {first_pack.id} and {pack.id}: {', '.join(fields)}")

    # A brand groups a bank's country entries into one row in the app. A brand nothing else
    # carries, and that is no bank's key, groups nothing: most likely a typo.
    brand_members = defaultdict(set)
    for key in bank_packs:
        brand_members[key].add(key)
    for pack in packs.values():
        for entity in pack.entities:
            if "brand" in entity:
                brand_members[entity["brand"]].add(entity["key"])
    for pack in packs.values():
        for entity in pack.entities:
            brand = entity.get("brand")
            if brand is not None and len(brand_members[brand]) < 2:
                warn(f"packs/{pack.id}: bank {entity['key']!r} has brand {brand!r}, which no other bank shares")


def logo_bytes(entity, base_url):
    """The bytes of an entity's logo file, or None when it has no logo or the file is missing."""
    path = local_file(entity["logo"], base_url) if base_url and "logo" in entity else None
    return path.read_bytes() if path is not None and path.is_file() else None


def check_logos(packs, base_url):
    """Every logo is a file the app can keep, and every logo file is used."""
    used = set()
    checked = set()
    for pack in packs.values():
        for entity in pack.entities:
            url = entity.get("logo")
            if url is None:
                continue
            where = f"packs/{pack.id}: {entity['key']}"
            path = local_file(url, base_url)
            if path is None:
                error(f"{where}: logo {url} is not under {base_url}/")
                continue
            used.add(path)
            expected = f"{base_url}/packs/{pack.id}/logos/{entity['key']}.png"
            if url != expected:
                warn(f"{where}: logo is {url}; logos belong at {expected}")
            if not path.is_file():
                error(f"{where}: logo file {rel(path)} does not exist")
            elif path not in checked:
                checked.add(path)
                check_logo_file(path)

    for pack in packs.values():
        logos_dir = pack.dir / "logos"
        if not logos_dir.is_dir():
            continue
        for path in sorted(logos_dir.iterdir()):
            if not path.name.startswith(".") and path.resolve() not in used:
                warn(f"{rel(path)}: no entity uses this file")


def check_sources(packs):
    """Each logo's origin is written down in its pack's sources.json."""
    for pack in packs.values():
        with_logo = [entity["key"] for entity in pack.entities if "logo" in entity]
        path = pack.dir / "sources.json"
        if not path.exists():
            if with_logo:
                warn(f"packs/{pack.id}: no sources.json, so no logo's source is recorded ({len(with_logo)} logos)")
            continue
        sources = load_json(path)
        if sources is None:
            continue
        if not isinstance(sources, dict):
            error(f"{rel(path)}: expected an object of entries by key")
            continue
        keys = {entity["key"] for entity in pack.entities}
        for key in sorted(set(sources) - keys):
            warn(f"{rel(path)}: entry {key!r} matches no entity in the pack")
        # An entry with only a note (say, "white background removed") records an edit, not
        # where the logo came from.
        missing = [
            key for key in with_logo
            if not isinstance(sources.get(key), dict) or not ({"source", "kind"} & set(sources[key]))
        ]
        if missing:
            listed = ", ".join(missing[:5]) + (", ..." if len(missing) > 5 else "")
            warn(f"{rel(path)}: no recorded source for {len(missing)} of {len(with_logo)} logos ({listed})")


# ---------------------------------------------------------------------------------------------
# What was published: version bumps and keys

# What builds up to 1.12.17 act on in an installed pack.json: the fields of PackDefinition,
# PackPayeeEntry and PackBankEntry in the app's model/CatalogModels.kt, each with the value the
# app assumes when it is absent. Those builds read a pack again only when its version goes up, so
# a change to any of these needs a bump. Nothing else does, and nothing else should get one: a
# bump shows an update badge to everyone who installed the pack, and an update on 1.12.16 or
# older retitles its payees. That leaves out aliases, a payee's category and countries (only the
# v2 index reads them), match (read only at install and update, which download the pack anew), a
# payee's mcc and website and a bank's website (parsed, never read: newer builds take mcc and
# website from the v2 index) and the order of the entries (an update adds new ones, but never
# reorders the ones a user has). Names count in every language: builds up to 1.12.15 pick a name
# by the device's own language, not only the app's five.
OLD_APP_PACK_FIELDS = ("id", "type", "name")
OLD_APP_FIELDS = {
    "payees": {"key": None, "name": {}, "logo": ""},
    "banks": {"key": None, "name": {}, "logo": "", "countries": None, "card_background": None,
              "accent_color": None, "closed": False, "brand": None},
}


def git(*args, stdin=None):
    return subprocess.run(["git", "-C", str(ROOT), *args], input=stdin, capture_output=True)


def committed_files(base, paths):
    """{path: bytes} of the files at `paths` (relative to this folder) as committed at `base`."""
    if not paths:
        return {}
    request = "".join(f"{base}:./{path}\n" for path in paths).encode("utf-8", "surrogateescape")
    out = git("cat-file", "--batch", stdin=request).stdout
    found = {}
    pos = 0
    for path in paths:
        end = out.find(b"\n", pos)
        if end < 0:
            break
        header = out[pos:end].split()
        pos = end + 1
        if len(header) == 3 and header[1] == b"blob":
            size = int(header[2])
            found[path] = out[pos:pos + size]
            pos += size + 1  # the object, then a newline
    return found


def packs_at(base):
    """Each pack.json committed at `base`, parsed (None if it doesn't parse), by pack id.

    None, after a warning, when git can't tell: then nothing is compared with `base`.
    """
    skipped = "so packs were not checked for missing version bumps or removed keys"
    try:
        found = git("rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    except OSError:
        warn(f"git is not available, {skipped}")
        return None
    if found.returncode != 0:
        warn(f"{base!r} is not a commit here (or this is not a git checkout), {skipped}")
        return None
    commit = found.stdout.decode("ascii", "replace").strip()
    listed = git("ls-tree", "-r", "-z", "--name-only", commit, "--", "packs")
    if listed.returncode != 0:
        warn(f"git could not list the packs at {base}, {skipped}")
        return None
    paths = [
        name for name in listed.stdout.decode("utf-8", "surrogateescape").split("\0")
        if len(PurePosixPath(name).parts) == 3 and name.startswith("packs/") and name.endswith("/pack.json")
    ]
    files = committed_files(commit, paths)
    packs = {}
    for path in paths:
        try:
            packs[PurePosixPath(path).parts[1]] = parse_json(files[path].decode("utf-8"))
        except (KeyError, ValueError, UnicodeDecodeError):
            packs[PurePosixPath(path).parts[1]] = None
    return packs


def old_app_view(data):
    """The parts of a pack.json that builds up to 1.12.17 act on, leaving out values they assume anyway.

    Entries are by key, as their order changes nothing for a user who has the pack.
    """
    if not isinstance(data, dict):
        return {"pack": {}, "entries": {}}
    kind = data.get("type")
    defaults = OLD_APP_FIELDS.get(kind, {}) if isinstance(kind, str) else {}
    entries = {}
    listed = data.get(kind) if defaults else None
    for entity in listed if isinstance(listed, list) else []:
        if isinstance(entity, dict):
            entries[str(entity.get("key"))] = {
                field: entity[field] for field in defaults
                if field in entity and not same_json(entity[field], defaults[field])
            }
    return {"pack": {field: data.get(field) for field in OLD_APP_PACK_FIELDS}, "entries": entries}


def old_app_changes(old, new):
    """What builds up to 1.12.17 would read differently in `new` than in `old`, in a few words each."""
    before, after = old_app_view(old), old_app_view(new)
    changes = [f"the pack's {field}" for field in OLD_APP_PACK_FIELDS
               if not same_json(before["pack"][field], after["pack"][field])]
    for key in sorted(set(before["entries"]) | set(after["entries"])):
        if key not in before["entries"]:
            changes.append(f"{key} added")
        elif key not in after["entries"]:
            changes.append(f"{key} removed")
        else:
            a, b = before["entries"][key], after["entries"][key]
            fields = [field for field in sorted(set(a) | set(b)) if not same_json(a.get(field), b.get(field))]
            if fields:
                changes.append(f"{key} {'/'.join(fields)}")
    return changes


def changed_since(base):
    """Paths under packs/ that differ from `base` in the working tree, untracked ones included, or None.

    An untracked file that `base` has byte for byte is no change. A branch that has taken main's
    newer packs in as files, before it is rebased onto main, would otherwise need a bump for each
    of them when compared with origin/main, although nothing published differs. (git diff lists
    such a file too, as deleted, since it isn't in the branch's index.)
    """
    changed = git("diff", "--name-only", "-z", "--relative", base, "--", "packs")
    untracked = git("ls-files", "--others", "--exclude-standard", "-z", "--", "packs")
    if changed.returncode != 0 or untracked.returncode != 0:
        return None
    names = [name for name in changed.stdout.decode("utf-8", "surrogateescape").split("\0") if name]
    new = [name for name in untracked.stdout.decode("utf-8", "surrogateescape").split("\0") if name]
    at_base = committed_files(base, new)
    unchanged = set()
    for name in new:
        try:
            if at_base.get(name) == (ROOT / name).read_bytes():
                unchanged.add(name)
        except OSError:
            pass
    names = sorted(set(names + new) - unchanged)
    return [name for name in names if not PurePosixPath(name).name.startswith(".")]


def check_version_bumps(packs, old_packs, base, base_url):
    """A pack that changed since `base` in what builds up to 1.12.17 read has a higher version,
    and only such a pack has one.

    Those builds re-download a pack only when its version goes up, so a change without a bump
    never reaches anyone who installed it. A logo file counts for every pack whose entries use
    it, and only for those: deleting a file nobody uses needs no bump.
    """
    names = changed_since(base)
    if names is None:
        warn(f"git could not list the changes since {base}, so packs were not checked for missing version bumps")
        return
    pack_files = set()
    logo_files = set()
    for name in names:
        parts = PurePosixPath(name).parts
        if len(parts) == 3 and parts[0] == "packs" and parts[2] == "pack.json":
            pack_files.add(parts[1])
        elif len(parts) == 4 and parts[0] == "packs" and parts[2] == "logos":
            logo_files.add((ROOT / name).resolve())

    for pack_id, pack in sorted(packs.items()):
        if base_url is None:
            # Without catalog.json the logo URLs can't be resolved: fall back to the pack's own folder.
            used = {path for path in logo_files if path.parent == (pack.dir / "logos").resolve()}
        else:
            used = {local_file(entity["logo"], base_url) for entity in pack.entities if "logo" in entity}
        logos = sorted(path for path in logo_files if path in used)
        if pack_id not in pack_files and not logos:
            continue
        old = old_packs.get(pack_id)
        old_version = old.get("version") if isinstance(old, dict) else None
        if not TYPE_TESTS["integer"](old_version):
            continue  # a new pack, or one that was broken at base: any version will do
        new_version = pack.data["version"]

        content = old_app_changes(old, pack.data) if pack_id in pack_files else []
        pack_dir = pack.dir.resolve()
        content += [path.relative_to(pack_dir).as_posix() if pack_dir in path.parents else rel(path) for path in logos]
        if new_version < old_version:
            error(f"packs/{pack_id}: version went down from {old_version} to {new_version} since {base}")
        elif content and new_version == old_version:
            shown = "; ".join(content[:4]) + ("; ..." if len(content) > 4 else "")
            error(f"packs/{pack_id}: changed since {base} in what builds up to 1.12.17 read ({shown}) "
                  f"but version is still {new_version}; bump it in pack.json and catalog.json, "
                  "or those who installed the pack never get the change")
        elif not content and new_version > old_version:
            warn(f"packs/{pack_id}: version went from {old_version} to {new_version} since {base}, but nothing "
                 "builds up to 1.12.17 read changed; the bump only shows everyone with the pack an update badge")


def keys_by_kind(pack_data):
    """{"banks": {key: pack id}, "payees": {key: pack id}} over pack.json contents by pack id."""
    keys = {"banks": {}, "payees": {}}
    for pack_id, data in sorted(pack_data.items()):
        kind = data.get("type") if isinstance(data, dict) else None
        listed = data.get(kind) if isinstance(kind, str) and kind in keys else None
        for entity in listed if isinstance(listed, list) else []:
            if isinstance(entity, dict) and isinstance(entity.get("key"), str):
                keys[kind].setdefault(entity["key"], pack_id)
    return keys


def load_renamed():
    """meta/renamed.json: old key -> the key that replaced it."""
    path = ROOT / "meta" / "renamed.json"
    if not path.exists():
        return {}
    data = load_json(path)
    if data is None:
        return {}
    if not isinstance(data, dict) or not all(isinstance(new, str) for new in data.values()):
        error(f"{rel(path)}: expected an object of old key -> new key")
        return {}
    return data


def check_removed_keys(packs, broken, old_packs, base):
    """A bank or payee key published at `base` is still in a pack of its kind, or meta/renamed.json
    names the key that replaced it.

    The app links accounts and payees to catalog keys for good, and build.py turns renamed.json
    into the index's `renamed`, which apps follow to the new key.
    """
    now = {pack_id: pack.data for pack_id, pack in packs.items()}
    # A pack with errors still holds its keys, as far as it can be read. One that isn't JSON
    # can't say, and its own error is listed already.
    now.update({pack_id: data for pack_id, data in broken.items() if data is not None})
    unreadable = {pack_id for pack_id, data in broken.items() if data is None}
    current = keys_by_kind(now)
    published = keys_by_kind(old_packs)
    renamed = None
    vanished = defaultdict(list)  # (noun, pack id at base) -> keys that renamed.json doesn't cover
    for kind, noun in (("banks", "bank"), ("payees", "payee")):
        for key, pack_id in sorted(published[kind].items()):
            if key in current[kind] or pack_id in unreadable:
                continue
            if renamed is None:
                renamed = load_renamed()
            new = renamed.get(key)
            if new is None:
                vanished[(noun, pack_id)].append(key)
            elif new not in current[kind]:
                error(f"meta/renamed.json: {key!r} -> {new!r}, but no {noun} pack has the key {new!r}")
    for (noun, pack_id), keys in vanished.items():
        if len(keys) == 1:
            error(f"packs/{pack_id}: {noun} key {keys[0]!r} was published at {base} and is in no pack now; "
                  f"keep it, or add \"{keys[0]}\": \"<the key that replaced it>\" to meta/renamed.json")
        else:
            shown = ", ".join(keys[:6]) + (", ..." if len(keys) > 6 else "")
            error(f"packs/{pack_id}: {len(keys)} {noun} keys published at {base} are in no pack now ({shown}); "
                  "keep them, or give each the key that replaced it in meta/renamed.json")


# ---------------------------------------------------------------------------------------------
# Names that can't be told apart

def fold_name(text):
    """A name the way search compares it: case, ё/е, Latin accents, punctuation and spaces ignored.

    "Т-Банк" = "т банк", "Café" = "CAFE", "İşbank" = "isbank". Cyrillic й and ї keep their marks:
    they are letters of their own.
    """
    text = unicodedata.normalize("NFC", text).casefold().replace("ё", "е").replace("ı", "i")
    kept = []
    base = ""
    for ch in unicodedata.normalize("NFD", text):
        if unicodedata.combining(ch):
            if base and ord(base) < 0x250:  # a mark on a Latin letter
                continue
        else:
            base = ch
        kept.append(ch)
    return "".join(ch for ch in unicodedata.normalize("NFC", "".join(kept)) if ch.isalnum())


def shared_countries(a, b):
    """Where index entries `a` and `b` are both offered, in words, or None. No `c` = everywhere."""
    ours, theirs = set(a.get("c", [])), set(b.get("c", []))
    if not ours and not theirs:
        return "every country"
    common = (ours or theirs) if not (ours and theirs) else ours & theirs
    return ", ".join(sorted(common)) if common else None


def check_equal_names(broken):
    """Warns about two banks, or two brands, whose names fold to the same text in a country they share.

    Search can't tell such a pair apart, and a payee of that name could be linked to either. The
    entries come from build.py's own reading of the packs and meta/, so their countries and
    aliases are the ones the index publishes. That reading trusts the packs to fit the schema.
    """
    if broken:
        warn(f"names were not compared, because {len(broken)} pack(s) have errors")
        return
    try:
        resolved, _ = build.resolve()
    except Exception as e:  # build.py's own bug: report it, but keep this run's findings
        warn(f"names were not compared: build.py could not read the packs ({type(e).__name__}: {e})")
        return
    if build.errors:
        warn(f"python3 build.py --check finds {len(build.errors)} error(s) in the packs or meta/; run it to see them"
             + ("" if resolved else ". Names were not compared"))
    if resolved is None:
        return
    for noun, entries in (("banks", resolved.banks), ("brands", resolved.brands)):
        by_name = defaultdict(dict)  # folded name -> {key: (entry, the name as written)}
        for entry in entries:
            for text in list(entry["n"].values()) + entry.get("a", []):
                folded = fold_name(text)
                if folded:
                    by_name[folded].setdefault(entry["k"], (entry, text))
        reported = set()
        for folded, found in sorted(by_name.items()):
            for (key, (a, text_a)), (other, (b, text_b)) in itertools.combinations(sorted(found.items()), 2):
                where = shared_countries(a, b)
                if where is None or (key, other) in reported:
                    continue
                reported.add((key, other))
                names = f"«{text_a}»" if text_a == text_b else f"«{text_a}» and «{text_b}»"
                warn(f"{noun} {key} ({a['p']}) and {other} ({b['p']}) both go by {names} in {where}; "
                     "search can't tell them apart (rename one, or drop the alias)")


# ---------------------------------------------------------------------------------------------

def report(pack_count):
    """Prints the warnings, then the errors, and exits with status 1 if there are errors."""
    if warnings:
        print(f"Warnings ({len(warnings)}):")
        for message in warnings:
            print(f"  ! {message}")
        print()
    if errors:
        print(f"Errors ({len(errors)}):")
        for message in errors:
            print(f"  ✗ {message}")
        print(f"\nValidation FAILED: {len(errors)} error(s), {len(warnings)} warning(s) in {pack_count} pack(s)")
        sys.exit(1)
    print(f"Validation passed: {pack_count} pack(s), {len(warnings)} warning(s)")
    sys.exit(0)


def unsupported_schema(name, problem):
    print(f"schema/{name}: {problem}. validate.py implements only the keywords in SCHEMA_KEYWORDS.")
    sys.exit(2)


def main():
    parser = argparse.ArgumentParser(description="Check the catalog before a commit or a push.")
    parser.add_argument("--base", default="HEAD",
                        help="the commit to compare with for version bumps and removed keys (default: HEAD)")
    args = parser.parse_args()

    catalog_schema = load_json(ROOT / "schema" / "catalog.schema.json")
    pack_schema = load_json(ROOT / "schema" / "pack.schema.json")
    catalog = load_json(ROOT / "catalog.json")
    if catalog_schema is None or pack_schema is None:
        report(0)

    try:
        packs, broken = load_packs(pack_schema)
    except SchemaError as e:
        unsupported_schema("pack.schema.json", e)
    try:
        index_ok = catalog is not None and check_schema(catalog, catalog_schema, "catalog.json")
    except SchemaError as e:
        unsupported_schema("catalog.schema.json", e)

    base_url = catalog["base_url"].rstrip("/") if index_ok else None
    if index_ok:
        check_index(catalog, packs, broken, base_url)
        check_logos(packs, base_url)
    else:
        warn("pack listings and logos were not checked, because catalog.json has errors")
    check_keys(packs, base_url)
    check_sources(packs)
    old_packs = packs_at(args.base)
    if old_packs is not None:
        check_version_bumps(packs, old_packs, args.base, base_url)
        check_removed_keys(packs, broken, old_packs, args.base)
    check_equal_names(broken)
    report(len(packs) + len(broken))


if __name__ == "__main__":
    main()
