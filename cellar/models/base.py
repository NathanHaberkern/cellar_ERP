"""
Base classes and shared enumerations.

LedgerEntry is the append-only contract from the data model: rows are
inserted, never updated or deleted. A correction is a NEW row whose
`supersedes` points at the one it replaces — both are retained.
"""
from django.conf import settings
from django.db import models


class Program(models.TextChoices):
    TABLE = "table", "Table"
    PORT = "port", "Port"
    ROSE = "rose", "Rosé"


class LotKind(models.TextChoices):
    STANDARD = "standard", "Standard"
    BLEND = "blend", "Blend"
    COFERMENT = "coferment", "Co-ferment"
    SPLIT = "split", "Split"
    # A parcel racked off a finished bulk lot to be prepped and bottled. Kept distinct
    # from STANDARD on purpose: _abbr_lot_count() (the singleton display rule) counts
    # only STANDARD lots, so splitting a parcel off 25VERD must not silently re-render
    # the parent as 25VERD1.
    BOTTLING = "bottling", "Bottling parcel"


class SourceType(models.TextChoices):
    ESTATE = "estate", "Estate"
    PURCHASED = "purchased", "Purchased"
    CONTRACT = "contract", "Contract"


class AppendOnly(models.Model):
    """Insert-mostly base. After creation a row may NOT be edited, except:
      * voided_at — mark a wrong entry void (kept for audit, excluded from reports)
      * fields listed in the subclass CLOSE_FIELDS — e.g. emptied_at, removed_at
    Deletes are never allowed; correct by voiding and entering a new row."""
    created_at = models.DateTimeField(auto_now_add=True)
    operator = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.PROTECT, related_name="+",
    )
    supersedes = models.ForeignKey(
        "self", null=True, blank=True,
        on_delete=models.PROTECT, related_name="+",
    )
    voided_at = models.DateTimeField(null=True, blank=True,
        help_text="void a wrong entry — kept for audit, excluded from reports")
    notes = models.TextField(blank=True)

    CLOSE_FIELDS = ()                 # subclass-specific fields settable after creation
    _ALWAYS_EDITABLE = ("voided_at",)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk and not self._state.adding:
            old = type(self).objects.filter(pk=self.pk).first()
            if old is not None:
                allowed = set(self._ALWAYS_EDITABLE) | set(self.CLOSE_FIELDS)
                changed = [f.name for f in self._meta.concrete_fields
                           if getattr(self, f.attname) != getattr(old, f.attname)]
                illegal = [c for c in changed if c not in allowed]
                if illegal:
                    raise ValueError(
                        f"{type(self).__name__} is append-only; can't edit {illegal} after "
                        f"creation (allowed: {sorted(allowed) or 'none'}). Void it and add a new row.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError(f"{type(self).__name__} is append-only; void it instead of deleting.")

    @property
    def is_voided(self):
        return self.voided_at is not None
