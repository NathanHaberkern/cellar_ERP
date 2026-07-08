"""
Serializers for the Cellar API.

IMPORTANT (the one place this package needs your real code to finish):
Every ModelSerializer below uses `fields = "__all__"`, so it reflects whatever
fields actually exist on the model at runtime -- I did NOT hand-enumerate columns,
because the model source wasn't in the handoff package. That means these bind
correctly as-is. The only thing to verify per model is the import path in the
`from cellar.models.<module> import ...` lines: the handoff documents which module
each model lives in, and that's what I used. If your models/__init__.py re-exports
everything you could simplify these to `from cellar.models import ...`, but the
submodule imports are more precise and will work either way.

The request/response serializers at the bottom (LotCreate, Redesignate) DO name
fields explicitly, because they mirror the documented service signatures in
cellar/services/generator.py -- confirm the argument names against those functions.
"""

from rest_framework import serializers

# --- reference masters (editable CRUD) ---------------------------------------
from cellar.models.base import Program
from cellar.models.reference import (
    Variety,
    Grower,
    Vineyard,
    Block,
    VarietalDesignation,
    Vessel,
    Additive,
    LabAnalyte,
    ConfigConstant,
)

# --- spine (read; lots are minted via services, not raw create) --------------
from cellar.models.spine import (
    HarvestEvent,
    WeighTag,
    Lot,
    LotDesignation,
)

# --- crush-out / tax reference ------------------------------------------------
from cellar.models.crushout import TaxClass

# --- aging reference ----------------------------------------------------------
from cellar.models.aging import Room, Location, Rack, Container, BarrelOrder

# --- bottling reference -------------------------------------------------------
from cellar.models.bottling import BottleFormat, DryGood

# --- reporting reference ------------------------------------------------------
from cellar.models.reporting import Material


class _Base(serializers.ModelSerializer):
    """Shared base so behaviour (e.g. future audit-field handling) lives in one
    place. `fields = "__all__"` is set on each concrete subclass."""

    class Meta:
        abstract = True


# ----------------------- reference master serializers ------------------------
class VarietySerializer(_Base):
    class Meta:
        model = Variety
        fields = "__all__"


class GrowerSerializer(_Base):
    class Meta:
        model = Grower
        fields = "__all__"


class VineyardSerializer(_Base):
    class Meta:
        model = Vineyard
        fields = "__all__"


class BlockSerializer(_Base):
    class Meta:
        model = Block
        fields = "__all__"


class VarietalDesignationSerializer(_Base):
    class Meta:
        model = VarietalDesignation
        fields = "__all__"


class VesselSerializer(_Base):
    class Meta:
        model = Vessel
        fields = "__all__"


class AdditiveSerializer(_Base):
    class Meta:
        model = Additive
        fields = "__all__"


class LabAnalyteSerializer(_Base):
    class Meta:
        model = LabAnalyte
        fields = "__all__"


class ConfigConstantSerializer(_Base):
    class Meta:
        model = ConfigConstant
        fields = "__all__"


class TaxClassSerializer(_Base):
    class Meta:
        model = TaxClass
        fields = "__all__"


class RoomSerializer(_Base):
    class Meta:
        model = Room
        fields = "__all__"


class LocationSerializer(_Base):
    class Meta:
        model = Location
        fields = "__all__"


class RackSerializer(_Base):
    class Meta:
        model = Rack
        fields = "__all__"


class ContainerSerializer(_Base):
    class Meta:
        model = Container
        fields = "__all__"


class BarrelOrderSerializer(_Base):
    class Meta:
        model = BarrelOrder
        fields = "__all__"


class BottleFormatSerializer(_Base):
    class Meta:
        model = BottleFormat
        fields = "__all__"


class DryGoodSerializer(_Base):
    class Meta:
        model = DryGood
        fields = "__all__"


class MaterialSerializer(_Base):
    class Meta:
        model = Material
        fields = "__all__"


# ----------------------- spine (read-oriented) -------------------------------
class HarvestEventSerializer(_Base):
    class Meta:
        model = HarvestEvent
        fields = "__all__"


class WeighTagSerializer(_Base):
    class Meta:
        model = WeighTag
        fields = "__all__"


class LotDesignationSerializer(_Base):
    class Meta:
        model = LotDesignation
        fields = "__all__"


class LotSerializer(_Base):
    """List/detail read serializer for lots. `code` is Lot's derived property
    (rendered from current_designation) — surfaced explicitly since fields=__all__
    only covers columns, and the human-readable code is what clients display."""

    code = serializers.ReadOnlyField()

    class Meta:
        model = Lot
        fields = "__all__"


# ----------------------- service action inputs -------------------------------
class LotCreateSerializer(serializers.Serializer):
    """
    Input for POST /api/lots/  -> generator.create_lot(
        vintage, variety, program, block=None, vineyard=None,
        status=Lot.Status.RECEIVING, production_intent="", override_code=None)

    variety/block/vineyard are PrimaryKeyRelatedFields so they validate existence
    and hand the service real instances. program is the Program enum.
    """

    vintage = serializers.IntegerField()
    variety = serializers.PrimaryKeyRelatedField(queryset=Variety.objects.all())
    program = serializers.ChoiceField(choices=Program.choices)
    block = serializers.PrimaryKeyRelatedField(
        queryset=Block.objects.all(), required=False, allow_null=True)
    vineyard = serializers.PrimaryKeyRelatedField(
        queryset=Vineyard.objects.all(), required=False, allow_null=True)
    production_intent = serializers.CharField(required=False, allow_blank=True)
    override_code = serializers.CharField(required=False, allow_blank=True)


class RedesignateSerializer(serializers.Serializer):
    """
    Input for POST /api/lots/{id}/redesignate/
    -> generator.redesignate(lot, variety, program, block=None, vineyard=None)
    """

    variety = serializers.PrimaryKeyRelatedField(queryset=Variety.objects.all())
    program = serializers.ChoiceField(choices=Program.choices)
    block = serializers.PrimaryKeyRelatedField(
        queryset=Block.objects.all(), required=False, allow_null=True)
    vineyard = serializers.PrimaryKeyRelatedField(
        queryset=Vineyard.objects.all(), required=False, allow_null=True)


class VoidSerializer(serializers.Serializer):
    """Input for the void action on append-only rows (mirrors the admin 'Void
    selected' action). Reason is optional but recommended for the audit trail."""

    reason = serializers.CharField(required=False, allow_blank=True)
