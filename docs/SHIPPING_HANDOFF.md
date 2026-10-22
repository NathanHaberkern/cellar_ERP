# Shipping app — session handoff

As of **Oct 22, 2026** — Tranches 1, 2 and 4 built; UPS adapter (first half of 5) built. Read this, then `HANDOFF.md` (whole-ERP context), then the
build plan: https://claude.ai/artifact/NSpwCYVU7tNbdcfh7XFA4k

## 1. What this is

A `shipping` Django app inside cellar_ERP that replaces the Commerce7 **Shipment
Manager** subscription:

> C7 orders (compliance-passed) → address validation → carrier routing → pack pools
> → box fit + weight → buy FedEx/UPS label → print 4x6 PDF → post tracking to C7
> (C7 sends the ship email).

~1,200 packages/yr. Direct carrier APIs (FedEx + UPS), **not** Shippo — decided on
cost (~$200–250/yr saved) and so labels, tracking and freight cost live in our DB.

## 2. Status

| Tranche | Scope | State |
|---|---|---|
| 1 | App skeleton, models, users/permissions page, FedEx adapter, box seed | **Built.** Sandbox label printed (2-Day, no alcohol). |
| 2 | C7 order sync, FedEx address validation + review queue, carrier routing, pooling, box fit | **Done.** Exit gate passed Oct 21 (replay). |
| 3 | FedEx label certification | Blocked on FedEx (below). |
| 4 | Pool page: buy labels, print 4x6 PDFs, void, post tracking to C7 | **Built Oct 22** (271 tests). Needs a live test. |
| 5 | UPS adapter ✓ built Oct 22, CIE test label verified Oct 22 (prints upright, 4x6); daily tracking sync + delivery-exception queue (both carriers) — not started | Half |
| — | GLS adapter + pickups + QBO wholesale (GLS) order intake + tracking to QBO | **Built Oct 22**; QBO sandbox verified; waits on GLS credentials |
| 6 | Later: freight cost → QBO export, ZPL direct print, hold-at-location (same-state only) | — |

### Tranche 1 exit gate — partly met
- `fedex_check` → 200 OK (sandbox credentials good).
- `fedex_test_label --service FEDEX_2_DAY --no-alcohol` → **label printed.**
- Full gate (Ground + ALCOHOL/CONSUMER + adult signature) is **blocked by FedEx**: the
  sandbox *test account* lacks both services. FedEx accepted our request format.
  - `CUSTOMER.ALCOHOLSPECIALSERVICETYPE.INELIGIBLE` — txn `f2dbd761-f0bc-4ff3-80cc-924f8f1d1a34`
  - `GROUND.SHIPPING.NOTAUTHORIZED` — txn `7ce092f6-9a9d-4383-8520-b840caabd440`
  - **Nate's action:** call FedEx Web Services 1-800-463-3339 (or rep Oscar Martinez) to
    enable Alcohol + Ground/Home Delivery on the sandbox test account, and confirm the real
    account (ending **2488**) has the alcohol agreement and API shipping enabled.
  - Then: `python manage.py fedex_test_label --box "4 Bottle" --bottles 4` (Ground + alcohol
    by default) = the certification label.

### Tranche 2 — what was built and decided
- **C7 fields confirmed** by `c7_probe` on the live tenant (Oct 21): `complianceStatus`
  (Compliant / Not Checked / Quarantined seen), order-level `club` {clubTitle,
  clubPackageTitle, shipmentBuildStatus}, `items[].volumeInML` + `weight`,
  `selectedShippingOptions.requestedShipDate`, `shipTo.stateCode/zipCode/address/address2`.
  `shipping[].carrier` is just C7's shipping-method label ("UPS Ground") — ignored.
  The C7 app has NO club-membership route permission; not needed (club is on the order).
- **Compliance gate** `SHIP_COMPLIANCE_GATE=off` (2026): all orders flow, status shown.
  Nate plans `on` for 2027: only "Compliant" pools; everything else (incl. Forced) held
  until released with a note by someone with `shipping.override_compliance`.
