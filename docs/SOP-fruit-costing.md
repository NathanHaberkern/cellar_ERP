# SOP — Fruit Costing and Related-Party Transfer Pricing

**St. Amant Winery** · BW-CA-5526
**Applies to:** all fruit delivered from a commonly-controlled vineyard entity to the winery entity
**Owner:** General Manager
**Review:** annually, before August 1
**Version:** 1.0 — *effective date and approval to be completed on adoption*

> This SOP was drafted for internal use and has not been reviewed by a CPA or tax
> counsel. Sections 3, 4 and 8 state positions with tax consequences and should be
> confirmed with the winery's accountant before the first harvest it governs.

---

## 1. Purpose

The vineyard and the winery are separate legal entities under common control. Fruit
moving between them is a related-party transaction, and IRC §482 requires it to be
priced at arm's length — the price unrelated parties would have agreed to. The IRS
may reallocate income between commonly-controlled entities where it is not.

This SOP sets the method, the evidence standard, and the timing, so that the price
on any given lot is the result of a policy rather than a decision made after the
fact. **Consistency and pre-commitment are worth more than the precision of any
individual number.** A defensible method applied evenly, including in the years it
works against us, is the position that survives examination. Choosing a different
basis each year, or per lot, is not.

## 2. Scope

| In scope | Out of scope |
|---|---|
| Estate and commonly-controlled fruit delivered to the winery | Third-party purchased fruit (Mohr-Fry, Spencer Ranch) — priced by contract |
| Fruit sold externally, as evidence for §3 | Bulk wine sales and custom crush |
| The provisional-to-final pricing cycle | Farming cost accounting inside the vineyard entity |

Third-party purchase contracts are **not** repriced by this SOP. Their invoice price
is entered on the weigh tag and governs directly.

## 3. Pricing hierarchy

Apply in order. Stop at the first tier that produces a price. Do not skip a tier
because a lower one gives a more convenient number.

| Tier | Basis | `basis` value | Use when |
|---|---|---|---|
| 1 | Arm's-length sale of the same variety, vintage **and block** | `arms_length_sale` | We sold part of that block's crop to an unrelated buyer |
| 2 | Arm's-length sale of the same variety and vintage, different block | `arms_length_sale` | We sold that variety but not from that block |
| 3 | Third-party purchase contract for the same variety, vintage and district | `contract` | We bought comparable fruit that year |
| 4 | Grape Crush Report district average, same vintage | `district_average` | Report has published (see §4) |
| 5 | Grape Crush Report district average, **prior** vintage | `prior_year_district` | At delivery, before the report publishes — **provisional** |
| 6 | Negotiated | `negotiated` | Nothing above applies; requires written rationale |

### 3.1 The tier-1 and tier-2 rule is not optional

Where we sell part of a crop and crush the rest, **the sale price governs the
internal transfer of the same fruit.** An actual arm's-length sale is a Comparable
Uncontrolled Price and outranks any published average. Falling back to a district
average when our own invoices show a higher number for the same fruit in the same
week is the single most examinable thing we could do.

A differential between the sold price and the transferred price is permitted where
there is a real commercial reason for it. It must be written into `source_ref` or
`notes` at the time, not reconstructed later. Acceptable reasons include:

- different block, materially different fruit quality or maturity
- a spot buyer paying up against short supply late in the season
- meaningfully different payment terms, tonnage, or delivery risk

"District average is our policy" is **not** an acceptable reason when a tier-1 or
tier-2 comparable exists.

### 3.2 Districts

- Lodi fruit → **District 11**
- Amador fruit → **District 10**

Take the weighted average price for the variety in that district from the report.

## 4. The timing problem, and the provisional price

The Grape Crush Report for a vintage does not publish until the following calendar
year — preliminary around February 10, final around March 10. Fruit received in
September therefore cannot be priced against its own vintage's district average.

