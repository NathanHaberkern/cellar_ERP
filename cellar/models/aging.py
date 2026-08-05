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

    def _foreign_gal_by_kind(self, kind):
        total = Decimal(0)
        for t in self.toppings.filter(voided_at__isnull=True):
            if t.event.source_lot_id != self.lot_id and t.event.kind == kind:
                total += t.volume_added
        return total

    @property
    def routine_topped_gal(self):
        """Foreign wine added by ROUTINE topping — the creep the 5-gal flag watches."""
        return self._foreign_gal_by_kind(ToppingEvent.Kind.ROUTINE)

    @property
    def partial_fill_gal(self):
        """Foreign wine added by deliberate PARTIAL_FILL ullage fills.

        Tracked apart from routine topping on purpose. Both are real wine and
        both count toward composition (see foreign_topped_gal, and the lineage
        edges ToppingTarget writes) — labelling eligibility does not care what
        you meant. But the 5-gallon flag exists to catch routine topping
        QUIETLY ACCUMULATING past a material threshold, and a single recorded
        35-gallon ullage fill is not that: it would trip the flag on day one
        and then mask any genuine creep afterwards. Separating the accumulators
        keeps the flag measuring the thing it was built to measure, without
        pretending the fill wine isn't there.
        """
        return self._foreign_gal_by_kind(ToppingEvent.Kind.PARTIAL_FILL)

    @property
    def foreign_topped_gal(self):
        """ALL foreign wine in this barrel, both kinds. This is the composition
        number — unchanged in meaning from before the accumulators were split."""
        return self.routine_topped_gal + self.partial_fill_gal

    @property
    def is_flagged(self):
        """Routine topping creep past 5 gal. Deliberate ullage fills are
        excluded — see partial_fill_gal for why."""
        return self.emptied_at is None and self.routine_topped_gal > 5

    @property
    def has_partial_fill(self):
        """Surfaced separately so a barrel carrying a deliberate fill is still
        visible in the UI without being mislabelled as topping contamination."""
        return self.emptied_at is None and self.partial_fill_gal > 0

    def __str__(self):
        return f"{self.lot} in {self.container} ({self.volume_gal} gal)"


class VolumeLoss(AppendOnly):
    lot = models.ForeignKey("cellar.Lot", on_delete=models.PROTECT, related_name="volume_losses")
    volume_gal = models.DecimalField(max_digits=8, decimal_places=1)
    reason = models.CharField(max_length=60)
    occurred_at = models.DateField()
    # NORMAL vs ABNORMAL (migration 0030)
    #
    # Normal loss — lees, racking, evaporation, filtration, sampling, topping — is
    # CAPITALIZED into the wine that survives. No code needed: costing.cost_basis_volume()
    # divides an unchanged dollar total by a smaller balance, so cost/gal simply rises.
    #
    # Abnormal loss — a tank leak, a barrel failure, a contamination requiring
    # destruction — is EXPENSED. Flagging it here posts a credit against the lot and a
    # matching ABNORMAL_LOSS expense row, so the surviving wine does NOT absorb it.
    #
    # A flag rather than a gallons or percentage threshold, on purpose: whether a loss
    # was abnormal is a judgement about what happened, not about how big it was. A
    # 3-gallon loss to a cracked barrel head is abnormal; a 30-gallon evaporation over
    # a summer is not.
    is_abnormal = models.BooleanField(
        default=False,
        help_text="expense this loss instead of capitalizing it into the remaining wine")

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
                    # Imported inside save() on purpose: cellar.services.costing
                    # imports back into cellar.models, so a module-level import
                    # here is circular.
                    from cellar.services import costing as costing_svc
                    cpg = costing_svc.parent_cost_per_gal(ev.source_lot)
                    # The ROUTINE/PARTIAL_FILL distinction already exists on the
                    # event; before this it was flattened to TOPPING here and
                    # lost to every downstream reader. Carrying it through is
                    # what lets partx.py report a large cross-tax-class fill
                    # while still skipping de minimis routine topping.
                    rel = (LotLineage.Relationship.PARTIAL_FILL
                           if ev.kind == ToppingEvent.Kind.PARTIAL_FILL
                           else LotLineage.Relationship.TOPPING)
                    self.contribution = LotLineage.objects.create(
                        parent_lot=ev.source_lot, child_lot=target_lot,
                        relationship_type=rel,
                        volume_gal=self.volume_added,
                        occurred_at=costing_svc.to_business_date(ev.topped_at),
                        cost_per_gal_snapshot=cpg)
                super().save(*args, **kwargs)
        else:
            super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.event} → {self.placement.container} ({self.volume_added} gal)"