- **Requested ship date** = computed hold until that date. `services/todo.daily_items()`
  produces Daily-checklist items (due/late ships, address reviews, open pools); **not yet
  wired** into `cellar/services/daily_plan.draft_items` — one line, Nate said "eventually".
- **Combining:** same recipient name + exact same address (normalized: case, St/Street,
  ZIP+4) → one shipment, boxes combined. Same address, different name → separate, same pool.
- **UPS Store:** routed UPS when "UPS Store" or a "PMB" line appears in name/company/street;
  plus a per-order "Ship via" override (Auto/FedEx/UPS) — FedEx never forced into a
  restricted state. (#60869 was a UPS Store with no store name.)
- **Exit gate passed (Oct 21):** replay of 270 Fall club orders — boxes 260/269, the 9 "misses"
  were C7 tracking-number mix-ups (other orders' tracking pasted in); combining all match;
  carrier 1 miss (#60869, UPS Store → fixed above). Default pool at 29 cases is OK (no split).
- **Box fit:** magnum alone; "Port Club Volume" items in Port box; rest fills 12s then closest.
- **Pools:** carrier first; Default pool = exactly 1 each of `SHIP_DEFAULT_CLUB_SKUS`;
  rest sorted by box/SKU signature, split evenly near 20 **12-bottle cases** (~240 bottles,
  216-264 ok; confirmed by Nate). Address units never split across pools.
- **Bottle size** order of trust: SkuShippingProfile → C7 volumeInML → ERP BottleFormat → hold.
- **Address validation:** FedEx `/address/v1/addresses/resolve`. Formatting-only changes
  pass; real changes / unresolved / suite missing → review queue (Use FedEx / Keep / Edit).
  Response parsing written from FedEx docs + defensive defaults — **verify on first live
  pull** (sandbox may return canned data).
- **Exit gate:** `python manage.py shipping_replay` re-packs fulfilled Club orders since
  Sep 1 (read-only), compares box counts to C7 tracking numbers, combining (shared
  tracking) and carrier; prints proposed pools → `label_output/shipping_replay.txt`.
- **UI:** `/shipping/` (nav "Shipping" for sync/build perms): Pull orders, address review,
  held, ready + residential override, pool preview, Build pools, open pools, Dissolve.
- New perm `override_compliance` (in Shipping Admin defaults for NEW installs only — existing
  groups unchanged; Nate is superuser).

### Tranche 4 — what was built and decided (Oct 22)
- **Pool page** `/shipping/pools/<id>/` (linked from "Open pools"): boxes in packing
  order with contents, weight, ship-to, label; **Buy N labels** (one carrier call per
  box, each saved as it comes back — a refusal on one box doesn't stop the rest; press
  again to buy only the missing ones), per-box **Buy / Print / Void**, **Print all**
  (one merged PDF in box order), **Post tracking to C7**, per-order **Post** retry.
- **Statuses are recomputed from labels** (`services/shipments.refresh_pool`): pool
  OPEN → LABELED (every box labeled) → SHIPPED (every order fulfilled); order POOLED →
  LABELED → FULFILLED. A void puts exactly the affected orders back.
- **Label request** per box: ship-to = first order's (combined orders share the
  address), box dims + declared weight, reference = order numbers ("60812/60813"),
  FedEx Home Delivery if residential else Ground; UPS Ground. Alcohol + adult
  signature always. Recipient phone falls back to SHIP_FROM_PHONE when C7 has none;
  **no recipient email** sent to the carrier (C7 sends the ship email).
- **Refused before calling the carrier:** no SHIP_FROM_PHONE; any order in the pool
  changed in C7 after pooling (dissolve + rebuild).
- **C7 writeback** (`services/fulfillment.py`): `POST order/{id}/fulfillment`,
  `type: Shipped`, `sendTransactionEmail: true`, `shipped.carrier` + `trackingNumbers`
  (every box the order is in), `packageCount`, `items: [{orderItemId, quantity}]`
  from the synced order's lines (`hasShipping` ones). A combined box posts the same
  tracking to each order. **Never posts SANDBOX labels.** `SHIP_C7_WRITEBACK=off` =
  buy real labels without touching C7. No double posts: an already-succeeded post is
  skipped; after a timeout/5xx the retry first GETs the order and only posts if C7
  doesn't already have the tracking. Every attempt is a `FulfillmentPost` row.
  Carrier names sent to C7: `SHIP_C7_CARRIER_FEDEX` / `_UPS` (default "FedEx"/"UPS") —
  match whatever Shipment Manager wrote (the replay output lists them).
- **Void refused once posted to C7** — the customer already has that number. Delete
  the fulfillment in C7 by hand first, then void here.
- **Permissions:** pages + buy/print/post = `print_labels` (Packer has it); Void =
  `void_labels` (Shipping Admin). Nav "Shipping" now also shows for print-only users.
- Daily to-do gains "Post tracking to C7 for <pool>" for LABELED pools.
- **Verify on the first live post** (low-volume test): the request body above was
  written from C7's docs (`orderItemId` = the order line's `id`); the read-back shape
  (`fulfillments[].shipped.trackingNumbers`) is the one the replay already reads.

### UPS adapter (Oct 22)
- `shipping/carriers/ups.py`, written from UPS's published OpenAPI spec (Shipping
  v2409). OAuth client-credentials (HTTP Basic + `x-merchant-id`), ship, void.
  CIE = `UPS_ENV=sandbox`.
