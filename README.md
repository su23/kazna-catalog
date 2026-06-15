# kazna-catalog

Content catalog for [Kazna](https://kazna.app) — downloadable packs of payees, banks, and categories with logos.

Served via GitHub Pages at `https://catalog.kazna.app`.

## Structure

```
catalog.json                          ← root index, fetched by the app on marketplace open
schema/
  catalog.schema.json                 ← JSON schema for catalog.json
  pack.schema.json                    ← JSON schema for pack files
packs/
  {pack-id}/
    logos/                            ← PNG logos, stable paths (not versioned)
      {key}.png
    v{n}/
      pack.json                       ← versioned pack definition
```

## catalog.json

The root index lists all available packs. The app fetches this once to populate the marketplace screen.

Key fields per pack entry:

| Field | Description |
|---|---|
| `id` | Unique slug, kebab-case |
| `type` | `payees` \| `banks` \| `categories` \| `mixed` |
| `version` | Integer, bumped on any change to the pack |
| `name` / `description` | Localized strings (`en` required, `ru` optional) |
| `entity_count` | Total entities in the pack |
| `pack_url` | Path to the versioned `pack.json` (relative to `base_url`) |
| `preview_logos` | Up to 4 logo paths shown in the marketplace before installing |

## Pack versioning

- `pack.json` lives at `packs/{id}/v{n}/pack.json` — the path includes the version so it is immutable once published
- Logos live at `packs/{id}/logos/{key}.png` — stable paths, not versioned (logo files are updated in-place)
- When a pack gains new entities or fixes, bump `version` in both `catalog.json` and the new `v{n}/pack.json`

## Logo specs

- Format: PNG with transparency
- Size: 256×256 px recommended (app will render at 40–80dp)
- Name: matches the payee/bank `key` field, e.g. `pyaterochka.png`

## Adding a new pack

1. Create `packs/{id}/logos/` and add PNG logos
2. Create `packs/{id}/v1/pack.json` following the schema in `schema/pack.schema.json`
3. Add an entry to `catalog.json` pointing to the new pack
4. Bump `generated_at` in `catalog.json`
5. Push to `main` — GitHub Pages deploys automatically

## Updating an existing pack

1. Add/update logos in `packs/{id}/logos/` (in-place, same filename)
2. Create `packs/{id}/v{n}/pack.json` (new version folder, do not edit old ones)
3. In `catalog.json`: increment `version` and update `pack_url` to `packs/{id}/v{n}/pack.json`
4. Push to `main`
