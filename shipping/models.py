"""Shipping data model.

What is append-only and what isn't, and why:

* Label and FulfillmentPost inherit cellar's AppendOnly. A label is money spent
  with a carrier and a tracking number a customer may already have; a fulfillment
  post is an email already sent. Neither may be edited or deleted -- a wrong label
  is voided at the carrier and marked voided here, then a new one is bought.
* ShipOrder, PackPool and Package are working state (status moves forward as the
  run is packed), so they are ordinary mutable models.
* BoxSize, BottleWeight and SkuShippingProfile are reference data Nate maintains.

Carrier credentials and account numbers are NOT stored here. They live in
environment variables (.env locally, Heroku config vars in production) and are
read through settings.SHIPPING, so the database never holds a secret and a
database copy can be handed to anyone without rotating keys.
"""
from decimal import Decimal

from django.conf import settings
from django.db import models

from cellar.models.base import AppendOnly

from .permissions import CUSTOM_PERMISSIONS


class Carrier(models.TextChoices):
    FEDEX = "fedex", "FedEx"
    UPS = "ups", "UPS"
    GLS = "gls", "GLS"


class CarrierEnvironment(models.TextChoices):
    SANDBOX = "sandbox", "Sandbox (test)"
    PRODUCTION = "production", "Production"


# --- reference data ---------------------------------------------------------

class BoxSize(models.Model):
    """A shipper box. Seeded from Nate's box table (migration 0002)."""

    class Use(models.TextChoices):
        STANDARD = "standard", "Table wine & port"
        PORT_CLUB = "port_club", "Port club shipments only"
        MAGNUM = "magnum", "Magnums only"

    name = models.CharField(max_length=40, unique=True)
    use = models.CharField(max_length=20, choices=Use.choices, default=Use.STANDARD,
                           help_text="Port club box is used only for 'Port Club Volume' line "
                                     "items; magnum box only for magnums.")
    length_in = models.DecimalField(max_digits=5, decimal_places=2)
    width_in = models.DecimalField(max_digits=5, decimal_places=2)
    height_in = models.DecimalField(max_digits=5, decimal_places=2)
    tare_lb = models.DecimalField(max_digits=5, decimal_places=2,
                                  help_text="empty packed box: shipper + inserts")
    max_bottles = models.PositiveSmallIntegerField()
    max_weight_lb = models.DecimalField(max_digits=5, decimal_places=2,
                                        help_text="declared weight is capped here")
    active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "max_bottles", "name"]

    def __str__(self):
        return self.name

    @property
    def dimensions_display(self):
        return f"{self.length_in:g} × {self.width_in:g} × {self.height_in:g} in"

    def declared_weight(self, bottle_weights_lb):
        """Tare + each bottle's weight, capped at the box's max weight.

        The cap exists because the max weights are Nate's measured full-box
        weights; if the per-bottle figures ever drift above reality we'd
        rather declare the known full weight than overstate it to the carrier.
        """
        total = self.tare_lb + sum((Decimal(w) for w in bottle_weights_lb), Decimal("0"))
        return min(total, self.max_weight_lb)


class BottleWeight(models.Model):
    """Packed weight of one bottle, by fill volume (750 ml wine, 500 ml port, magnum)."""
    ml = models.PositiveIntegerField(unique=True)
    label = models.CharField(max_length=40)
    weight_lb = models.DecimalField(max_digits=5, decimal_places=2)

    class Meta:
        ordering = ["ml"]

    def __str__(self):
        return f"{self.label} — {self.weight_lb} lb"

    @property
    def is_magnum(self):
        return self.ml >= MAGNUM_MIN_ML


MAGNUM_MIN_ML = 1500


