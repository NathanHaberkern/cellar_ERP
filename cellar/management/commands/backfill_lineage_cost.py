"""Backfill LotLineage.occurred_at and .cost_per_gal_snapshot for edges written
before migration 0027.

WHY A REPLAY AND NOT A SINGLE PASS
----------------------------------
The naive fix — snapshot every edge at its parent's cost/gal *today* — is wrong
for any parent that accumulated cost after the blend. A 2024 lot blended in
March and topped through November would hand its child a March transfer priced
at November's cost.

So this replays the graph in chronological order, tracking a running cost and
running volume per lot exactly as the live services would have:

    for each edge, oldest first:
        cpg   = running_cost[parent] / running_vol[parent]
        write cpg onto the edge
        move (cpg * volume_gal) from parent to child
        move volume_gal from parent to child

Edges are ordered by occurred_at where derivable, else created_at (Nate's call:
created_at is approximately right for rows keyed in real time, and the cost
ledger starts at 2025 anyway).

APPEND-ONLY BYPASS
------------------
LotLineage is AppendOnly, so .save() refuses to edit these fields after
creation. This command writes through queryset.update(), which bypasses save().
That is correct for a one-time historical repair on fields that did not exist
when the rows were written — it is NOT a pattern to copy into live services.
Live code sets both fields at creation.

USAGE
-----
    python manage.py backfill_lineage_cost --dry-run     # report only
    python manage.py backfill_lineage_cost               # write
    python manage.py backfill_lineage_cost --force       # re-snapshot filled rows
"""
from collections import defaultdict
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

QUANT = Decimal("0.0001")


class Command(BaseCommand):
    help = "Backfill LotLineage.occurred_at / .cost_per_gal_snapshot (pre-0027 edges)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="report what would change; write nothing")
        parser.add_argument("--force", action="store_true",
                            help="re-snapshot edges that already have a value")

    def handle(self, *args, **opts):
        from cellar.models import LotLineage
        from cellar.services import costing as costing_svc

        dry = opts["dry_run"]
        force = opts["force"]

        edges = list(LotLineage.objects.filter(voided_at__isnull=True)
                     .select_related("parent_lot", "child_lot"))
        if not edges:
            self.stdout.write("No lineage edges. Nothing to do.")
            return

        # ---- order: occurred_at, else created_at -------------------------
        def sort_key(e):
            d = e.occurred_at or costing_svc.to_business_date(e.created_at)
            return (d, e.id)

        edges.sort(key=sort_key)

        # ---- seed running cost/volume from each lot's OWN direct costs ----
        # Direct cost only: inherited cost arrives through the replay itself,
        # so seeding with lot_cost() would double-count every parent.
        lots = {}
        for e in edges:
            lots[e.parent_lot_id] = e.parent_lot
            lots[e.child_lot_id] = e.child_lot

        # Reconstruct each lot's volume as it stood BEFORE any lineage edge fired:
        # today's balance, plus everything that has since left through a LIQUID edge.
        #
        # Two traps here, both found by test_backfill_prices_a_legacy_edge:
        #   1. Use volumes.lot_balance() directly, NOT cost_basis_volume(). The
        #      latter falls back to the booking volume when the balance is 0 —
        #      which for a drained parent is ALREADY the pre-transfer figure, so
        #      adding outbound on top double-counts it ($20/gal became $10/gal).
        #   2. Only _LIQUID_EDGES move balance. BOTTLING_SPLIT is accounted through
        #      BottlingRun, so adding it back would inflate the divisor.
        from cellar.services import volumes as vol_svc
        from cellar.services.aging import _lot_volume

        out_by_lot = defaultdict(lambda: Decimal("0"))
        for x in edges:
            if x.relationship_type in vol_svc._LIQUID_EDGES:
                out_by_lot[x.parent_lot_id] += Decimal(str(x.volume_gal or 0))

        run_cost = {}
        run_vol = defaultdict(lambda: Decimal("0"))
        for lot_id, lot in lots.items():
            run_cost[lot_id] = Decimal(str(costing_svc.lot_direct_cost(lot)))
            bal = vol_svc.lot_balance(lot)
            if bal is None:
                v = _lot_volume(lot)
                bal = Decimal(str(v)) if v else Decimal("0")
            run_vol[lot_id] = Decimal(str(bal)) + out_by_lot[lot_id]

        wrote = skipped = no_basis = dates = 0
        rows = []

        for e in edges:
            vol = Decimal(str(e.volume_gal or 0))
            pid, cid = e.parent_lot_id, e.child_lot_id

            # occurred_at
            new_date = e.occurred_at or costing_svc.to_business_date(e.created_at)
            set_date = (e.occurred_at is None and new_date is not None)

            if e.cost_per_gal_snapshot is not None and not force:
                skipped += 1
                if set_date:
                    rows.append((e.pk, {"occurred_at": new_date}))
                    dates += 1
                continue

            pv = run_vol.get(pid) or Decimal("0")
            if pv <= 0:
                no_basis += 1
                if set_date:
                    rows.append((e.pk, {"occurred_at": new_date}))
                    dates += 1
                continue

            cpg = (run_cost.get(pid, Decimal("0")) / pv).quantize(QUANT)
            moved = cpg * vol

            run_cost[pid] = run_cost.get(pid, Decimal("0")) - moved
            run_cost[cid] = run_cost.get(cid, Decimal("0")) + moved
            run_vol[pid] = pv - vol
            run_vol[cid] = run_vol.get(cid, Decimal("0")) + vol

            payload = {"cost_per_gal_snapshot": cpg}
            if set_date:
                payload["occurred_at"] = new_date
                dates += 1
            rows.append((e.pk, payload))
            wrote += 1

            self.stdout.write(
                f"  {e.parent_lot.code} → {e.child_lot.code} "
                f"[{e.relationship_type}] {vol} gal @ ${cpg}/gal = ${moved.quantize(Decimal('0.01'))}")

        if not dry:
            with transaction.atomic():
                for pk, payload in rows:
                    # queryset.update() — deliberate AppendOnly bypass, see module docstring
                    LotLineage.objects.filter(pk=pk).update(**payload)

        verb = "would write" if dry else "wrote"
        self.stdout.write(self.style.SUCCESS(
            f"\n{verb}: {wrote} cost snapshots, {dates} occurred_at dates. "
            f"skipped {skipped} already-snapshotted, {no_basis} with no volume basis."))
        if no_basis:
            self.stdout.write(self.style.WARNING(
                f"{no_basis} edge(s) had no parent volume to divide by — those parents "
                f"never booked a gauge. They stay null and fall back to live computation."))
        if dry:
            self.stdout.write(self.style.WARNING("DRY RUN — nothing written."))