class LotReservation(AppendOnly):
    """Wine set aside for a future sale, bottling blend, or program.

    WHY THE CONTAINER IS OPTIONAL RATHER THAN REQUIRED
    ---------------------------------------------------
    Three real granularities, one model:

      container set, one row per barrel  — a barrel selection ("these 12 of the
          40 barrels of 21TN1"). Barrel identity is not incidental here: the
          Container model carries cooper/forest/toast/grain precisely because
          which barrel a wine sat in changes what it is, and selecting barrels
          for a reserve bottling is the entire practice.
      container null, volume_gal set     — "500 gallons of this lot, barrels to
          be chosen." The common case when a wine is committed before it has
          been tasted through.
      container null, volume_gal null    — the whole lot is spoken for. This is
          the shape the 2019 TN_NW / TEMP_NW Naked Wines bulk blends needed and
          had nowhere to live, which is what surfaced this gap.

    WHY NOT ON Container ALONE: a barrel is emptied and refilled next vintage,
    and a reservation anchored only to the container would silently carry over
    onto unrelated wine. The thing reserved is THIS WINE IN THIS BARREL, so the
    lot is always required and the container narrows it.

    NOT the same axis as Lot.program (table/port/rose), which is TTB tax
    classification. This is a commercial commitment and carries no tax meaning.
    """
    class Purpose(models.TextChoices):
        SALE = "sale", "Sale to a buyer"
        BLEND = "blend", "Future bottling blend"
        HOLD = "hold", "Held / library"

    lot = models.ForeignKey("cellar.Lot", on_delete=models.PROTECT, related_name="reservations")
    placement = models.ForeignKey(AgingPlacement, null=True, blank=True,
                                  on_delete=models.PROTECT, related_name="reservations",
                                  help_text="specific barrel; null = lot-level reservation")
    purpose = models.CharField(max_length=8, choices=Purpose.choices, default=Purpose.SALE)
    destination = models.ForeignKey("cellar.ExternalDestination", null=True, blank=True,
                                    on_delete=models.PROTECT, related_name="reservations",
                                    help_text="the buyer, where one is known")
    label = models.CharField(max_length=80, blank=True,
                             help_text="program name when it isn't a buyer yet, e.g. 'Naked Wines'")
    volume_gal = models.DecimalField(max_digits=9, decimal_places=1, null=True, blank=True,
                                     help_text="lot-level partial reservation; null with no "
                                               "placement = the whole lot")
    reserved_at = models.DateField()
    released_at = models.DateField(null=True, blank=True,
                                   help_text="released without being fulfilled")
    satisfied_at = models.DateField(null=True, blank=True,
                                    help_text="closed because the wine was blended away")

    # Closing a reservation is a legitimate post-creation edit; the commitment
    # itself is not. AppendOnly keeps released/satisfied rows visible, which is
    # the point — if a buyer disputes what was set aside, the history is there.
    CLOSE_FIELDS = ("released_at", "satisfied_at")

    @property
    def is_open(self):
        return self.released_at is None and self.satisfied_at is None and self.voided_at is None

    def __str__(self):
        who = self.destination or self.label or self.get_purpose_display()
        where = self.placement.container if self.placement else self.lot
        return f"{where} reserved for {who}"
