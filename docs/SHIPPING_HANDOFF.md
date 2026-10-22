# Shipping app — session handoff

As of **Oct 21, 2026 (evening)** — Tranche 2 built. Read this, then `HANDOFF.md` (whole-ERP context), then the
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
| 2 | C7 order sync, FedEx address validation + review queue, carrier routing, pooling, box fit | **Built** (236 tests pass). Exit gate pending: Nate runs `shipping_replay`. |
| 3 | FedEx label certification | Blocked on FedEx (below). |
| 4 | Queue/pool/print pages, void, C7 fulfillment writeback, low-volume live test | — |
| 5 | UPS adapter; daily tracking sync + delivery-exception queue (both carriers) | — |
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
- **Box fit:** magnum alone; "Port Club Volume" items in Port box; rest fills 12s then closest.
- **Pools:** carrier first; Default pool = exactly 1 each of `SHIP_DEFAULT_CLUB_SKUS`;
  rest sorted by box/SKU signature, split evenly near 20 **packages** (assumption: Nate's
  "~20 cases" = boxes — confirm). Address units never split across pools.
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

1. `git pull`; confirm Nate ran `migrate` and `shipping_replay`. Read
   `label_output/shipping_replay.txt` (stage it from his folder) — box-count, combining and
   carrier differences are the Tranche 2 exit gate. Fix rule gaps it shows.
2. Confirm "~20 cases" means 20 boxes (packages), or switch pool sizing to bottles/12.
3. First live **Pull orders**: check FedEx address-validation results look sane (review
   queue not flooded by formatting); adjust `_material_differences` / parse if needed.
4. FedEx sandbox alcohol + Ground still blocked (Nate to call FedEx) → Tranche 3 cert label.
5. Then Tranche 4: pack/print pages, buy labels per package, void, C7 fulfillment writeback
   (`POST /order/{id}/fulfillment`; a combined box posts the same tracking to every order in it).
