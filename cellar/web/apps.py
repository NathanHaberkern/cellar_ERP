from django.apps import AppConfig


class CellarWebConfig(AppConfig):
    """Registered as its own app (distinct label) so Django's APP_DIRS loader
    discovers cellar/web/templates, cellar/web/static, and the web_extras
    template tags. Carries no models of its own -- it's a presentation layer over
    cellar's models and services."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "cellar.web"
    label = "cellar_web"
    verbose_name = "Cellar Web (HTMX)"
