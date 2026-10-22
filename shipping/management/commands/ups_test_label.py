"""UPS exit gate: check credentials, then buy one UPS CIE (test) label.

    python manage.py ups_test_label --check          # OAuth only; never prints keys
    python manage.py ups_test_label                  # 1 Bottle, alcohol + adult signature
    python manage.py ups_test_label --box "4 Bottle" --bottles 4 --home
    python manage.py ups_test_label --void 1Z...

Same rules as fedex_test_label: refuses unless UPS_ENV=sandbox, writes the
PDF to label_output/ and records the label. Print the PDF on the thermal
printer and check (a) it's upright -- if not, flip GIF_ROTATE in
shipping/carriers/ups.py -- and (b) the barcode scans.
"""
import datetime as dt
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from shipping.carriers import (Address, CarrierConfigError, CarrierError, LabelRequest,
                               Parcel, get_adapter)
from shipping.carriers.ups import default_service
from shipping.models import BottleWeight, BoxSize, Label
from shipping.services.labels import buy_label, void_label

# UPS's own Atlanta HQ (a business address in their test docs); reserved 555-01xx phone.
TEST_RECIPIENT = dict(name="Label Test", company="UPS", street1="55 Glenlake Pkwy NE",
                      city="Atlanta", state="GA", postal_code="30328", phone="4045550100")


class Command(BaseCommand):
    help = "Check UPS credentials, or buy/void a UPS test (CIE) label."

    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true", help="only request an OAuth token")
        parser.add_argument("--box", default="1 Bottle")
        parser.add_argument("--bottles", type=int, default=1)
        parser.add_argument("--ml", type=int, default=750)
        parser.add_argument("--home", action="store_true", help="mark recipient residential")
        parser.add_argument("--no-alcohol", action="store_true",
                            help="omit the alcohol indicator (if the CIE account lacks the "
                                 "alcohol contract) -- NOT a certification label")
        parser.add_argument("--void", metavar="TRACKING")
        parser.add_argument("--out", default=str(Path(settings.BASE_DIR) / "label_output"))

    def handle(self, *args, **opts):
        if settings.SHIPPING["UPS"]["ENV"] != "sandbox":
            raise CommandError("UPS_ENV is not 'sandbox' -- this command only buys test labels.")
        try:
            adapter = get_adapter("ups")
        except CarrierError as e:
            raise CommandError(str(e))

        if opts["check"]:
            try:
                adapter._token()
            except CarrierError as e:
                raise CommandError(f"UPS refused the credentials: {e}\nCheck UPS_CLIENT_ID / "
                                   f"UPS_CLIENT_SECRET / UPS_ACCOUNT_NUMBER in .env.")
            self.stdout.write(self.style.SUCCESS("OK -- UPS issued a token. Run ups_test_label."))
            return
        if opts["void"]:
            label = Label.objects.filter(tracking_number=opts["void"], carrier="ups").first()
            if label is None:
                raise CommandError(f"No recorded UPS label {opts['void']}.")
            try:
                void_label(label, adapter=adapter)
            except Exception as e:  # CarrierError or LabelRefused
                raise CommandError(f"Void failed: {e}")
            self.stdout.write(self.style.SUCCESS(f"Voided {opts['void']}."))
            return

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

        to = Address(**TEST_RECIPIENT, residential=bool(opts["home"]))
        req = LabelRequest(
            ship_from=Address.from_settings(ship_from), ship_to=to,
            parcel=Parcel(box.length_in, box.width_in, box.height_in, weight,
                          reference="UPS-CIE-TEST"),
            service=default_service(to.residential), ship_date=dt.date.today(),
            contains_alcohol=not opts["no_alcohol"],
        )
        try:
            label = buy_label(req, carrier="ups", adapter=adapter)
        except CarrierConfigError as e:
            raise CommandError(f"UPS rejected the API credentials: {e}")
        except CarrierError as e:
            detail = f"\n{e.details}" if e.details else ""
            raise CommandError(f"UPS refused the label: {e}{detail}")

        out = Path(opts["out"])
        out.mkdir(parents=True, exist_ok=True)
        self.stdout.write(self.style.SUCCESS(
            f"Test label {label.tracking_number} · UPS Ground · {box} · {weight} lb · "
            + ("adult signature only" if opts["no_alcohol"] else "alcohol + adult signature")))
        if label.net_charge is not None:
            self.stdout.write(f"Test charge: {label.net_charge} {label.currency}")
        for doc in label.documents.all():
            p = out / f"ups_cie_{label.tracking_number}_{doc.sequence}.pdf"
            p.write_bytes(bytes(doc.content))
            self.stdout.write(f"  {p}")
        self.stdout.write(f"Void it with: python manage.py ups_test_label --void {label.tracking_number}")