- Alcohol = shipment-level `RestrictedArticles.AlcoholicBeveragesIndicator` (needs the
  UPS alcohol contract on the account); adult signature = package
  `DeliveryConfirmation.DCISType 3`; negotiated rate preferred over published.
- UPS has no PDF labels: we ask for **GIF and convert to a 4x6 PDF** (crop the doc
  tab, rotate to portrait). Verified Oct 22 on the first CIE label: formatted
  correctly, so `GIF_ROTATE = -90` is right.
- `python manage.py ups_test_label --check` (token only), then `ups_test_label`
  (1 Bottle, alcohol + adult sig), `--box "4 Bottle" --bottles 4 --home`,
  `--no-alcohol`, `--void 1Z...`. Refuses unless UPS_ENV=sandbox.
- UPS developer app keys are in Nate's `.env` (Oct 22). Still to confirm before going
  live: the production UPS account has the UPS alcohol shipping agreement.

### GLS + QuickBooks wholesale orders (Oct 22)
- **Why:** wholesale orders live in QBO, not C7. An Invoice or Sales Receipt whose
  **Ship via** is GLS (`SHIP_QBO_GLS_SHIP_METHODS`, default `GLS,GLS US,GSO`) and has
  no tracking number is pulled by **Pull QuickBooks (GLS)** on the Shipping page.
  As of Oct 22 no QBO document uses a GLS ship method yet (seen: CUR, SAW) — Nate
  needs a "GLS" entry in QBO's Shipping methods list.
