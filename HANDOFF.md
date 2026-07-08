# St. Amant Cellar ERP — Project Handoff / Context

This document carries the full context needed to continue this project in a new
conversation. Read it first. The complete, deployable codebase is in
`stamant_cellar_COMPLETE.zip`.

---

## 1. What this is
A custom Django ERP for **St. Amant Winery** (Lodi, CA · BW-CA-5526 · EIN 94-2275571),
covering the full winemaking lifecycle: harvest → fermentation → crush-out (pressing,
fortification) → aging (barrels/oak) → bottling → tax-paid removal, with **true COGS** and
**TTB/CA compliance reporting**. Built collaboratively over a long prior conversation,
tranche by tranche, each validated against the winery's real data before shipping.

Specialties that shape the domain: **Portuguese/Iberian varieties**, a **fortified Port
program** (fortification is the key feature commercial tools like InnoVint/Vintrace don't
handle well — a main reason for building custom), and Heritage Lodi wines. ~250 tons/yr.

## 2. Who the user is (Nate)
General Manager of St. Amant. **Technically capable** (Python, VBA, Django, API experience).
Direct, momentum-oriented, prefers "proceed and self-correct" over long planning. Provides
inputs in batches, reacts best to concrete artifacts. Wants correctness on compliance and
appreciates being told when something is wrong (including errors in his own filings). Uses
Commerce7 (DtC) and QBO (accounting) — SKUs match across systems. Vineyard partners:
**Mohr-Fry Ranches** (Lodi, District 11, Heritage) and **Spencer Ranch** (Amador, District 10,
Iberian; family connection via Tim Spencer).

## 3. Architecture & stack
- **Django 5.1** project. App = `cellar`. Project config = `config/` package.
- **All business logic lives in `cellar/services/`** (pure Python functions on the models) —
  this is deliberate: it's the seam that lets an API + web + iOS client sit on top without
  rewriting logic. UI has always been scoped as a later layer on top of the finished model.
- **Data-entry UI is currently the Django admin** (a build/test scaffold, NOT the final UX).
- **Append-only ledger**: most event models inherit `AppendOnly` (insert-only; can't edit or
  delete; correct mistakes by the "Void selected" admin action + a new row). Temporal models
  have `CLOSE_FIELDS` (e.g. `emptied_at`, `removed_at`) that can be set after creation.
- Deploy target: **Heroku** (Basic dynos + Postgres Essential-0). Runs locally on SQLite,
  Heroku on Postgres, switched by env vars only. Public site stays on **Kinsta** (separate).

## 4. What's built (complete inventory)

### Models (`cellar/models/`)
- **base.py** — `AppendOnly` (insert-only + `voided_at` + `CLOSE_FIELDS`), enums.
- **reference.py** — Variety, Grower, Vineyard(+`crush_district`), Block, VarietalDesignation,
  Vessel, Additive(+`unit_cost`), LabAnalyte, ConfigConstant, LotSequenceCounter.
- **spine.py** — HarvestEvent, WeighTag(+bins, +`purchase_price_per_ton`, +`fruit_cost_per_ton`),
  WeighTagBin, Lot, LotDesignation, WeighTagAllocation, LotLineage.
- **spirits.py** — HighProofSpiritLedger (WG + PG + `cost`; blended proof/cost aggregates).
- **ledger.py** — Reading, Addition(+`quantity`/`cost`).
- **fermentation.py** — Destemming, TankAssignment, ColdSoak, PumpOver, PunchDown, Inoculation,
  LabRequest, LabResult(+values), CellarNote.
- **crushout.py** — TaxClass, VolumeMeasurement, PressingEvent, FortificationEvent(+`spirit_cost`),
  BookToBond.
- **aging.py** — Room, Location, BarrelOrder, Container, Rack, RackAssignment, AgingPlacement,
  VolumeLoss, ToppingEvent, ToppingTarget.
