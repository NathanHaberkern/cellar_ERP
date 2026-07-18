"""
Aging tranche — containers, racks, placements, topping, cost.

Key decisions encoded:
  * one container registry for tank / drum / barrel / foudre — uniform "where is this lot"
  * barrel ID is permanent identity; location lives on a separate rack assignment
  * oak tier snapshotted at fill (fill 1=New, 2=1st use, 3=2nd use, 4+=Neutral)
  * topping always draws a tracked source lot; routine books evaporative loss,
    partial-fill books none; foreign wine >5 gal cumulative flags the barrel until rack-out
  * order-level fees (delivery + FX/bank) allocated pro-rata by base price → landed USD cost
"""
from decimal import Decimal

from django.db import models, transaction
from .base import AppendOnly
from .spine import LotLineage


class OakTier(models.TextChoices):
    NEW = "new", "New"
    FIRST = "first_use", "1st use"
    SECOND = "second_use", "2nd use"
    NEUTRAL = "neutral", "Neutral"
    NONE = "none", "None (non-oak)"


class Room(models.Model):
    name = models.CharField(max_length=80, unique=True, help_text="e.g. Old Barrel Room")
    notes = models.TextField(blank=True)

    def __str__(self):
        return self.name


class Location(models.Model):
    """A row/column within one room — the unit you batch by (e.g. OBC1)."""
    room = models.ForeignKey(Room, on_delete=models.PROTECT, related_name="locations")
    code = models.CharField(max_length=30, unique=True, help_text="row/column code, e.g. OBC1")

    def __str__(self):
        return self.code


class BarrelOrder(models.Model):
    supplier = models.CharField(max_length=120)
    order_date = models.DateField()
    currency = models.CharField(max_length=3, default="EUR")
    fx_rate_to_usd = models.DecimalField(max_digits=8, decimal_places=5, default=Decimal("1"))
    bank_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0,
                                   help_text="order currency; FX/bank conversion cost")
    delivery_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0,
                                       help_text="order currency")

    def total_base(self):
        return sum((c.base_price or 0 for c in self.containers.all()), Decimal(0))

    def __str__(self):
        return f"{self.supplier} {self.order_date}"


class Container(models.Model):
    class Type(models.TextChoices):
        TANK = "tank", "Tank"
        SS_DRUM = "ss_drum", "SS drum"
        BARREL = "barrel", "Barrel"
        FOUDRE = "foudre", "Foudre"

    class Pool(models.TextChoices):
        TABLE = "table", "Table wine"
        PORT = "port", "Port"

    container_id = models.CharField(max_length=30, unique=True, help_text="permanent ID, e.g. 2501")
    type = models.CharField(max_length=10, choices=Type.choices)
    capacity_gal = models.DecimalField(max_digits=7, decimal_places=1)
    # Dedicated barrel pool — a port barrel stays a port barrel even when empty.
    # Blank for tanks. Gates the empty-barrel picker; a rack is pool-homogeneous.
    pool = models.CharField(max_length=8, choices=Pool.choices, blank=True)
    barcode = models.CharField(max_length=60, blank=True)
    active = models.BooleanField(default=True)
    # oak attributes (blank for tank / drum)
    format = models.CharField(max_length=40, blank=True, help_text="e.g. '60 gal', '130 gal', 'foudre'")
    origin = models.CharField(max_length=60, blank=True)
    forest = models.CharField(max_length=60, blank=True)
    cooper = models.CharField(max_length=80, blank=True)
    toast = models.CharField(max_length=40, blank=True)
    head_toast = models.CharField(max_length=40, blank=True)
    grain = models.CharField(max_length=40, blank=True)
    year_made = models.PositiveSmallIntegerField(null=True, blank=True)
    # cost
    order = models.ForeignKey(BarrelOrder, null=True, blank=True, on_delete=models.PROTECT,
                              related_name="containers")
    base_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True,
                                     help_text="order currency")

    @property
    def is_oak(self):
        return self.type in (self.Type.BARREL, self.Type.FOUDRE)

    @property
    def fill_count(self):
        return self.placements.count()

    def current_placement(self):
        return self.placements.filter(emptied_at__isnull=True, voided_at__isnull=True).order_by("-filled_at").first()

    def current_lot(self):
        p = self.current_placement()
        return p.lot if p else None

    def current_occupant_label(self):
        """What's in the barrel now, for display: the lot code, else an imported
        legacy label ("00PORT"), else None (empty)."""
        p = self.current_placement()
        if not p:
            return None
        return p.lot.code if p.lot_id else (p.legacy_lot_code or None)

    def effective_location(self):
        """A barrel is wherever its rack is — location is never on the barrel."""
        a = self.current_rack_assignment()
        return a.rack.location if (a and a.rack.location_id) else None

    def landed_cost_usd(self):
        if not self.order or self.base_price is None:
            return None
        o = self.order
        total_base = o.total_base()
        share = (self.base_price / total_base) if total_base else 0
        fees = (o.bank_fee + o.delivery_fee) * share
        return ((self.base_price + fees) * o.fx_rate_to_usd).quantize(Decimal("0.01"))

    def current_rack_assignment(self):
        return self.rack_assignments.filter(removed_at__isnull=True).order_by("-assigned_at").first()

    def __str__(self):
        return self.container_id


