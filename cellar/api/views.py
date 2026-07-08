"""
Cellar API views — bound to the real cellar/services signatures.

1. ModelViewSets over reference masters -> CRUD.
2. Thin wrappers over cellar/services/* -> compliance/cost read-layer + rendered forms.

Lots mutate only through generator.create_lot / redesignate; the append-only ledger
is never exposed to PUT/PATCH/DELETE.
"""

import os
import tempfile
from datetime import date

from django.conf import settings
from django.http import HttpResponse

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from cellar.models.reference import (
    Variety, Grower, Vineyard, Block, VarietalDesignation,
    Vessel, Additive, LabAnalyte, ConfigConstant,
)
from cellar.models.spine import HarvestEvent, WeighTag, Lot
from cellar.models.crushout import TaxClass
from cellar.models.aging import Room, Location, Rack, Container, BarrelOrder
from cellar.models.bottling import BottleFormat, DryGood
from cellar.models.reporting import Material

from . import serializers as s
from .permissions import ReadOnlyOrStaff

from cellar.services import generator as gen
from cellar.services import aging as aging_svc
from cellar.services import costing as costing_svc
from cellar.services import reporting as reporting_svc
from cellar.services import excise as excise_svc
from cellar.services import crush_report as crush_svc
from cellar.services import forms as forms_svc


# --- winery header + template paths (override via settings / env) -------------
_WINERY = getattr(settings, "WINERY", {
    "EIN": "94-2275571", "REGISTRY": "BW-CA-5526",
    "NAME_ADDRESS": "St. Amant Winery, 1 Winemaster Way, Lodi, CA 95240",
})
_TPL_DIR = getattr(settings, "FORMS_TEMPLATE_DIR",
                   os.path.join(settings.BASE_DIR, "cellar", "forms_templates"))
_TPL_5120 = os.path.join(_TPL_DIR, "f5120_17.pdf")
_TPL_5000 = os.path.join(_TPL_DIR, "f5000_24.pdf")


# ============================================================================
# Reference master CRUD
# ============================================================================
class _RefViewSet(viewsets.ModelViewSet):
    permission_classes = [ReadOnlyOrStaff]

    def get_queryset(self):
        return self.serializer_class.Meta.model.objects.all().order_by("pk")


class VarietyViewSet(_RefViewSet): serializer_class = s.VarietySerializer
class GrowerViewSet(_RefViewSet): serializer_class = s.GrowerSerializer
class VineyardViewSet(_RefViewSet): serializer_class = s.VineyardSerializer
class BlockViewSet(_RefViewSet): serializer_class = s.BlockSerializer
class VarietalDesignationViewSet(_RefViewSet): serializer_class = s.VarietalDesignationSerializer
class VesselViewSet(_RefViewSet): serializer_class = s.VesselSerializer
class AdditiveViewSet(_RefViewSet): serializer_class = s.AdditiveSerializer
class LabAnalyteViewSet(_RefViewSet): serializer_class = s.LabAnalyteSerializer
class ConfigConstantViewSet(_RefViewSet): serializer_class = s.ConfigConstantSerializer
class TaxClassViewSet(_RefViewSet): serializer_class = s.TaxClassSerializer
class RoomViewSet(_RefViewSet): serializer_class = s.RoomSerializer
class LocationViewSet(_RefViewSet): serializer_class = s.LocationSerializer
class RackViewSet(_RefViewSet): serializer_class = s.RackSerializer
class ContainerViewSet(_RefViewSet): serializer_class = s.ContainerSerializer
class BarrelOrderViewSet(_RefViewSet): serializer_class = s.BarrelOrderSerializer
class BottleFormatViewSet(_RefViewSet): serializer_class = s.BottleFormatSerializer
class DryGoodViewSet(_RefViewSet): serializer_class = s.DryGoodSerializer
class MaterialViewSet(_RefViewSet): serializer_class = s.MaterialSerializer


# ============================================================================
# Spine — read-only over HTTP for now
# ============================================================================
class HarvestEventViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [ReadOnlyOrStaff]
    serializer_class = s.HarvestEventSerializer
    queryset = HarvestEvent.objects.all().order_by("-pk")


class WeighTagViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [ReadOnlyOrStaff]
    serializer_class = s.WeighTagSerializer
    queryset = WeighTag.objects.all().order_by("-pk")


# ============================================================================
# Lots — read via ORM, mutate only through services
# ============================================================================
class LotViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [ReadOnlyOrStaff]
    serializer_class = s.LotSerializer
    queryset = Lot.objects.select_related("current_designation").order_by("-pk")

    def create(self, request, *args, **kwargs):
        payload = s.LotCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        data = {k: v for k, v in data.items() if not (k in ("production_intent", "override_code") and v in (None, ""))}
        try:
            lot = gen.create_lot(**data)
        except Exception as e:  # noqa: BLE001
            raise DRFValidationError({"detail": str(e)})
        # stamp who created it (Lot is not append-only; editable)
        if hasattr(lot, "created_by") and lot.created_by_id is None:
            lot.created_by = request.user
            lot.save(update_fields=["created_by"])
        return Response(self.get_serializer(lot).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def redesignate(self, request, pk=None):
        lot = self.get_object()
        payload = s.RedesignateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            gen.redesignate(lot, **payload.validated_data)
        except Exception as e:  # noqa: BLE001
            raise DRFValidationError({"detail": str(e)})
        lot.refresh_from_db()
        return Response(self.get_serializer(lot).data)

    @action(detail=True, methods=["get"])
    def composition(self, request, pk=None):
        try:
            return Response(aging_svc.composition_of(self.get_object()))
        except Exception as e:  # noqa: BLE001
            raise DRFValidationError({"detail": str(e)})

    @action(detail=True, methods=["get"])
    def oak(self, request, pk=None):
        try:
            return Response(aging_svc.oak_detail(self.get_object()))
        except Exception as e:  # noqa: BLE001
            raise DRFValidationError({"detail": str(e)})

    @action(detail=True, methods=["get"])
    def cost(self, request, pk=None):
        lot = self.get_object()
        try:
            return Response({
                "lot_cost": costing_svc.lot_cost(lot),
                "lot_cost_per_gal": costing_svc.lot_cost_per_gal(lot),
            })
        except Exception as e:  # noqa: BLE001
            raise DRFValidationError({"detail": str(e)})


# ============================================================================
# Reports — period-scoped JSON
# ============================================================================
def _req_int(request, key):
    raw = request.query_params.get(key)
    if raw is None or str(raw).strip() == "":
        raise DRFValidationError({key: "required"})
    try:
        return int(raw)
    except ValueError:
        raise DRFValidationError({key: "must be an integer"})


def _req_date(request, key):
    raw = request.query_params.get(key)
    if not raw:
        raise DRFValidationError({key: "required (YYYY-MM-DD)"})
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise DRFValidationError({key: "must be ISO date YYYY-MM-DD"})


class Report5120View(APIView):
    """GET /api/reports/5120-17/?year=2025&month=10"""
    permission_classes = [ReadOnlyOrStaff]

    def get(self, request):
        year, month = _req_int(request, "year"), _req_int(request, "month")
        return Response(reporting_svc.build_5120_17(year, month))


class Report5120Part3View(APIView):
    permission_classes = [ReadOnlyOrStaff]

    def get(self, request):
        year, month = _req_int(request, "year"), _req_int(request, "month")
        return Response(reporting_svc.build_5120_17_part3(year, month))


class Report5120Part4View(APIView):
    permission_classes = [ReadOnlyOrStaff]

    def get(self, request):
        year, month = _req_int(request, "year"), _req_int(request, "month")
        return Response(reporting_svc.build_5120_17_part4(year, month))


class ExciseView(APIView):
    """GET /api/reports/excise/?year=2025&start=2025-10-01&end=2026-01-01"""
    permission_classes = [ReadOnlyOrStaff]

    def get(self, request):
        year = _req_int(request, "year")
        start, end = _req_date(request, "start"), _req_date(request, "end")
        return Response(excise_svc.compute_period_excise(year, start, end))


class CrushReportView(APIView):
    """GET /api/reports/crush/?year=2025"""
    permission_classes = [ReadOnlyOrStaff]

    def get(self, request):
        year = _req_int(request, "year")
        rows = crush_svc.ca_crush_report(year)
        return Response({"rows": rows, "totals": crush_svc.crush_report_totals(rows)})


# ============================================================================
# Rendered documents — build data, fill template to a temp file, stream bytes
# ============================================================================
def _stream(path, content_type, filename):
    with open(path, "rb") as f:
        data = f.read()
    resp = HttpResponse(data, content_type=content_type)
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


def _tmp(suffix):
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    return path


class Render5120PdfView(APIView):
    """GET /api/reports/5120-17/pdf/?year=2025&month=10&version=Original"""
    permission_classes = [ReadOnlyOrStaff]

    def get(self, request):
        year, month = _req_int(request, "year"), _req_int(request, "month")
        part1 = reporting_svc.build_5120_17(year, month)["fields"]
        part3 = reporting_svc.build_5120_17_part3(year, month)
        part4 = reporting_svc.build_5120_17_part4(year, month)
        header = {
            "MONTH": f"{month:02d}", "YEAR": str(year),
            "OPERATED_BY": _WINERY["NAME_ADDRESS"],
            "REGISTRY_NUMBER": _WINERY["REGISTRY"], "EIN": _WINERY["EIN"],
            "Chk2": request.query_params.get("version", "Original"),
        }
        out = _tmp(".pdf")
        try:
            forms_svc.render_5120_17_pdf(_TPL_5120, out, part1=part1, part3=part3, part4=part4, header=header)
            return _stream(out, "application/pdf", f"5120-17_{year}-{month:02d}.pdf")
        finally:
            if os.path.exists(out):
                os.remove(out)


class Render500024PdfView(APIView):
    """GET /api/reports/5000-24/pdf/?year=2025&start=2025-10-01&end=2026-01-01&serial=2025-4"""
    permission_classes = [ReadOnlyOrStaff]

    def get(self, request):
        year = _req_int(request, "year")
        start, end = _req_date(request, "start"), _req_date(request, "end")
        serial = request.query_params.get("serial")
        if not serial:
            raise DRFValidationError({"serial": "required (5000.24 serial number, e.g. 2025-4)"})
        net_tax = excise_svc.compute_period_excise(year, start, end)["net_tax"]
        header = {
            "Employer_ID": _WINERY["EIN"], "Plant_No": _WINERY["REGISTRY"],
            "Taxpayer_Address": _WINERY["NAME_ADDRESS"],
        }
        out = _tmp(".pdf")
        try:
            forms_svc.render_5000_24_pdf(
                _TPL_5000, out, net_tax=net_tax, serial_number=serial,
                period_start=start, period_end=end, header=header, date_signed=date.today())
            return _stream(out, "application/pdf", f"5000-24_{serial}.pdf")
        finally:
            if os.path.exists(out):
                os.remove(out)


class RenderCrushPdfView(APIView):
    """GET /api/reports/crush/pdf/?year=2025"""
    permission_classes = [ReadOnlyOrStaff]

    def get(self, request):
        year = _req_int(request, "year")
        rows = crush_svc.ca_crush_report(year)
        totals = crush_svc.crush_report_totals(rows)
        out = _tmp(".pdf")
        try:
            forms_svc.render_crush_report_pdf(rows, year, out, totals=totals)
            return _stream(out, "application/pdf", f"crush_report_{year}.pdf")
        finally:
            if os.path.exists(out):
                os.remove(out)


class RenderCrushCsvView(APIView):
    """GET /api/reports/crush/csv/?year=2025"""
    permission_classes = [ReadOnlyOrStaff]

    def get(self, request):
        year = _req_int(request, "year")
        rows = crush_svc.ca_crush_report(year)
        out = _tmp(".csv")
        try:
            forms_svc.crush_report_csv(rows, out)
            return _stream(out, "text/csv", f"crush_report_{year}.csv")
        finally:
            if os.path.exists(out):
                os.remove(out)