Using the most recent published data available on the day is a reasonable and
defensible position; arm's-length analysis is judged on what could reasonably have
been known at the time, and there is no requirement to use data that does not yet
exist. But a prior-year average is a **lagging** benchmark and is directionally
biased by wherever the market cycle sits. It is therefore treated as provisional,
never final.

### 4.1 Two-step pricing

1. **At delivery** — book the tier-5 price, set `is_provisional = True`, and record
   the report edition in `source_ref`.
2. **By March 31 of the following year** — enter a `FruitPriceRevision` carrying the
   final price from that vintage's Final Grape Crush Report. The system books the
   signed difference as its own dated cost line.

The intercompany fruit supply agreement must contain a provisional-price and
true-up clause matching this. A post-harvest final price is ordinary grape-contract
practice — the Crush Report's own January 10 and January 31 price-finality cutoffs
exist because of it — so structuring the internal transfer the same way as external
contracts is itself an arm's-length argument.

### 4.2 The true-up runs both ways

If the final figure comes in **below** the provisional price, the true-up is a
credit and it is booked. A policy that only corrects in one direction is not a
policy. In a falling market (as in the recent Lodi oversupply) the credit case will
be the normal one.

## 5. Procedure — at delivery

| # | Step | Where |
|---|---|---|
| 1 | Confirm which tier of §3 applies. If tier 1 or 2, pull the invoice or contract. | — |
| 2 | Create or confirm the `FruitPrice` row for the vintage, variety and (if block-specific) block. | Reference → **Fruit prices** |
| 3 | Enter `price_per_ton`. | " |
| 4 | Set `basis` to the tier used. | " |
| 5 | Enter `source_ref` — the invoice/contract number, or the report edition, district and variety. **Never leave blank.** | " |
| 6 | Tick `is_provisional` if and only if `basis = prior_year_district`. | " |
| 7 | Confirm the weigh tag has **no** `fruit_cost_per_ton` — a tag price overrides `FruitPrice` and will not be trued up. | Intake |

`source_ref` examples:

- `Grape Crush Report 2025 Final, District 11, Zinfandel — $1,600/ton weighted avg`
- `Invoice 2026-114, [buyer], 18.4 tons Zinfandel, Marian's block, 9/22/26`

## 6. Procedure — the annual true-up

Run in **March**, after the Final Grape Crush Report publishes.

| # | Step | Where |
|---|---|---|
| 1 | Download the Final report; note the weighted average for each variety in Districts 10 and 11. | nass.usda.gov / cdfa.ca.gov |
| 2 | List every `FruitPrice` row with `is_provisional = True` for the vintage. | Reference → **Fruit prices** |
| 3 | For each, add a `FruitPriceRevision`: final price, `basis = district_average`, `source_ref` naming the report edition, `effective_on` = the report's publication date. | Reference → **Fruit price revisions** |
| 4 | Run the cost poster. | `manage.py` cost posting |
| 5 | Run reconciliation; confirm zero drift. | `manage.py cost_reconcile` |
| 6 | Review the true-up total; if material, notify the accountant before the entities' returns are filed. | — |
| 7 | Confirm the vineyard entity invoices (or credits) the winery for the same amount. **The books and the paper have to agree.** | — |

Step 7 is the one that gets skipped and it is the one that matters most. A true-up
recorded only in the ERP, with no corresponding intercompany invoice, is a
bookkeeping entry rather than a transaction, and is worth nothing as evidence.

### 6.1 Correcting a true-up

Void the `FruitPriceRevision`, **void the posted `CostEntry` rows for it**, enter the
corrected revision, and re-post. Voiding only the revision will not repost, because
posting is idempotent on the source reference. Reconciliation catches this and
`close_period()` refuses to close, so the error surfaces loudly — but it must still
be fixed by hand.

## 7. What the system does, and what it does not

**It does:**