class Rack(models.Model):
    rack_id = models.CharField(max_length=20, unique=True, help_text="e.g. R001 — distinct from barrel IDs")
    location = models.ForeignKey(Location, null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="racks", help_text="row/column this rack sits in")
    positions = models.PositiveSmallIntegerField(default=2)
    size_class = models.CharField(
        max_length=10, default="standard",
        choices=[("standard", "Standard (55/60/70)"), ("large", "Large (130)")],
        help_text="which barrel size this rack holds")
    barcode = models.CharField(max_length=60, blank=True)

    def occupants(self):
        """position -> container, for the current assignments."""
        out = {}
        for a in RackAssignment.objects.filter(rack=self, removed_at__isnull=True):
            out[a.position] = a.container
        return out

    def current_lots(self):
        """Distinct lots currently held on this rack."""
        lots = {}
        for c in self.occupants().values():
            lot = c.current_lot()
            if lot:
                lots[lot.id] = lot
        return list(lots.values())

    @property
    def is_split(self):
        return len(self.current_lots()) > 1

    def __str__(self):
        return self.rack_id


class RackAssignment(AppendOnly):
    CLOSE_FIELDS = ("removed_at",)
    container = models.ForeignKey(Container, on_delete=models.PROTECT, related_name="rack_assignments")
    rack = models.ForeignKey(Rack, on_delete=models.PROTECT, related_name="assignments")
    position = models.PositiveSmallIntegerField(help_text="1 = left/lower")
    assigned_at = models.DateTimeField()
    removed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.container} @ {self.rack} pos {self.position}"


