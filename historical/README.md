# Historical vintage import — 2023 / 2024

Back-loads a paper vintage far enough to answer three questions and survive an
audit: **how many gallons did we make, where did they go, what did they cost.**

It deliberately does *not* reconstruct the cellar. No vessels, tank assignments,
barrel placements, readings or pump-overs — those are 2025-forward, entered live.

---

## Setup

```powershell
copy historical\_TEMPLATE\*.csv historical\2023\
copy historical\_TEMPLATE\*.csv historical\2024\
```

Then delete the example rows from your copies. Lines starting with `#` are comments
and are skipped by the importer — leave them in as a field reference.

Missing files are simply skipped, so a vintage with no bulk sales needs no
`06_removals.csv`.

---

## The seven files, and why they are in this order

| File | What it writes | Notes |
|---|---|---|
| `00_opening_inventory.csv` | carry-in lots | wine in bond before the vintage starts |
| `01_spirit_receipts.csv` | `HighProofSpiritLedger` receipts | **must** precede any fortification |
| `02_fruit.csv` | harvest → weigh tag → lot → allocation | one row per tag; several rows may feed one lot |
| `03_production.csv` | gallons + tax class, plus per-lot oak $ | `book_to_bond` or `fortify_*` |
| `04_blends.csv` | `LotLineage` edges | drives composition % *and* inherited cost |
| `05_bottling.csv` | bottling runs + dry goods | |
| `06_removals.csv` | taxpaid, bulk, in-bond, must, adjustments | |

The order is enforced. `FortificationEvent.save()` refuses a draw larger than the
HPGS balance on hand, so a fortification keyed before its spirit receipt fails —
which is the correct behaviour, and the reason receipts are file 01.

---

## Running it

```powershell
python manage.py import_historical historical\2023                          # dry run
python manage.py import_historical historical\2023 --yes
python manage.py import_historical historical\2023 --yes --overhead-pool 41500
```

Dry run by default. Every file is parsed and cross-checked **before** anything is
written — a partial write into an append-only ledger is expensive to undo, since
voided rows stay visible forever.

**Re-running is safe.** Every writer keys on a natural identity (lot code, weigh-tag
number, sku + date, fortification date + PG) and skips what already exists. Fix the
CSV and re-run; do not hand-unpick the ledger.

`--overhead-pool` takes one dollar figure for the whole vintage and spreads it
across the imported lots **by gallons produced**, booked as
`LotCostAdjustment(basis=allocated)` so it stays distinguishable from a per-lot
figure you typed. A 3,000-gallon lot did not consume the same cellar overhead as a
200-gallon one.

---

## Things worth knowing before you key 96 lots

**Lot codes are forced.** `lot_code` is the code on the paper — `23TR2` — and that
is what the system will show. This bypasses `LotSequenceCounter` on purpose: a 2023
lot must not consume a 2025 sequence number. The code must start with the two-digit
form of its `vintage_year`; a mismatch is a hard error, because it means one of the
two columns is a typo and guessing which produces a lot findable by neither.

**Carry-in wine becomes real lots**, not an inventory adjustment. It has to: 2022
wine bottled in 2023 needs something for the `BottlingRun` to point at, and every
removal model FKs a `Lot` or a `BottlingRun`.

**Blends are written as raw `LotLineage` edges**, not through
`services.blending.blend()`. The blend service enforces live-workflow invariants —
vessel co-occupancy, source balance, the tax-class matrix — against state this
import never creates. The edge is what costing and composition actually read. This
is the one place the importer goes around a service, and it is on purpose.

**Blends do not book to bond.** Blending moves wine already in bond; it does not
*produce* wine for 5120.17 purposes. The child gets a stated `VolumeMeasurement`,
which is what costing and composition read.

**Vineyards and blocks are matched, not minted.** If a name does not match your
curated master data the importer creates it and *warns*, because a `Block` carries a
variety and participates in `VarietalDesignation` resolution (block > vineyard >
variety). Read the warnings — a second "Mohr-Fry Ranches" under a different spelling
forks the abbreviation catalog.

**Cost that has no ledger home lands in `LotCostAdjustment`.** On a live 2025 lot,
barrel cost comes from `AgingPlacement` custody intervals and additives from
`Addition` rows. Neither exists for an imported vintage, so both would silently come
back `$0`. Oak (per lot, from `03_production.csv`) and overhead (allocated) are
booked as dated, signed, append-only rows instead. They show on the lot Cost panel as
**Assigned costs** — an auditor can tell a measured cost from one somebody typed.

---

## Protecting the import from a reset

`reset_transactional` now takes `--keep-vintages`:

```powershell
python manage.py reset_transactional --yes --keep-vintages 2023,2024
```

It protects lots by `vintage_year` and then walks **outward** through every FK path,
so a kept lot keeps its weigh tags, harvest events, bond bookings, blends, bottling
runs, removals and cost rows — not just the `Lot` row. Models with no path to a lot
(spirit ledger, material transactions, sequence counters, daily plans) are scoped by
their own year field, and the spirit ledger is widened to include the draw behind any
kept fortification, whose FK is `PROTECT`.

A transactional model that isn't covered by either rule raises `CommandError` rather
than being silently wiped. If you add a model and this command starts refusing to
run, that is the guard working — add it to `_lot_paths()` or `_DATE_SCOPED`.

`ExternalDestination` also moved into the always-keep set. Its own docstring calls it
reference data, every removal FKs it with `PROTECT`, and re-typing the buyer list
after each reset was pure friction.