class SkuShippingProfile(models.Model):
    """Manual override of a SKU's bottle size.

    Normally the size comes from the SKU's BottleFormat in the cellar's
    bottling runs. Use this for SKUs that were never bottled in the ERP
    (historical stock, merch, gift sets) or where the format is wrong.
    """
    sku = models.CharField(max_length=60, unique=True)
    ml = models.PositiveIntegerField(help_text="bottle size in ml (750, 500, 1500)")
    bottles_per_unit = models.PositiveSmallIntegerField(
        default=1, help_text="bottles in one unit of this SKU (e.g. 3 for a 3-pack)")
    notes = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["sku"]

    def __str__(self):
        return f"{self.sku}: {self.bottles_per_unit} × {self.ml} ml"


# --- orders -----------------------------------------------------------------

class ShipOrder(models.Model):
    """One order that ships, with the ship-to frozen at sync time.

    Most orders come from Commerce7 (DtC). Wholesale orders whose QuickBooks
    invoice / sales receipt has a GLS ship method come from QBO (source=qbo);
    for those, `c7_order_id` holds "qbo:<Invoice|SalesReceipt>:<QBO Id>" (it's
    the row's unique external key, named before QBO orders existed) and
    `c7_order_number` holds the QBO DocNumber.

    The address is a snapshot on purpose: once a label is bought, a later edit
    in C7 or QBO must not silently change what this row says the label was for.
    A changed address shows up as a mismatch on re-sync instead.
    """

    class Source(models.TextChoices):
        C7 = "c7", "Commerce7"
        QBO = "qbo", "QuickBooks"

    class Status(models.TextChoices):
        NEW = "new", "New"
        ADDRESS_REVIEW = "address_review", "Address needs review"
        READY = "ready", "Ready to pool"
        POOLED = "pooled", "In a pool"
        LABELED = "labeled", "Labeled"
        FULFILLED = "fulfilled", "Fulfilled in C7"
        ON_HOLD = "on_hold", "On hold"
        CANCELLED = "cancelled", "Cancelled"

    source = models.CharField(max_length=4, choices=Source.choices, default=Source.C7,
                              db_index=True)
    c7_order_id = models.CharField(max_length=64, unique=True,
                                   help_text="C7 order id, or qbo:<type>:<id> for QBO orders")
    c7_order_number = models.CharField(max_length=32, db_index=True)
    ordered_at = models.DateTimeField(null=True, blank=True)

    ship_name = models.CharField(max_length=120)
    ship_company = models.CharField(max_length=120, blank=True)
    ship_street1 = models.CharField(max_length=120)
    ship_street2 = models.CharField(max_length=120, blank=True)
    ship_city = models.CharField(max_length=80)
    ship_state = models.CharField(max_length=2)
    ship_postal_code = models.CharField(max_length=10)
    ship_country = models.CharField(max_length=2, default="US")
    ship_phone = models.CharField(max_length=30, blank=True)
    ship_email = models.EmailField(blank=True)
    ship_residential = models.BooleanField(null=True, blank=True,
                                           help_text="from address validation; blank = unknown")
    address_validated_at = models.DateTimeField(null=True, blank=True)

    # Pre-validation address, kept when FedEx's suggestion is accepted or the
    # address is edited by hand, so the change is never silent.
    address_original = models.JSONField(default=dict, blank=True)
    address_suggested = models.JSONField(default=dict, blank=True,
                                         help_text="FedEx's resolved address, if different")
    address_issues = models.JSONField(default=list, blank=True,
                                      help_text="why this address is in review")
    address_classification = models.CharField(max_length=20, blank=True,
                                              help_text="FedEx: RESIDENTIAL / BUSINESS / MIXED / UNKNOWN")
    address_reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                            on_delete=models.PROTECT, related_name="+")

    c7_customer_id = models.CharField(max_length=64, blank=True, db_index=True)
    is_club = models.BooleanField(default=False, help_text="shown as the Club tag")
    club_name = models.CharField(max_length=120, blank=True)
    club_package = models.CharField(max_length=120, blank=True,
                                    help_text="C7 club package, e.g. 'Fall 2026 (4-Bottle)'")
    requested_ship_date = models.DateField(null=True, blank=True,
                                           help_text="customer's requested ship date; held until then")
    compliance_status = models.CharField(max_length=40, blank=True,
                                         help_text="as reported by Commerce7 / VinoCheck")
    compliance_override_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                               on_delete=models.PROTECT, related_name="+")
    compliance_override_at = models.DateTimeField(null=True, blank=True)
    compliance_override_note = models.CharField(max_length=200, blank=True)
    hold_note = models.CharField(max_length=200, blank=True,
                                 help_text="why it was put on hold by hand")
    c7_change_note = models.CharField(
        max_length=200, blank=True,
        help_text="set when C7 changed the order after it was pooled or labeled")

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEW,
                              db_index=True)
    carrier = models.CharField(max_length=10, choices=Carrier.choices, blank=True)
    carrier_reason = models.CharField(max_length=120, blank=True,
                                      help_text="why this carrier, e.g. 'FedEx-restricted state'")
    carrier_override = models.CharField(
        max_length=10, choices=Carrier.choices, blank=True,
        help_text="force a carrier when the rules can't tell (e.g. a UPS Store address "
                  "with no store name). FedEx is never forced into a restricted state.")

    c7_payload = models.JSONField(default=dict, blank=True,
                                  help_text="raw C7 order as last synced, for audit")
    synced_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-ordered_at", "-id"]
        permissions = CUSTOM_PERMISSIONS

    def __str__(self):
        return f"#{self.c7_order_number} {self.ship_name}"

    ADDRESS_FIELDS = ("ship_name", "ship_company", "ship_street1", "ship_street2", "ship_city",
                      "ship_state", "ship_postal_code", "ship_country")

    def address_dict(self):
        return {f: getattr(self, f) for f in self.ADDRESS_FIELDS}

    @property
    def bottle_count(self):
        return sum(line.bottle_count for line in self.lines.all())

    @property
    def is_qbo(self):
        return self.source == self.Source.QBO

    @property
    def qbo_ref(self):
        """(entity, id) for a QBO order, e.g. ("Invoice", "17805")."""
        _, entity, txn_id = self.c7_order_id.split(":", 2)
        return entity, txn_id