- Becomes a normal ShipOrder (`source=qbo`, key `qbo:Invoice:<Id>`, number = DocNumber):
  same address check, holds, pools, labels, print page. Always GLS
  (`carrier_override="gls"`, can't be changed here; GLS-only pools). Compliance gate
  doesn't apply (wholesale). C7 sync never touches QBO orders.
- **Our QBO data:** ShipAddr is usually free-form (Line1..5, last line "City, ST  ZIP",
  name/attention first) — parsed in `qbo_intake.parse_address`; unreadable → address
  review. Lines are **cases**: bottles = Qty × 12 (`SHIP_QBO_BOTTLES_PER_CASE`) unless
  the description says "12 x 500 ml"; half cases count bottles. Only income account
  `40100…` lines are boxed (CRV/fees skipped, `SHIP_QBO_SHIP_ACCOUNTS`). Bottle size:
  SkuShippingProfile/bottling run → same SKU's C7 size → 750 ml.
- **Tracking back:** "Post tracking" on a GLS pool writes `TrackingNum` (comma-separated,
  31-char QBO limit) + `ShipDate` onto the invoice (sparse update, fresh SyncToken).
  Never for sandbox labels; `SHIP_QBO_WRITEBACK=off` to skip. A QBO doc that later shows
  a tracking number (or loses GLS, or is deleted) is closed/flagged on the next pull.
- **QBO client** `integrations/qbo.py` (shared, like commerce7.py) + app `integrations`
  with `QBOConnection` (the one DB row holding the rotating refresh token — Intuit
  replaces it on every refresh, so it can't live in env). Connect once at
  `/integrations/qbo/` (superuser): Intuit consent → callback saves tokens. Disconnect
  there revokes the tokens at Intuit. 429/5xx retried with backoff (2s, 5s, 15s); every
  error carries Intuit's `intuit_tid`. **Sandbox connect + pull verified by Nate 2026-10-22.**
- **GLS adapter** `shipping/carriers/gls.py` — written to GLS's ShipmentHub API v2
  (https://shipmenthub.gls-us.com, Postman collection, read Oct 22); not yet run
  with real credentials. `https://ship.gls-us.com/v2`: GET /Token (12 h), POST/DELETE
  /Shipment, POST /Pickup. Service CPS (GLS Ground), ADULT_SIG_REQD, label
  PDF_4x5LABEL (base64 `PaperLabel`), weight rounded up to whole lb.
  **No GLS test server**: GLS_ENV=sandbox calls the live API but marks labels as tests
  (never written to QBO; GLS bills only on scan — void them anyway) and refuses pickups.
  `gls_test_label --check`, then `gls_test_label` (12 Bottle to a Lodi test business).
- **GLS pickups** (`CarrierPickup`, `shipments.request_pickup` / `pickup_options`): Nate
  wants a pickup only when there are GLS boxes, never a standing one. On a labeled GLS
  pool (production only): **Request GLS pickup (now–3 PM)**. GLS requires a 4-hour
  window (`GLS_PICKUP_MIN_WINDOW_MIN`=240), so after ~11 AM, or on a weekend, the page asks
  "Too late for 3 PM" and offers **Push to 4 PM today** (until ~noon) or the
  **next business day** 9 AM–3 PM. One pickup covers every GLS box labeled since the
  last pickup; a second press shows the booked one. Every request carries Nate's note
  "We close at 3PM. If late, please call 661-808-3988." (`GLS_PICKUP_*` in .env).
  GLS's docs don't name the confirmation field (table cut off) — `_find` tries likely
  names; check on first use.
- **Nate's actions:** (1) GLS API user/password + account number →
  `GLS_ACCOUNT_NUMBER/USERNAME/PASSWORD`, then `gls_test_label --check`;
  (2) ~~Intuit developer app + sandbox connect~~ done 2026-10-22; production keys need
  the app hosted on https (App URLs, redirect URI) plus public EULA/privacy pages; (3) add "GLS" to
  QBO Shipping methods and use it on wholesale invoices.

## 3. Files (all new unless noted)

```
shipping/
  apps.py            post_migrate → ensure_default_groups
  permissions.py     5 custom perms + "Packer" / "Shipping Admin" groups (created only if missing)
  models.py          BoxSize, BottleWeight, SkuShippingProfile, ShipOrder(+Line), PackPool,
                     Package, Label (AppendOnly), LabelDocument (immutable), FulfillmentPost (AppendOnly)
  admin.py           reference data editable; Label/FulfillmentPost read-only
  carriers/base.py   neutral dataclasses (Address, Parcel, LabelRequest, LabelResult), CarrierError
  carriers/fedex.py  OAuth (cached), ship, cancel; alcohol at PACKAGE level
  carriers/registry.py  get_adapter(); account number normalized to digits
  services/labels.py buy_label / void_label (carrier call first, then DB)
  management/commands/fedex_test_label.py  --box --bottles --ml --home --service --no-alcohol --void
  management/commands/fedex_check.py       credential diagnostic, never prints values
  migrations/0001_initial.py, 0002_seed_boxes_and_bottle_weights.py
  tests/  fakes.py, test_fedex.py, test_models_and_labels.py, test_users_page.py
cellar/web/users.py + templates/web/users.html, user_form.html   Users page (needs auth.change_user)
T2: integrations/commerce7.py (shared C7 client), shipping/services/{packing,intake,pools,todo}.py,
    shipping/views.py, urls.py, templates/shipping/*, migrations/0003, management/commands/
    {c7_check,c7_probe,shipping_pull,shipping_replay}.py, tests/{test_c7_probe,test_packing,
    test_intake_and_pools}.py; MODIFIED carriers/{base,fedex,__init__}.py, models.py,
    permissions.py, config/urls.py, base.html nav
T4/T5: shipping/services/{shipments,fulfillment}.py, carriers/ups.py, templates/shipping/pool.html,
    management/commands/ups_test_label.py, tests/{test_ups,test_labels_and_fulfillment}.py;
    MODIFIED views.py, urls.py, carriers/registry.py, services/todo.py, index.html, settings
    (SHIP_C7_WRITEBACK, SHIP_C7_CARRIER_*), .env.example
MODIFIED: config/settings.py (INSTALLED_APPS + SHIPPING block), cellar/web/urls.py (users routes),
          cellar/web/templates/web/base.html (Users nav link), requirements.txt (+requests),
          .env.example (shipping keys), .gitignore (+label_output/)
```

## 4. Settled decisions

- **Credentials only in env** (`.env` locally, Heroku config vars). No carrier-account table
  (dropped from the plan in T1 to avoid duplicating secrets). Keys: `FEDEX_ENV`,
  `FEDEX_CLIENT_ID` (= API Key), `FEDEX_CLIENT_SECRET` (= Secret Key), `FEDEX_ACCOUNT_NUMBER`,
  optional `FEDEX_ALCOHOL_SHIPPER_AGREEMENT`; `UPS_ENV/CLIENT_ID/CLIENT_SECRET/ACCOUNT_NUMBER`;
  `C7_APP_ID/APP_SECRET/TENANT_ID`; `SHIP_FROM_PHONE` (required) + optional `SHIP_FROM_*`.
- Sandbox uses FedEx's **test account number** from the project page, not the real account.
- **One C7 app** for the whole ERP (shared client module, not inside `shipping/`).
- **VinoCheck is the compliance gate.** App pulls only orders C7 marks compliance-passed and
  never re-decides compliance. Optional opt-out backstop: licensed-states list.
- **Label:** 4x6 PDF via browser print. FedEx Ground / Home Delivery, UPS Ground. No rate shopping.
  Residential → Home Delivery; business/unknown → Ground.
- **Every label:** ALCOHOL (`alcoholRecipientType: CONSUMER`) + `SIGNATURE_OPTION` ADULT.
- **Dimensions** rounded UP to whole inches; weight to 0.1 lb.
- **Users:** individual logins; roles = Django groups; direct per-user shipping perms allowed;
  can't deactivate yourself; superuser only via /admin/.
- **Club tag** on orders (from C7 club data); one pooling rule for all orders.
- **Customer email:** `sendTransactionEmail` on (C7 sends ship confirmation).
- **Pickup:** existing FedEx/UPS scheduled pickup; no pickup API.
- **Cutover:** test in a low-volume period; then Nate cancels Shipment Manager.
- **Address validation** (FedEx) in T2; **tracking sync** in T5; **hold-at-location** T6.

### Boxes & weights (seeded, editable in admin)
| Box | Use | in | tare lb | max btl | max lb |
|---|---|---|---|---|---|
| 1 / 2 / 3 / 4 / 6 / 12 Bottle | table & port | 15.5×5×5.5 / 15×10×5.5 / 20×12.75×6.5 / 20×15×5.5 / 14×9.75×14 / 18.5×14×14.5 | 0.6/1.1/1.9/1.9/2.3/4.9 | 1/2/3/4/6/12 | 4/8/12/17/22/42 |
| Port | **only** line items named "Port Club Volume XX" | 16×13×4.5 | 2.0 | 1 | 8 |
| Magnum | magnums only | 6×6×18 | 0.7 | 1 | 10 |

Bottle weights: 750 ml 3.0 lb · 500 ml port 2.16 lb · magnum (1.5 L) 7.5 lb.
Declared weight = tare + bottles, capped at box max. Bottle size from the SKU's
`BottleFormat` (via `BottlingRun.sku`), overridable per SKU in `SkuShippingProfile`.
A magnum ships alone in its Magnum box; the rest of the order boxes normally (2 labels).

### Packing rules for Tranche 2 (Nate's current practice)
- Pools of ~20 cases (18–22 OK) so he can pack and error-check as he goes.
- Group by **carrier first**, then SKU efficiency. Closest-fit box, fewest packages.
- **FedEx-restricted → UPS:** WV, VT, SC, OR, ND, NH, MO, MD, ME, IN, ID, CO (segregated pool).
- FedEx shipments **to a UPS Store** get flagged and moved to UPS. UPS → UPS Store is fine.
- Same customer/address, multiple orders → **one box**, same pool.
- Same address, **different names** → separate boxes, same pool.
- "Default" pool = identical default club orders (1× each 23RTEMP, 24SOUZ, 24IRED, 24TN), all in
  one pool; other 4-bottle orders pool normally.

### To verify against the live C7 API in Tranche 2
- Field carrying VinoCheck **compliance status** on an order.
- Field carrying **club membership** (for the Club tag).
- Fulfillment write: `POST /order/{id}/fulfillment` (`type: Shipped`, `shipped.trackingNumbers`,
  `carrier`, items by SKU, `sendTransactionEmail`).

## 5. How to work with Nate (and this setup)

- Follow the Cellar ERP project memory (ways-of-working): repo-first, batched questions with defaults, flag scope
  changes, rationale in docstrings, `makemigrations --check` + `check` + full tests per tranche.
- **Repo:** local folder `C:\Users\natha\OneDrive - St. Amant Winery\01 - Operations\ERP Software
  Design\stamant_cellar` (connect it to the session). GitHub `NathanHaberkern/cellar_ERP`
  (session has read-only; Nate commits/pushes himself). Before building, compare the folder
  with GitHub HEAD.
- The device link moves files only (no shell on Nate's laptop). Build + test in the cloud
  clone, then write files into the folder. The session CAN push to GitHub now (Claude
  GitHub app has write access as of Oct 21); push, then Nate runs `git fetch; git reset
  origin/main` so his git matches the files already written into his folder. **Gotcha:** re-staging a file at the same
  `/mnt/user-data/outputs/...` path can deliver a stale copy — stage each revision under a
  new filename and verify sizes with `device_list_dir` after writing.
- Nate runs commands in PowerShell 7 inside `.venv` (Python 3.12). Avoid one-liners with
  nested quotes — PowerShell mangles them; ship a management command instead.
- Nate's local `.env` has `ALLOW_HARD_DELETE=True` (testing phase); tests that check
  append-only guards must pin it off. Web tests pin `SECURE_SSL_REDIRECT=False` + plain
  static storage (see `test_users_page.py`).
- Never paste placeholder brackets in `.env` examples (`<key>` got copied literally once).

## 6. First steps for the next session

1. **FedEx:** sandbox alcohol + Ground still blocked (Nate to call FedEx, section 2) →
   Tranche 3 certification label → production keys. With production keys, also
   verify real address validation on a Pull (sandbox returned canned Chilean data).
2. **UPS:** keys in Nate's `.env`; CIE test label verified Oct 22. Next: production
   UPS keys (`UPS_ENV=production`) for the live test.
3. **Live test (cutover rehearsal), low-volume week:** production keys, Pull →
   Build pools → open a pool → Buy labels → Print all → pack → Post tracking to C7.
   Check in C7 that the order shows Fulfilled with the right carrier name and
   tracking, and that the customer email went out. Use `SHIP_C7_WRITEBACK=off` for a
   first dry run if Shipment Manager is still posting the same orders.
4. Then Tranche 5 second half: daily tracking sync (FedEx Track API, UPS Tracking
   API) + delivery-exception queue; then Nate cancels Shipment Manager.
5. Still one line to wire when Nate wants it: `shipping.services.todo.daily_items`
   into `cellar/services/daily_plan.draft_items`.
