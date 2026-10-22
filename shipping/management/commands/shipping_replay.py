"""Tranche 2 exit gate: re-pack orders that already shipped and compare with what really happened.

    python manage.py shipping_replay                          # Club orders paid since Sep 1
    python manage.py shipping_replay --since 2026-10-01 --until 2026-10-15
    python manage.py shipping_replay --channel all

Pulls FULFILLED ship orders from C7, runs them through the same packing rules
as "Build pools" (services/packing.py), and compares against C7's record of
how each order actually went out:

  * boxes     -- our package count vs the number of distinct tracking numbers C7
                 holds for those orders (packageCount when there's no tracking).
  * combining -- orders that shared a tracking number in C7 were packed in one
                 box; we should have combined the same ones.
  * carrier   -- our routing vs the carrier on C7's fulfillment.
  * pools     -- printed for Nate to eyeball; there is no record to diff them against.

Read-only: writes nothing to the database or to C7. The report is printed and
saved to label_output/shipping_replay.txt. Addresses are reduced to state +
order number in the report.
"""
from collections import defaultdict
from datetime import date
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from integrations.commerce7 import C7Error, Commerce7Client
from shipping.services.intake import resolve_ml
from shipping.services.packing import build_shipments, order_from_c7, plan_pools
from shipping.services.pools import db_boxes, weight_lookup


def _c7_shipping(raw):
    """(tracking numbers, package count, carrier) from an order's fulfillments."""
    tracking, count, carriers = [], 0, set()
    for f in raw.get("fulfillments") or []:
        shipped = f.get("shipped") or {}
        nums = shipped.get("trackingNumbers") or f.get("trackingNumbers") or []
        tracking += [str(n) for n in nums if n]
        count += int(f.get("packageCount") or 0)
        c = (shipped.get("carrier") or f.get("carrier") or "").lower()
        if c:
            carriers.add("fedex" if "fedex" in c else "ups" if "ups" in c else c)
    return tracking, count, carriers


class Command(BaseCommand):
    help = "Re-pack already-shipped C7 orders with the app's rules and compare (read-only)."

    def add_arguments(self, parser):
        parser.add_argument("--since", type=date.fromisoformat, default=date(2026, 9, 1))
        parser.add_argument("--until", type=date.fromisoformat)
        parser.add_argument("--channel", default="Club", help="Club / Web / POS / all")
        parser.add_argument("--limit", type=int, default=2000)
        parser.add_argument("--out", default=str(Path(settings.BASE_DIR) / "label_output"
                                                 / "shipping_replay.txt"))

    def handle(self, *args, **o):
        conf = settings.SHIPPING
        params = {"orderDeliveryMethod": "Ship", "fulfillmentStatus": "Fulfilled",
                  "orderPaidDate": (f"btw:{o['since']}|{o['until']}" if o["until"]
                                    else f"gte:{o['since']}")}
        if o["channel"].lower() != "all":
            params["channel"] = o["channel"]
        try:
            client = Commerce7Client.from_settings()
            raws = list(client.iter_pages("order", "orders", params, o["limit"]))
        except C7Error as e:
            raise CommandError(str(e))

        out = []
        say = out.append
        say(f"shipping_replay  {params}")
        say(f"{len(raws)} fulfilled orders from C7")
        if not raws:
            self._finish(out, o["out"])
            return

        orders, c7 = [], {}
        for raw in raws:
            d = order_from_c7(raw, ml_for_sku=resolve_ml)
            orders.append(d)
            c7[d.number] = _c7_shipping(raw)

        shipments, problems = build_shipments(orders, db_boxes(), weight_lookup(),
                                              conf["FEDEX_RESTRICTED_STATES"])
        for number, msg in problems:
            say(f"  CAN'T PACK #{number}: {msg}")

        # C7's real combining: orders that share any tracking number.
        by_tracking = defaultdict(set)
        for number, (tracking, _, _) in c7.items():
            for t in tracking:
                by_tracking[t].add(number)
        c7_partners = defaultdict(set)
        for group in by_tracking.values():
            for n in group:
                c7_partners[n] |= group

        box_ok = box_bad = 0
        box_lines, combine_lines, carrier_lines = [], [], []
        for s in shipments:
            nums = set(s.numbers)
            tracking = {t for n in nums for t in c7[n][0]}
            real = len(tracking) if tracking else sum(c7[n][1] for n in nums)
            ours = len(s.packages)
            boxes = " + ".join(p.box.name for p in s.packages)
            tag = "/".join(f"#{n}" for n in sorted(nums))
            state = s.orders[0].state
            if real == ours:
                box_ok += 1
            else:
                box_bad += 1
                box_lines.append(f"  {tag} {state}: app {ours} ({boxes}, "
                                 f"{sum(p.bottle_count for p in s.packages)} btl) vs C7 {real}")
            c7_group = set().union(*(c7_partners.get(n, {n}) for n in nums)) or nums
            if c7_group != nums:
                combine_lines.append(f"  app combined {tag}; C7 shipped together: "
                                     f"{'/'.join('#' + n for n in sorted(c7_group))}")
            real_carriers = set().union(*(c7[n][2] for n in nums))
            if real_carriers and s.carrier not in real_carriers:
                carrier_lines.append(f"  {tag} {state}: app {s.carrier.upper()} "
                                     f"({s.carrier_reason or 'default'}) vs C7 "
                                     f"{'/'.join(sorted(real_carriers)).upper()}")

        total = box_ok + box_bad
        say("")
        say(f"BOX COUNT  {box_ok}/{total} shipments match C7"
            + (f" ({100 * box_ok // total}%)" if total else ""))
        out += box_lines or ["  all match"]
        say("")
        say(f"COMBINING  {len(combine_lines)} difference(s)")
        out += combine_lines or ["  all match"]
        say("")
        say(f"CARRIER  {len(carrier_lines)} difference(s)")
        out += carrier_lines or ["  all match"]

        pools = plan_pools(shipments, default_skus=conf["DEFAULT_CLUB_SKUS"],
                           target_bottles=12 * conf["POOL_TARGET_CASES"],
                       max_bottles=12 * conf["POOL_MAX_CASES"])
        say("")
        say(f"POOLS  {len(pools)} pools, {sum(p.package_count for p in pools)} packages, "
            f"{sum(p.bottle_count for p in pools)} bottles")
        for p in pools:
            say(f"  {p.name}: {p.cases} cases ({p.bottle_count} btl), {p.package_count} packages, "
                f"{len(p.shipments)} shipments")
            for s in p.shipments:
                boxes = ", ".join(pk.box.name + "[" + " ".join(
                    "%sx%s" % (c["sku"], c["qty"]) for c in pk.contents) + "]"
                    for pk in s.packages)
                club = " club" if any(x.is_club for x in s.orders) else ""
                say(f"    {'/'.join('#' + n for n in s.numbers)} {s.orders[0].state}{club}: {boxes}")
        self._finish(out, o["out"])

    def _finish(self, lines, path):
        text = "\n".join(lines) + "\n"
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        self.stdout.write(text)
        self.stdout.write(self.style.SUCCESS(f"Saved to {p}"))