class ShipOrderLine(models.Model):
    order = models.ForeignKey(ShipOrder, on_delete=models.CASCADE, related_name="lines")
    sku = models.CharField(max_length=60)
    title = models.CharField(max_length=200, blank=True)
    quantity = models.PositiveIntegerField()
    ml = models.PositiveIntegerField(null=True, blank=True,
                                     help_text="bottle size resolved at sync; blank = unknown")
    bottles_per_unit = models.PositiveSmallIntegerField(default=1)
    is_port_club = models.BooleanField(default=False,
                                       help_text="'Port Club Volume' item → Port box")

    def __str__(self):
        return f"{self.quantity} × {self.sku}"

    @property
    def bottle_count(self):
        return self.quantity * self.bottles_per_unit


# --- packing ----------------------------------------------------------------

class PackPool(models.Model):
    """A batch of ~20 cases packed and checked together, one carrier per pool."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        LABELED = "labeled", "Labeled"
        SHIPPED = "shipped", "Shipped"

    name = models.CharField(max_length=60)
    carrier = models.CharField(max_length=10, choices=Carrier.choices)
    is_default = models.BooleanField(default=False,
                                     help_text="the identical default-club-shipment pool")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.get_carrier_display()})"


class Package(models.Model):
    """One physical box. Orders to the same address share a package."""
    pool = models.ForeignKey(PackPool, null=True, blank=True, on_delete=models.PROTECT,
                             related_name="packages")
    box = models.ForeignKey(BoxSize, on_delete=models.PROTECT, related_name="+")
    orders = models.ManyToManyField(ShipOrder, related_name="packages")
    bottle_count = models.PositiveSmallIntegerField()
    weight_lb = models.DecimalField(max_digits=6, decimal_places=2,
                                    help_text="declared weight sent to the carrier")
    contents = models.JSONField(default=list, blank=True,
                                help_text="what goes in this box: [{order, sku, qty}]")
    sequence = models.PositiveSmallIntegerField(default=0, help_text="packing order in the pool")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["pool_id", "sequence", "id"]

    def __str__(self):
        return f"Package {self.pk} — {self.box} ({self.bottle_count} btl)"

    @property
    def live_label(self):
        return self.labels.filter(voided_at__isnull=True).first()


# --- carrier records (append-only) -------------------------------------------

class Label(AppendOnly):
    """A label bought from a carrier.

    package is nullable only for sandbox test labels made by the
    `fedex_test_label` command; every production label belongs to a package.
    """
    package = models.ForeignKey(Package, null=True, blank=True, on_delete=models.PROTECT,
                                related_name="labels")
    carrier = models.CharField(max_length=10, choices=Carrier.choices)
    environment = models.CharField(max_length=12, choices=CarrierEnvironment.choices)
    service = models.CharField(max_length=40)
    tracking_number = models.CharField(max_length=40, db_index=True)
    master_tracking_number = models.CharField(max_length=40, blank=True)
    net_charge = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    weight_lb = models.DecimalField(max_digits=6, decimal_places=2)
    response_summary = models.JSONField(default=dict, blank=True,
                                        help_text="carrier response with label images stripped")
    voided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                  on_delete=models.PROTECT, related_name="+")

    CLOSE_FIELDS = ("voided_by",)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_carrier_display()} {self.tracking_number}"


class LabelDocument(models.Model):
    """A printable image returned with a label.

    FedEx alcohol shipments can return more than one document per package, and
    every one of them has to go on the box -- so documents are rows, not a
    single field on Label. Stored in the database: at ~50-100 KB each and
    ~1,200 packages a year that's well under the Essential-0 limit.
    """
    label = models.ForeignKey(Label, on_delete=models.PROTECT, related_name="documents")
    kind = models.CharField(max_length=40, help_text="carrier's document type, e.g. LABEL")
    content_type = models.CharField(max_length=40, default="application/pdf")
    content = models.BinaryField()
    sequence = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["label_id", "sequence"]

    def __str__(self):
        return f"{self.label} {self.kind} #{self.sequence}"

    def save(self, *args, **kwargs):
        if self.pk and not self._state.adding:
            raise ValueError("LabelDocument is immutable once stored.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if getattr(settings, "ALLOW_HARD_DELETE", False):
            return super().delete(*args, **kwargs)
        raise ValueError("LabelDocument is immutable; void the label instead.")


class FulfillmentPost(AppendOnly):
    """One tracking write-back, logged whether it worked or not: a Commerce7
    fulfillment for C7 orders, or the TrackingNum/ShipDate update on the QBO
    invoice for QBO (GLS) orders -- response_summary["target"] says which.

    Keyed on order + tracking numbers so a retry after a timeout can check
    whether C7 already has the fulfillment before posting (and emailing) again.
    """
    order = models.ForeignKey(ShipOrder, on_delete=models.PROTECT, related_name="fulfillment_posts")
    carrier = models.CharField(max_length=10, choices=Carrier.choices)
    tracking_numbers = models.JSONField(default=list)
    send_email = models.BooleanField(default=True)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    succeeded = models.BooleanField(default=False)
    response_summary = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"C7 fulfillment #{self.order.c7_order_number} {self.tracking_numbers}"


class CarrierPickup(AppendOnly):
    """An on-call pickup request (GLS only so far), logged whether it worked or not.

    GLS doesn't come by daily: a pickup is booked on days a GLS pool has labels.
    One successful, unvoided pickup per carrier per day covers every pool --
    a second pool the same day is just more boxes for the same driver.
    """
    carrier = models.CharField(max_length=10, choices=Carrier.choices)
    pool = models.ForeignKey(PackPool, null=True, blank=True, on_delete=models.PROTECT,
                             related_name="pickups")
    pickup_date = models.DateField()
    packages = models.PositiveSmallIntegerField()
    weight_lb = models.DecimalField(max_digits=8, decimal_places=2)
    succeeded = models.BooleanField(default=False)
    confirmation = models.CharField(max_length=40, blank=True)
    response_summary = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_carrier_display()} pickup {self.pickup_date} {self.confirmation}"
