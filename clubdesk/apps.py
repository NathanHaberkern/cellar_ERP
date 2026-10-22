from django.apps import AppConfig


class ClubdeskConfig(AppConfig):
    """Club desk: member requests from email -> approved Commerce7 changes -> draft replies.

    Its own app for the same reason as `shipping`: a C7 API change or an email
    parsing problem never puts a migration in the cellar app. It reads shipping's
    ShipOrder for club orders that are already built, and may set a hold or a
    requested ship date on one that is not pooled yet; it never buys labels.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "clubdesk"
    verbose_name = "Club desk"

    def ready(self):
        from django.db.models.signals import post_migrate

        from .permissions import ensure_default_groups

        post_migrate.connect(ensure_default_groups, sender=self,
                             dispatch_uid="clubdesk.ensure_default_groups")