- keep the as-booked price permanently on the `FruitPrice` row — the true-up never
  rewrites it, so what was booked at delivery remains visible
- book the difference as a separate, dated `CostEntry` under the **Fruit** category
- date the true-up to the report's publication, not the delivery, so it lands in an
  open period and carries a `deferred_note` naming the month it relates to
- exclude allocations whose price came from a weigh tag rather than `FruitPrice`
- refuse to close an accounting period while a revision is entered but unposted

**It does not:**

- retroactively restate a closed period. The true-up is a new fact learned in March,
  not a correction of September. This is deliberate: a closed month has already been
  summarised into a QBO journal entry and reported.
- price fruit for you, or check that the tier in §3 was applied honestly
- generate the intercompany invoice

## 8. Considerations behind the policy

Recorded so that future readers understand why the method is what it is.

**Timing asymmetry.** The vineyard recognises revenue at delivery. The winery
capitalises fruit into inventory and does not deduct it until the wine sells —
several years for Heritage lots, longer for Port. A higher transfer price therefore
accelerates taxable income into the vineyard today and defers the matching deduction
at the winery for years. Where ownership of the two entities is identical this is a
cash-tax cost with no permanent benefit.

**Risk asymmetry.** A price set too low understates vineyard income and is the
direction §482 actually polices, with adjustment plus potential §6662 penalty
exposure. A price set too high costs cash timing only. The asymmetry in *risk*, not
the arithmetic, is why the hierarchy in §3 is followed rather than optimised around.

**Non-tax consequences.** Where the entities do not have identical ownership, the
transfer price moves real money between different pockets and a chronically low price
is a fiduciary problem, not merely a tax one. A persistently low price also
understates the vineyard for lender borrowing-base, buy-sell valuation, and crop
insurance purposes.

**If the entities are ever merged.** This SOP ceases to apply. In a single entity
there is no transfer price: estate fruit enters inventory at actual farming cost, and
a market-based price would overstate inventory basis.

## 9. Records to retain

Retain for the longer of seven years or the applicable statute of limitations:

- the written intercompany fruit supply agreement, with the provisional/true-up clause
- this SOP, each version, dated
- Preliminary and Final Grape Crush Reports for each vintage
- third-party sale invoices and purchase contracts cited in any `source_ref`
- the intercompany invoices and true-up credits
- the ERP's `FruitPrice` and `FruitPriceRevision` rows (retained by the system;
  `reset_transactional` preserves both as master data)

---

## Appendix A — Worked example

2026 Zinfandel, Lodi District 11. 20 tons delivered September 18, 2026. No arm's-length
sale of this block, no comparable purchase contract, so tier 5 applies.

| | |
|---|---|
| Provisional price (2025 Final, Dist. 11 Zinfandel) | $1,600.00/ton |
| Booked at delivery, 20 tons | **$32,000.00** |
| `basis` | `prior_year_district` |
| `is_provisional` | ✔ |

March 10, 2027 — the 2026 Final report publishes at $1,425.00/ton.

| | |
|---|---|
| Final price | $1,425.00/ton |
| Delta | −$175.00/ton |
| True-up, 20 tons, dated 2027-03-10 | **−$3,500.00** |
| Fruit cost as booked (unchanged) | $32,000.00 |
| **Total fruit cost on the lot** | **$28,500.00** |

The vineyard entity issues a credit memo to the winery for $3,500.00 in the same
month. Both cost lines remain visible on the lot's Cost tile.

## Appendix B — Basis codes

| Code | Meaning | Provisional? |
|---|---|---|
| `arms_length_sale` | Arm's-length sale of the same vintage's fruit | No |
| `contract` | Third-party purchase contract | No |
| `district_average` | Grape Crush Report, same vintage | No |
| `prior_year_district` | Grape Crush Report, prior vintage | **Yes** |
| `farming_cost` | Actual cost of farming | No |
| `negotiated` | Negotiated / other — requires written rationale | No |
