"""Tranche 1 exit gate: buy one FedEx SANDBOX label for a DTC wine package.

    python manage.py fedex_test_label
    python manage.py fedex_test_label --box "4 Bottle" --bottles 4 --home
    python manage.py fedex_test_label --service FEDEX_2_DAY
    python manage.py fedex_test_label --void 794600000000

Writes the PDF(s) to label_output/ (gitignored) and records the label in the
database, so it also exercises the save path. Refuses to run unless FEDEX_ENV
is "sandbox": a test command must never be able to buy a real label.

These same sandbox labels are what you'll send FedEx for certification in
Tranche 3, so print one and check it on your thermal printer.
"""
import datetime as dt
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from shipping.carriers import (Address, CarrierConfigError, CarrierError, LabelRequest,
                               Parcel, get_adapter)
from shipping.carriers.fedex import default_service
from shipping.models import BottleWeight, BoxSize, Label
from shipping.services.labels import buy_label, void_label

# A FedEx-owned business address (from FedEx's own certification docs), so the
# sandbox never sees a real customer's details. Phone is a reserved 555-01xx.
TEST_RECIPIENT = dict(name="Label Test", company="FedEx Services",
                      street1="10 FedEx Parkway", city="Collierville", state="TN",
                      postal_code="38017", phone="9015550100")


class Command(BaseCommand):
    help = "Buy (or void) a FedEx sandbox label for an alcohol + adult-signature package."

    def add_arguments(self, parser):
        parser.add_argument("--box", default="1 Bottle", help="BoxSize name (default: 1 Bottle)")
        parser.add_argument("--bottles", type=int, default=1)
        parser.add_argument("--ml", type=int, default=750, help="bottle size for weight")
        parser.add_argument("--home", action="store_true",
                            help="mark recipient residential and use Home Delivery")
        parser.add_argument("--service", default="",
                            help="FedEx serviceType to force, e.g. FEDEX_2_DAY or "
                                 "PRIORITY_OVERNIGHT (default: Ground / Home Delivery). "
                                 "Sandbox test accounts sometimes lack Ground.")
        parser.add_argument("--no-alcohol", action="store_true",
                            help="omit the ALCOHOL special service (adult signature stays on). "
                                 "Only for proving the rest of the pipeline while FedEx "
                                 "enables alcohol on the sandbox test account.")
        parser.add_argument("--void", metavar="TRACKING", help="void a sandbox label instead")
        parser.add_argument("--out", default=str(Path(settings.BASE_DIR) / "label_output"))

    def handle(self, *args, **opts):
        if settings.SHIPPING["FEDEX"]["ENV"] != "sandbox":
            raise CommandError("FEDEX_ENV is not 'sandbox' -- this command only buys test labels.")
        try:
            adapter = get_adapter("fedex")
        except CarrierError as e:
            raise CommandError(str(e))

        if opts["void"]:
            return self._void(opts["void"], adapter)

        ship_from = settings.SHIPPING["SHIP_FROM"]
        if not ship_from.get("phone"):
            raise CommandError("Set SHIP_FROM_PHONE in .env (both carriers require a shipper phone).")

        try:
            box = BoxSize.objects.get(name=opts["box"])
        except BoxSize.DoesNotExist:
            names = ", ".join(BoxSize.objects.values_list("name", flat=True))
            raise CommandError(f"No box {opts['box']!r}. Boxes: {names}")
        if opts["bottles"] > box.max_bottles:
            raise CommandError(f"{box} holds {box.max_bottles} bottles, not {opts['bottles']}.")
        bw = BottleWeight.objects.filter(ml=opts["ml"]).first()
        if bw is None:
            raise CommandError(f"No bottle weight for {opts['ml']} ml.")
        weight = box.declared_weight([bw.weight_lb] * opts["bottles"])

        to = Address(**TEST_RECIPIENT, residential=True if opts["home"] else False)
        req = LabelRequest(
            ship_from=Address.from_settings(ship_from), ship_to=to,
            parcel=Parcel(box.length_in, box.width_in, box.height_in, weight,
                          reference="T1-SANDBOX"),
            service=opts["service"] or default_service(to.residential),
            ship_date=dt.date.today(), contains_alcohol=not opts["no_alcohol"],
        )
        try:
            label = buy_label(req, carrier="fedex", adapter=adapter)
        except CarrierConfigError as e:
            raise CommandError(
                f"FedEx rejected the API credentials: {e}\n"
                f"Check FEDEX_CLIENT_ID / FEDEX_CLIENT_SECRET in .env are the project's "
                f"TEST key and secret (FEDEX_ENV=sandbox).")
        except CarrierError as e:
            detail = f"\n{e.details}" if e.details else ""
            hint = ""
            if e.code == "CUSTOMER.ALCOHOLSPECIALSERVICETYPE.INELIGIBLE":
                hint = ("\nThe sandbox test account isn't enabled for alcohol. Ask FedEx web "
                        "services support to enable it; meanwhile --no-alcohol proves the rest.")
            elif e.code == "GROUND.SHIPPING.NOTAUTHORIZED":
                hint = ("\nThe sandbox test account isn't enabled for Ground. Retry with "
                        "--service FEDEX_2_DAY, and ask FedEx web services support to enable "
                        "Ground/Home Delivery on the test account.")
            raise CommandError(f"FedEx refused the label: {e}{detail}{hint}")

        out = Path(opts["out"])
        out.mkdir(parents=True, exist_ok=True)
        paths = []
        for doc in label.documents.all():
            p = out / f"fedex_sandbox_{label.tracking_number}_{doc.sequence}_{doc.kind}.pdf"
            p.write_bytes(bytes(doc.content))
            paths.append(p)

        services = ("adult signature only -- NOT a certification label" if opts["no_alcohol"]
                    else "alcohol CONSUMER + adult signature")
        self.stdout.write(self.style.SUCCESS(
            f"Sandbox label {label.tracking_number} · {label.service} · {box} · "
            f"{weight} lb · {services}"))
        if label.net_charge is not None:
            self.stdout.write(f"Sandbox charge: {label.net_charge} {label.currency}")
        for p in paths:
            self.stdout.write(f"  {p}")
        self.stdout.write(f"Void it with: python manage.py fedex_test_label --void {label.tracking_number}")

    def _void(self, tracking, adapter):
        label = Label.objects.filter(tracking_number=tracking, carrier="fedex").first()
        if label is None:
            raise CommandError(f"No recorded FedEx label {tracking}.")
        try:
            void_label(label, adapter=adapter)
        except Exception as e:  # CarrierError or LabelRefused
            raise CommandError(f"Void failed: {e}")
        self.stdout.write(self.style.SUCCESS(f"Voided {tracking}."))