class AgingPlacement(AppendOnly):
    """Time a lot spends in a container. A mid-aging move = empty one, open the next."""
    CLOSE_FIELDS = ("emptied_at",)
    lot = models.ForeignKey("cellar.Lot", on_delete=models.PROTECT, related_name="placements",
                            null=True, blank=True)
    container = models.ForeignKey(Container, on_delete=models.PROTECT, related_name="placements")
    filled_at = models.DateField()
    emptied_at = models.DateField(null=True, blank=True)
    volume_gal = models.DecimalField(max_digits=8, decimal_places=1)
    fill_number = models.PositiveSmallIntegerField(null=True, blank=True, help_text="snapshot at fill")
    oak_tier = models.CharField(max_length=12, choices=OakTier.choices, blank=True, help_text="snapshot")
    # Imported/legacy occupancy: a historical wine label ("00PORT") for a barrel
    # whose lot predates the system. Displayed in the Oak view; not a Lot FK.
    # Real placements (racked via the app) leave this blank and use `lot`.
    legacy_lot_code = models.CharField(max_length=40, blank=True)

    def save(self, *args, **kwargs):
        creating = self._state.adding and not self.pk
        if creating and self.fill_number is None:
            prior = AgingPlacement.objects.filter(container=self.container).count()
            self.fill_number = prior + 1
        if creating and not self.oak_tier:
            self.oak_tier = self._tier_for(self.container, self.fill_number)
        super().save(*args, **kwargs)

    @staticmethod
    def _tier_for(container, fill_number):
        if not container.is_oak:
            return OakTier.NONE
        return {1: OakTier.NEW, 2: OakTier.FIRST, 3: OakTier.SECOND}.get(fill_number, OakTier.NEUTRAL)

    def duration_days(self, asof=None):
        from datetime import date
        end = self.emptied_at or asof or date.today()
        return max((end - self.filled_at).days, 0)

    @property
    def foreign_topped_gal(self):
        total = Decimal(0)
        for t in self.toppings.filter(voided_at__isnull=True):
            if t.event.source_lot_id != self.lot_id:
                total += t.volume_added
        return total

    @property
    def is_flagged(self):
        return self.emptied_at is None and self.foreign_topped_gal > 5

    def __str__(self):
        return f"{self.lot} in {self.container} ({self.volume_gal} gal)"


class VolumeLoss(AppendOnly):
    lot = models.ForeignKey("cellar.Lot", on_delete=models.PROTECT, related_name="volume_losses")
    volume_gal = models.DecimalField(max_digits=8, decimal_places=1)
    reason = models.CharField(max_length=60)
    occurred_at = models.DateField()

    def __str__(self):
        return f"{self.lot} −{self.volume_gal} ({self.reason})"


class ToppingEvent(AppendOnly):
    class Kind(models.TextChoices):
        ROUTINE = "routine", "Routine (replace evaporation)"
        PARTIAL_FILL = "partial_fill", "Partial-barrel ullage fill"

    source_lot = models.ForeignKey("cellar.Lot", on_delete=models.PROTECT, related_name="topping_sources")
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.ROUTINE)
    topped_at = models.DateField()

    def __str__(self):
        return f"top from {self.source_lot} ({self.get_kind_display()}) {self.topped_at}"


class ToppingTarget(AppendOnly):
    """One barrel topped in an event. Books evaporative loss (routine) and, when the
    source differs from the barrel's lot, a foreign contribution edge for composition."""
    event = models.ForeignKey(ToppingEvent, on_delete=models.PROTECT, related_name="targets")
    placement = models.ForeignKey(AgingPlacement, on_delete=models.PROTECT, related_name="toppings")
    volume_added = models.DecimalField(max_digits=7, decimal_places=1)
    evaporative_loss = models.DecimalField(max_digits=7, decimal_places=1, null=True, blank=True)
    contribution = models.ForeignKey(LotLineage, null=True, blank=True,
                                     on_delete=models.PROTECT, related_name="+")
    loss = models.ForeignKey(VolumeLoss, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    def save(self, *args, **kwargs):
        creating = self._state.adding and not self.pk
        if creating:
            ev = self.event
            target_lot = self.placement.lot
            self.evaporative_loss = self.volume_added if ev.kind == ToppingEvent.Kind.ROUTINE else Decimal(0)
            with transaction.atomic():
                if self.evaporative_loss and self.evaporative_loss > 0:
                    self.loss = VolumeLoss.objects.create(
                        lot=target_lot, volume_gal=self.evaporative_loss,
                        reason="topping evaporation", occurred_at=ev.topped_at)
                if ev.source_lot_id != target_lot.id:
                    self.contribution = LotLineage.objects.create(
                        parent_lot=ev.source_lot, child_lot=target_lot,
                        relationship_type=LotLineage.Relationship.TOPPING,
                        volume_gal=self.volume_added)
                super().save(*args, **kwargs)
        else:
            super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.event} → {self.placement.container} ({self.volume_added} gal)"
