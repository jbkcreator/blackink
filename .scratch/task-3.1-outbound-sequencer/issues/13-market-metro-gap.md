# Resolve the `market_metro` / `{city}` gap

Label: `wayfinder:grilling`
Status: closed
Assignee: —
Blocked by: —

## Resolution (2026-09-03) — decided by the Source of Truth, not by grilling

**Do not add `market_metro`. County is the unit. Remap `{city}` to county.**

> "The **county is the unit everywhere**: data, ranking, contract, and seat.
> **There is no metro layer.** National coverage means many counties, not a
> different geographic model." — Source of Truth §1.1
>
> "Replace metro-dependent fields/algorithms with county contracts. Preserve
> old `market_metro` schema text as **historical, not current authority**."
> — §2.1

So the schema was right and the merge tag was wrong. `companies.county_slug`
already exists; `county_allocations` already makes county first-class. The
`{city}` tag in `ALLOWED_TAGS` is a leftover from the superseded metro model
and its `companies.market_metro` source was never real.

**Actions:**

1. Repoint `ALLOWED_TAGS["city"]` at county. Consider renaming the tag to
   `{county}` — `{city}` is now actively misleading, and a county is not a
   city. Check existing templates before renaming.
2. County needs a **display form**. `county_slug` is a slug; copy needs
   "Hillsborough County", not `hillsborough`. Decide whether that is a
   derived function or a `counties` display-name column.
3. `validate_template` should reject the old tag rather than let it fail at
   `resolve_tags` during a send.

**Knock-on:** Touch 5's "Metro Speed Index" angle dies with the metro layer —
§2.3 replaces it with a "final **county**/public-observation angle". Touch 1's
"where you rank in Tampa" micro-ask becomes county-based. Neither is this
ticket's to fix; both are folded into ticket 19.

Also from §1.1: "Postal addresses and property-level filters remain useful
data, but must not silently recreate metro/ZIP-zone commercial exclusivity."
Do not reintroduce metro as a derived grouping.

Launch counties (§1.1): **Hillsborough and Pinellas** first, then Orange,
Duval, Polk, Pasco, Lee, Brevard, Volusia, Seminole. Miami-Dade, Broward and
Palm Beach are deliberately not first.

## Question

`ALLOWED_TAGS` in `src/services/outbound_templates.py` declares a `{city}`
merge tag sourced from `companies.market_metro`. **That column does not
exist** on the `Company` model. The tag cannot resolve.

`market_metro` is not incidental — the reference doc lists it as a contact/
company field the sequencer consumes (§2.1), Touch 1's Reply-YES micro-ask
is metro-specific ("where you rank in Tampa", §4.1), Touch 5's entire angle
is the **Metro Speed Index** (§6.1), and §2.3 requires metro benchmark
comparisons in the audit PDF.

`Company` does have `county_slug`. Decide:

- **Add a `market_metro` column** — then who populates it? The ingestion
  pipeline (`promotion_sweep`, `BaseIngestLoader`) would need to derive or
  receive it, and every already-promoted company would need backfilling.
  Is there a county → metro mapping, and is it one-to-one? (It is not:
  metros routinely span counties, and a county can sit outside any metro.)
- **Remap `{city}` to `county_slug`** — cheap, no migration, but "where you
  rank in Hillsborough" reads worse than "in Tampa", and a slug is not a
  display string.
- **Derive at render time** from a lookup table keyed on county.

Settle also:

- Is metro a property of the **company** or of the **county**? If county,
  it belongs on `counties`, not `companies`, and `county_allocations`
  already makes county a first-class concept.
- What is the **fallback** when a company has no metro? Touch 5's angle
  collapses without one — skip the contact, or degrade the copy?
- Does `validate_template` currently catch this? If `{city}` passes
  validation but fails at `resolve_tags`, the failure surfaces at send time
  — worth confirming which, since a send-time failure is much worse.

## Scope note

Small if remapped, migration-sized if added. The company-vs-county modelling
question is the interesting part.
