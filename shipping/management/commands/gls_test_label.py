"""GLS: check credentials, then buy (or void) one GLS test label.

    python manage.py gls_test_label --check        # token only; never prints credentials
    python manage.py gls_test_label                # 12 Bottle box, 12 x 750 ml, adult signature
    python manage.py gls_test_label --void 5XXXXXXX

The GLS adapter is written ahead of GLS's API guide (see shipping/carriers/gls.py),
so the first run is the real check: if GLS refuses a field, the error names it.
Refuses unless GLS_ENV=sandbox. Ship-to is a business address on GLS's own
network (Lodi), so a test label never names a customer.
"""
import datetime as dt
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from shipping.carriers import Address, CarrierError, LabelRequest, Parcel, get_adapter
from shipping.carriers.gls import default_service
from shipping.models import BottleWeight, BoxSize, Label
from shipping.services.labels import buy_label, void_label

TEST_RECIPIENT = dict(name="Label Test", company="Test Wine Shop", street1="100 W Pine St",
                      city="Lodi", state="CA", postal_code="95240", phone="2095550100",
                      residential=False)


class Command(BaseCommand):
    help = "Check GLS credentials, or buy/void a GLS test label."

    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true")
        parser.add_argument("--box", default="12 Bottle")
        parser.add_argument("--bottles", type=int, default=12)
        parser.add_argument("--void", metavar="TRACKING")
        parser.add_argument("--out", default=str(Path(settings.BASE_DIR) / "label_output"))

    def handle(self, *args, **opts):
        if settings.SHIPPING["GLS"]["ENV"] != "sandbox":
            raise CommandError("GLS_ENV is not 'sandbox' -- this command only buys test labels.")
        try:
            adapter = get_adapter("gls")
        except CarrierError as e:
            raise CommandError(str(e))
        if opts["check"]:
            try:
                adapter._token()
            except CarrierError as e:
                raise CommandError(f"GLS refused the credentials: {e}")
            self.stdout.write(self.style.SUCCESS("OK -- GLS issued a token. Run gls_test_label."))
            return
        if opts["void"]:
            label = Label.objects.filter(tracking_number=opts["void"], carrier="gls").first()
            if label is None:
                raise CommandError(f"No recorded GLS label {opts['void']}.")
            try:
                void_label(label, adapter=adapter)
            except Exception as e:  # CarrierError or LabelRefused
                raise CommandError(f"Void failed: {e}")
            self.stdout.write(self.style.SUCCESS(f"Voided {opts['void']}."))
            return

        ship_from = settings.SHIPPING["SHIP_FROM"]
        if not ship_from.get("phone"):
            raise CommandError("Set SHIP_FROM_PHONE in .env.")
        try:
            box = BoxSize.objects.get(name=opts["box"])
        except BoxSize.DoesNotExist:
            raise CommandError(f"No box {opts['box']!r}.")
        bw = BottleWeight.objects.get(ml=750)
        weight = box.declared_weight([bw.weight_lb] * min(opts["bottles"], box.max_bottles))
        req = LabelRequest(
            ship_from=Address.from_settings(ship_from), ship_to=Address(**TEST_RECIPIENT),
            parcel=Parcel(box.length_in, box.width_in, box.height_in, weight,
                          reference="GLS-TEST"),
            service=default_service(False), ship_date=dt.date.today())
        try:
            label = buy_label(req, carrier="gls", adapter=adapter)
        except CarrierError as e:
            detail = f"\n{e.details}" if e.details else ""
            raise CommandError(f"GLS refused the label: {e}{detail}")
        out = Path(opts["out"])
        out.mkdir(parents=True, exist_ok=True)
        self.stdout.write(self.style.SUCCESS(
            f"GLS test label {label.tracking_number} · {box} · {weight} lb"))
        for doc in label.documents.all():
            p = out / f"gls_test_{label.tracking_number}_{doc.sequence}.pdf"
            p.write_bytes(bytes(doc.content))
            self.stdout.write(f"  {p}")
        self.stdout.write(f"Void it with: python manage.py gls_test_label --void {label.tracking_number}")
