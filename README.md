# kazna-catalog

Payee and bank packs with logos for [Kazna](https://kazna.app).

Served by GitHub Pages at `https://catalog.kazna.app`. A push to `main` is live within minutes.

## Layout

```
catalog.json                 index of every pack; the app fetches it to list them
schema/
  catalog.schema.json        the shape of catalog.json
  pack.schema.json           the shape of packs/{id}/pack.json
packs/{id}/
  pack.json                  the pack itself: what the app installs
  logos/{key}.png            one logo per payee or bank, named by its key
  sources.json               where each logo came from (for us; the app never reads it)
validate.py                  checks all of the above; run it before every commit
download_logos.py            an early Clearbit/DuckDuckGo fetcher, no longer how logos are sourced
```

Files are edited in place, so every URL stays the same for good. A pack's `version` tells installed apps that it changed.

## catalog.json

Top level: `schema_version` (1), `generated_at` (the date of the last edit), `base_url` (`https://catalog.kazna.app`, which the app also has built in) and `packs`.

Each entry in `packs`:

| Field | Description |
|---|---|
| `id` | The pack's folder name, kebab-case |
| `type` | `payees` or `banks` |
| `version` | The same number as in pack.json |
| `name`, `description` | Localized text (see below) |
| `tags` | Kebab-case labels. The app stores them but doesn't show them yet. |
| `entity_count` | How many payees or banks pack.json has |
| `pack_url` | `{base_url}/packs/{id}/pack.json` |
| `preview_logos` | Up to 4 logo URLs of the pack's own payees or banks, shown on payee pack cards before install |

**Localized text** is an object keyed by language code, such as `{"en": "Russian Supermarkets", "ru": "Супермаркеты"}`. `en` is required, and the app falls back to it. Any code is allowed. Today the app shows `en`, `ru`, `de`, `es` and `fr`; names in other languages (`kk`, `uk`, `be`) wait until it does.

## pack.json

```json
{
  "id": "ru-supermarkets", "version": 2, "type": "payees",
  "name": { "en": "Russian Supermarkets", "ru": "Супермаркеты" },
  "payees": [
    { "key": "pyaterochka", "name": { "en": "Pyaterochka", "ru": "Пятёрочка" },
      "logo": "https://catalog.kazna.app/packs/ru-supermarkets/logos/pyaterochka.png",
      "mcc": [5411], "website": "https://5ka.ru" }
  ]
}
```

A payee pack has `payees` and a bank pack has `banks`, never both.

**Payee:** `key`, `name`, `logo`, plus optional `mcc` (merchant category codes, 1–9999) and `website`.

**Bank:**

```json
{ "key": "tbc-ge", "name": { "en": "TBC Bank", "ru": "ТБС Банк" },
  "logo": "https://catalog.kazna.app/packs/ge-banks/logos/tbc-ge.png",
  "countries": ["GE"], "website": "https://tbcbank.ge",
  "card_background": "solid:#00ADEE", "accent_color": "#00ADEE", "brand": "tbc" }
```

| Field | Description |
|---|---|
| `key`, `name` | Required |
| `logo` | Optional. A bank without one (a closed bank, say) is shown by its initial. |
| `countries` | ISO 3166-1 alpha-2 codes, or `null` for an international bank such as Revolut |
| `website` | The bank's site |
| `card_background` | `solid:#RRGGBB`, the colour of the bank's cards |
| `accent_color` | `#RRGGBB` |
| `closed` | `true` for a bank that no longer operates |
| `brand` | The group a country entry belongs to, such as `tbc` for `tbc-ge` and `tbc-uz`. The app shows each brand as one bank. |

### Keys

- Keys are kebab-case and permanent: the app links accounts and payees to them. Never rename or reuse a key.
- A bank key is in exactly one pack. The app keeps one row per bank key, so removing either of two packs that share one would delete the bank for both.
- Six old global-banks keys contain `_` (`bank_of_america`, `wells_fargo`, `deutsche_bank`, `bnp_paribas`, `credit_agricole`, `societe_generale`). The app's seed pins them, so they stay as they are. The schema allows exactly these six.
- A payee key may be in several packs (since app DB 301), as identical copies: the same name, MCCs, website and logo bytes. Today that's `yandex-go`, `yandex-cloud` and `lamoda`. Builds 1.12.16 and older can still drop a shared payee that has no transactions when one of its packs is removed.
- A bank group that operates in several countries gets one entry per country, each with the same `brand`, and its global entry leaves those countries out. Banks that are one institution across countries (Revolut, Wise, N26) stay a single entry.

## Versions

- Each pack has one integer `version`, in pack.json and in its catalog.json entry.
- Bump it whenever pack.json or any of the pack's logos changes, even for a logo-only fix. The app downloads a pack again only when its version goes up, so a change without a bump never reaches people who installed the pack.
- `sources.json`, and a pack's `description`, `tags` and `preview_logos` in catalog.json, need no bump: the app reads those from catalog.json, not from an installed pack.
- GitHub Pages caches files for 10 minutes. An app that installs a pack just after a push can still get the old logo.

## Logos

- PNG, 128×128, RGBA with a transparent background. The app draws every mark on a light plate.
- At most 256 KB, or the app drops the logo. Most are under 15 KB.
- Remove a white or light box around a mark, but keep full-bleed designs where white is part of the logo.
- Sourcing order: the brand's press kit or brand book, then a Wikimedia Commons SVG, then the brand site's SVG, and an app-store icon (RuStore for Russian brands) only as a last resort.
- Record every logo in its pack's `sources.json`, keyed by entity key:

  ```json
  { "lukoil": { "source": "https://commons.wikimedia.org/wiki/File:LUK_OIL_Logo.svg",
                "kind": "Wikimedia Commons SVG", "licence": "Public domain; trademark", "fetched": "2026-10-08",
                "processing": "rendered/cropped to the mark, fitted to 128x128 RGBA with a 6 px margin" } }
  ```

  Use `"source": null` when the URL is unknown, and say why in `note`.

## Validating

```
python3 validate.py                       # before a commit: compares the packs with HEAD
python3 validate.py --base origin/main    # before a push: compares them with what is published
```

It uses only the Python standard library, so it needs no venv. It exits with status 1 when there are errors, and lists warnings separately.

**Errors:**
- catalog.json or a pack.json doesn't fit its schema, isn't valid JSON, or repeats a key inside one object.
- A pack's folder name, `id`, `type`, `version` or `name` disagree between catalog.json and pack.json, or `entity_count` is wrong.
- `pack_url` is not `{base_url}/packs/{id}/pack.json`, or a pack is listed twice.
- A preview logo is not the logo of one of the pack's own entities.
- A key appears twice in one pack, or a bank key is in more than one pack.
- The copies of a shared payee differ.
- A logo file is missing, isn't a valid PNG, or is over 256 KB.
- A pack's pack.json or logos changed since the base commit (HEAD unless `--base` says otherwise) without a version bump, or its version went down. When git isn't available, this check is skipped with a warning.

**Warnings:**
- A logo is not 128×128.
- A logo URL is not `packs/{id}/logos/{key}.png`.
- A file in `logos/` is used by no entity.
- A pack folder is not listed in catalog.json.
- A bank's `brand` is shared by no other bank and is no bank's key.
- A pack has no `sources.json`, or it records no source for some logos.

The schemas are checked by a small built-in JSON Schema subset. A schema keyword it doesn't implement stops the run (exit status 2), so a schema edit can't silently go unchecked.

## Adding a pack

1. Create `packs/{id}/pack.json` and add the logos to `packs/{id}/logos/`.
2. Write `packs/{id}/sources.json`.
3. Add the pack to catalog.json, with up to 4 `preview_logos`, and set `generated_at` to today.
4. Run `python3 validate.py`, then commit and push to `main`.

A new country's bank pack installs itself for people in that country only after an app release maps the country's currency and seeds the pack. Until then, people add it from Banks → Bank Catalog.

## Updating a pack

1. Edit `pack.json`, or replace logos in place under the same file names.
2. Bump `version` in both pack.json and catalog.json. Update `entity_count`, `description` and `preview_logos` if they changed, and set `generated_at` to today.
3. Run `python3 validate.py`, then commit and push to `main`.