- **bottling.py** — BottleFormat, DryGood, BottlingRun, BottlingDryGoodUse, TaxPaidRemoval.
- **reporting.py** — BondTransfer, Material, MaterialTransaction(received/used/**destroyed**),
  SweeteningEvent, BondAdjustment, BulkTaxPaidRemoval.

### Services (`cellar/services/`)
- **generator.py** — lot-ID resolver (block→vineyard→variety precedence), atomic sequence
  counter, code renderer, `create_lot`, `redesignate`.
- **aging.py** — `oak_summary`/`oak_detail`, `composition_of`/`composition_report` (leaf-lot
  resolution — the label-compliance record), `plan_batch_location` (split/mismatch flags).
- **costing.py** — barrel depreciation via **custody intervals** (50/33/17 over 3 years),
  five-stream COGS rollup (`lot_cost`, `lot_cost_per_gal`, `bottling_cogs`).
- **reporting.py** — 5120.17 read-layer: `build_5120_17` (Part I), `build_5120_17_part3`
  (spirits, PG), `build_5120_17_part4` (materials), `lot_tax_class`.
- **excise.py** — CBMA engine: `excise_on_removals`, `compute_period_excise` (rates + tiered
  credit + annual cap, bottled + bulk removals).
- **backsweeten.py** — `backsweeten` (concentrate volume for target RS), `brix_to_sugar_gL`.
- **crush_report.py** — `ca_crush_report` (by district × variety), `crush_report_totals`.
- **forms.py** — fill fileable PDFs: `render_5120_17_pdf` (pypdf), `render_5000_24_pdf`
  (**pdftk** — these PDFs trip pypdf), `render_crush_report_pdf`/`crush_report_csv` (reportlab).

## 5. Key locked design decisions
- **Barrel depreciation:** 50/33/17 over 3 barrel-years, then $0; age-based; allocated to
  wines by **custody intervals** (fill-to-next-fill so turnaround/trailing idle → departing
  wine; leading idle → first wine of the year; whole-empty year → overhead). Rates/life are
  config. GAAP-defensible; confirm salvage/useful-life with CPA. (Book curve ≠ MACRS by
  design; ERP can output both — tax reconciliation stays the CPA's job.)
- **Fortification:** booked at volume-determination; tax class from the **target**, not lab
  ABV; base wine backed out (T − spirit WG); spirit drawn from HPGS account in proof gallons.
- **Composition** resolves to **leaf lots** (varietal/appellation label record); refuses to
  compute without a recorded produced volume (won't guess).
- **Topping:** always from a tracked lot; routine books evaporative loss, partial-fill books
  none; foreign wine >5 gal cumulative flags the barrel until rack-out.
- **Rounding (TTB):** gallons kept full-precision through computation; rounded once at the
  report boundary to the nearest tenth (27 CFR 24.281); round the summary, not each row.
- **CBMA credit:** single annual pool across classes — $1.00/gal first 30k, $0.90 next 100k,
  $0.535 next 620k, $0 beyond 750k; applied in order of removal (max annual credit $451,700).
- **Location** lives on the **rack only** (a rack is one physical place); barrels inherit;
  batch-coding a lot to a location flags split racks / location mismatches for verification.

## 6. Compliance findings (surfaced from real filings — TELL HIS FILER)
Reconstructing 2025 from his filed reports revealed two issues (endings all reconcile;
these are classification/rate issues, not balance errors):
1. **5120.17 base-wine classification:** his filings book the Port *base* wine in col (b)
   (16–21%). The base is under 16% until fortified, so it belongs in **col (a)**. Endings are
   unaffected (base nets to zero in its column), but the ERP produces the correct col (a)
   treatment. Likely immaterial (no tax effect) — his call whether to amend.
2. **Excise rate on Port (MORE IMPORTANT — affects tax owed):** his 5000.24 taxes Port at the
   ≤16% rate ($1.07) instead of the 16–21% rate ($1.57), an internal inconsistency with his
   own 5120.17. ~$118 underpayment across 2025 (Q2 filed $419.62 vs correct $483.02; Q4 filed
   $490 vs correct $544.63). Small dollars but it's federal excise — his filer should review.
   *(Assistant is not a tax advisor; presented as findings to verify, not certainties.)*

## 7. Validation status
- **Fortification:** validated against his real 2025 Tempranillo Port lot (base 648, finished
  741, spirit 93 WG, exact 5120.17 lines).
- **5120.17 read-layer:** validated against real **August** (bottling + transfer) and
  **October** (fermentation + fortification) — bulk accounts reconcile to the tenth; full 2025
  carry-forward chain ties every month.
- **COGS, oak, composition, topping, CBMA, back-sweetening, crush report:** validated with
  constructed cases mirroring his operations (barrel/bottling had no real data yet).
- **Not yet done:** a full real-month event re-entry (enter one month exactly as worked, from
  scratch, and confirm the 5120.17 still ties) — the strongest final proof before live filing.
  Also Parts III/IV validated logically, not yet against filed Part III/IV figures.

## 8. Deploy status (just completed)
`config/` scaffold is Heroku-ready and **tested**: `check --deploy` clean, migrations apply,
WhiteNoise collectstatic works, gunicorn serves, host-allowlist + SSL-redirect verified.
`rest_framework` + `corsheaders` already installed/configured for the API layer. See
`DEPLOY.md` in the package. Runs on his current Basic dynos + Postgres Essential-0 (upgrade
Postgres for backups, not size, before it's the system of record).

## 9. What's next (roadmap — was mid-discussion when we stopped)
The user wants to build the **front end**, scaling eventually to an **iOS app** + access from
any PC. Agreed architecture: **API-first (Django REST Framework)** — headless backend on
Heroku, thin clients (web first, iOS later) consuming the same JSON API. Next build = the
**DRF API layer** exposing the existing services as endpoints.

**OPEN QUESTION being discussed when we stopped: authentication approach.** Options laid out:
self-hosted hybrid (session auth for web + token for iOS, cheap, he owns the risk) vs.
outsourced identity (Auth0/Clerk/Cognito/Sign-in-with-Apple — more robust for compliance
data, small cost, less to secure himself). Was leaning toward deciding based on (a) user
count — few staff vs. eventual customers, and (b) how much he wants to own security. **Pick up
here: get his auth decision, then design and build the DRF API layer.**

Later: web front-end client (React SPA if iOS is firm, or Django+HTMX if iOS is "maybe"),
then iOS (native Swift for offline + barcode scanning, or cross-platform). Barcode fields
already exist on barrels and racks for a future scan-to-move flow.

Also outstanding: CDFA Crush Report *fillable* form (he'd need to provide the template to map
it like the others); 5000.24/Crush form-fill exists but a real-month re-entry validation is
pending; the two compliance findings to his filer.

## 10. Install / run (fresh)
Unzip `stamant_cellar_COMPLETE.zip` at a project root. Then:
```bash
pip install -r requirements.txt          # (use --break-system-packages if needed)
python manage.py migrate                 # SQLite locally
python manage.py createsuperuser
python manage.py runserver               # /admin/
```
For Heroku: follow `DEPLOY.md`. External tool dependency: **pdftk** (for 5000.24 fill).

## 11. Conventions / gotchas
- Business logic goes in `services/`, never in views/admin — keep the API-ready seam.
- Append-only models: never edit/delete; **void + re-add**. Aggregates exclude `voided_at`.
- Only `create_lot`/`redesignate` mint lot sequence numbers (concurrency-safe).
- COGS/composition walk the genealogy on read — fine at his scale; cache if ever slow.
- These excise PDFs break pypdf's form reader → 5000.24 fill uses pdftk.
- `estate_fruit_cost_per_ton` is a ConfigConstant (fruit COGS for estate fruit).
- Reports stream on demand (Heroku's filesystem is ephemeral — no S3 needed yet).
