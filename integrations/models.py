"""Connection state for external services that can't live in env vars.

QuickBooks is the one case so far: Intuit issues a new refresh token on every
refresh (and the old one stops working soon after), so it has to be written
somewhere the app can update. Env vars can't be, so it's this one row.
App credentials (client id / secret) still come only from the environment.
"""
from django.db import models


class QBOConnection(models.Model):
    environment = models.CharField(max_length=12, unique=True,
                                   help_text="sandbox or production")
    realm_id = models.CharField(max_length=32, help_text="QuickBooks company id")
    company_name = models.CharField(max_length=120, blank=True)
    refresh_token = models.TextField()
    refresh_expires_at = models.DateTimeField(null=True, blank=True)
    access_token = models.TextField(blank=True)
    access_expires_at = models.DateTimeField(null=True, blank=True)
    connected_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "QuickBooks connection"

    def __str__(self):
        return f"QuickBooks {self.company_name or self.realm_id} ({self.environment})"
