"""
Cellar API URL map. Mounted at /api/ (see config_patches/urls_additions.py).

    /api/auth/...            session + token login/logout/identity
    /api/<reference>/        CRUD viewsets (varieties, vineyards, additives, ...)
    /api/lots/               lot read + service actions (create, redesignate, ...)
    /api/reports/...         5120.17 (I/III/IV), excise, crush — JSON + PDF/CSV
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter

from . import views as v
from . import auth

router = DefaultRouter()

# reference masters (CRUD)
router.register("varieties", v.VarietyViewSet, basename="variety")
router.register("growers", v.GrowerViewSet, basename="grower")
router.register("vineyards", v.VineyardViewSet, basename="vineyard")
router.register("blocks", v.BlockViewSet, basename="block")
router.register("designations", v.VarietalDesignationViewSet, basename="designation")
router.register("vessels", v.VesselViewSet, basename="vessel")
router.register("additives", v.AdditiveViewSet, basename="additive")
router.register("analytes", v.LabAnalyteViewSet, basename="analyte")
router.register("config-constants", v.ConfigConstantViewSet, basename="configconstant")
router.register("tax-classes", v.TaxClassViewSet, basename="taxclass")
router.register("rooms", v.RoomViewSet, basename="room")
router.register("locations", v.LocationViewSet, basename="location")
router.register("racks", v.RackViewSet, basename="rack")
router.register("containers", v.ContainerViewSet, basename="container")
router.register("barrel-orders", v.BarrelOrderViewSet, basename="barrelorder")
router.register("bottle-formats", v.BottleFormatViewSet, basename="bottleformat")
router.register("dry-goods", v.DryGoodViewSet, basename="drygood")
router.register("materials", v.MaterialViewSet, basename="material")

# spine (read-only for now)
router.register("harvest-events", v.HarvestEventViewSet, basename="harvestevent")
router.register("weigh-tags", v.WeighTagViewSet, basename="weightag")

# lots (read + service-backed writes)
router.register("lots", v.LotViewSet, basename="lot")


auth_patterns = [
    path("csrf/", auth.CsrfView.as_view(), name="auth-csrf"),
    path("login/", auth.SessionLoginView.as_view(), name="auth-login"),
    path("logout/", auth.SessionLogoutView.as_view(), name="auth-logout"),
    path("token/", auth.ObtainTokenView.as_view(), name="auth-token"),
    path("token/logout/", auth.RevokeTokenView.as_view(), name="auth-token-logout"),
    path("whoami/", auth.WhoAmIView.as_view(), name="auth-whoami"),
]

report_patterns = [
    path("5120-17/", v.Report5120View.as_view(), name="report-5120"),
    path("5120-17/part3/", v.Report5120Part3View.as_view(), name="report-5120-p3"),
    path("5120-17/part4/", v.Report5120Part4View.as_view(), name="report-5120-p4"),
    path("5120-17/pdf/", v.Render5120PdfView.as_view(), name="report-5120-pdf"),
    path("5000-24/pdf/", v.Render500024PdfView.as_view(), name="report-5000-24-pdf"),
    path("excise/", v.ExciseView.as_view(), name="report-excise"),
    path("crush/", v.CrushReportView.as_view(), name="report-crush"),
    path("crush/pdf/", v.RenderCrushPdfView.as_view(), name="report-crush-pdf"),
    path("crush/csv/", v.RenderCrushCsvView.as_view(), name="report-crush-csv"),
]

urlpatterns = [
    path("auth/", include(auth_patterns)),
    path("reports/", include(report_patterns)),
    path("", include(router.urls)),
]
