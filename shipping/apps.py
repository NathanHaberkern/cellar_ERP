from django.apps import AppConfig


class ShippingConfig(AppConfig):
    """DTC shipping: Commerce7 orders -> carrier labels -> C7 fulfillment.

    Kept as its own app (not inside `cellar`) on purpose. Shipping touches the
    cellar only through the SKU string, so it can ship and evolve before the
    FinishedGoodsLot / case-goods tranche exists, and a carrier outage or API
    change never puts a migration in the compliance-critical cellar app.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "shipping"
    verbose_name = "Shipping"

    def ready(self):
        # Default permission groups are created after migrate, not inside a
        # migration: custom model permissions don't exist yet while migrations
        # run (Django creates them in its own post_migrate handler).
        from django.db.models.signals import post_migrate

        from .permissions import ensure_default_groups

        post_migrate.connect(ensure_default_groups, sender=self,
                             dispatch_uid="shipping.ensure_default_groups")
