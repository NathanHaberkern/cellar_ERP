"""
Seed the 2025 fruit contract prices and the high-proof spirit account.

    python manage.py seed_2025_costs             # dry run — prints what it would write
    python manage.py seed_2025_costs --yes

WHY THIS IS A COMMAND AND NOT A MIGRATION
-----------------------------------------
Two different kinds of data, and they need different homes:

  * FruitPrice rows are REFERENCE data — reset_transactional keeps them. They
    could live in a migration, except they depend on Variety and Block rows that
    are curated by hand and may not exist yet on a given database. A migration
    that silently no-ops when the catalog is empty is exactly the failure mode
    0009 was written to avoid, so this one is loud instead.

  * HighProofSpiritLedger rows are TRANSACTIONAL — reset_transactional deletes
    them. Putting them in a migration would mean a reset leaves you with an empty
    spirit account and no way back short of un-applying migrations. So they are
    re-seedable by hand, and you re-run this after a reset.

THE SPIRIT NUMBERS ARE NOT INVENTED
-----------------------------------
They're read off the filed 2025 Wine Premises Operations Reports, Part III:

    1/1/25 opening ....... 205.41 PG
    June draw ............ −112.40 PG
    Sept receipt ......... +866.50 PG   (959.51 total − 93.01 on hand)
    Sept draw ............ −129.88 PG
    Oct draw ............. −700.54 PG
    12/31/25 closing ..... 128.32 PG

Only the two INFLOWS are seeded here. The draws are not: they belong to specific
Port fortifications, and `FortificationEvent.save()` creates its own HPGS draw
when you enter the fortification. Seeding the draws too would double-debit the
account and the fortifications would then fail with "HPGS account holds only …".

Proof is 174.0 (Wine Secrets, ~174 and varying by shipment) and cost is $18.00/WG.
"""
from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from cellar.models import Block, FruitPrice, HighProofSpiritLedger, Variety

PROOF = Decimal("174.0")
COST_PER_WG = Decimal("18.00")

# variety name, block name fragment (None = varietal price), $/ton
FRUIT_PRICES_2025 = [
    ("Petite Sirah",       None,     Decimal("1000")),
    ("Alicante Bouschet",  None,     Decimal("1000")),
    ("Zinfandel",          None,     Decimal("1600")),
    ("Barbera",            None,     Decimal("1600")),
    ("Cabernet Sauvignon", "Martel", Decimal("2000")),
]

# label, date, proof gallons in
HPGS_RECEIPTS = [
    ("2025 opening balance (filed 5120.17 Part III)", date(2025, 1, 1), Decimal("205.41")),
    ("Wine Secrets — Sept 2025 delivery",             date(2025, 9, 15), Decimal("866.50")),
]


class Command(BaseCommand):
    help = "Seed 2025 fruit contract prices and the HPGS spirit account."

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true",
                            help="Actually write. Without this it is a dry run.")
        parser.add_argument("--vintage", type=int, default=2025)

    def handle(self, *args, **opts):
        commit, vintage = opts["yes"], opts["vintage"]

        self.stdout.write(self.style.WARNING(f"Fruit contract prices — {vintage}:"))
        fruit_plan = []
        for vname, blk_frag, price in FRUIT_PRICES_2025:
            variety = Variety.objects.filter(name__iexact=vname).first()
            if variety is None:
                self.stdout.write(self.style.ERROR(
                    f"  MISSING VARIETY '{vname}' — seed the variety catalog first."))
                continue
            block = None
            if blk_frag:
                block = Block.objects.filter(name__icontains=blk_frag).first()
                if block is None:
                    self.stdout.write(self.style.WARNING(
                        f"  no block matching '{blk_frag}' — writing ${price}/ton as the "
                        f"VARIETAL price for {vname}. Add the block and re-run to make it "
                        f"block-specific."))
            exists = FruitPrice.objects.filter(
                vintage_year=vintage, variety=variety, block=block).exists()
            where = f"{vname} / {block}" if block else vname
            self.stdout.write(f"  {'(exists)' if exists else '  +     '} {where:<34} ${price}/ton")
            fruit_plan.append((variety, block, price, exists))

        self.stdout.write(self.style.WARNING("\nHigh-proof spirit account (inflows only):"))
        on_hand = HighProofSpiritLedger.on_hand_pg()
        spirit_plan = []
        for label, when, pg in HPGS_RECEIPTS:
            exists = HighProofSpiritLedger.objects.filter(
                shipment_ref=label, voided_at__isnull=True).exists()
            wg = (pg / PROOF * 100).quantize(Decimal("0.01"))
            cost = (wg * COST_PER_WG).quantize(Decimal("0.01"))
            self.stdout.write(
                f"  {'(exists)' if exists else '  +     '} {when}  {pg:>8} PG  "
                f"= {wg:>7} WG @ {PROOF} proof   ${cost}   {label}")
            spirit_plan.append((label, when, pg, wg, cost, exists))

        self.stdout.write(
            f"\n  HPGS on hand now: {on_hand} PG. "
            f"Draws are NOT seeded — each Port fortification books its own.")

        if not commit:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN — nothing written. Re-run with --yes."))
            return

        with transaction.atomic():
            n_fruit = 0
            for variety, block, price, exists in fruit_plan:
                if exists:
                    continue
                FruitPrice.objects.create(
                    vintage_year=vintage, variety=variety, block=block,
                    price_per_ton=price, notes="2025 contract price (Nate)")
                n_fruit += 1

            n_spirit = 0
            for label, when, pg, wg, cost, exists in spirit_plan:
                if exists:
                    continue
                HighProofSpiritLedger.objects.create(
                    event_type=HighProofSpiritLedger.EventType.RECEIPT,
                    event_date=when, wine_gallons=wg, proof=PROOF,
                    proof_gallons=pg, supplier="Wine Secrets",
                    shipment_ref=label, cost=cost)
                n_spirit += 1

        self.stdout.write(self.style.SUCCESS(
            f"\nWrote {n_fruit} fruit prices and {n_spirit} spirit receipts. "
            f"HPGS now holds {HighProofSpiritLedger.on_hand_pg()} PG "
            f"at {HighProofSpiritLedger.current_blended_proof():.2f} blended proof."))
