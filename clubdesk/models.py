"""Club desk data model.

ClubRequest is working state: one member ask, parsed from an email, with the
exact changes we propose and where they stand. Nothing reaches Commerce7 until
Nate approves the row (status APPROVED, approved_by set).

C7Write is the audit trail of every call we made to Commerce7, including the
customer-account note that accompanies each change. Rows are never edited.
"""
from django.conf import settings
from django.db import models

from .permissions import CUSTOM_PERMISSIONS


class Kind(models.TextChoices):
    SKIP = "skip", "Skip a shipment"
    PAUSE = "pause", "Pause membership"
    CANCEL = "cancel", "Cancel membership"
    HOLD_PICKUP = "hold_pickup", "Hold for pickup"
    DELAY = "delay", "Delay delivery"
    ADDRESS = "address", "Change ship-to address"
    SWAP = "swap", "Swap wines"
    REFUND = "refund", "Refund"
    CARD = "card", "Payment card"
    STATUS = "status", "Where is my order"
    OTHER = "other", "Other"


class ClubRequest(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "New"
        PROPOSED = "proposed", "Proposed, waiting for approval"
        BLOCKED = "blocked", "Blocked, needs Nate"
        APPROVED = "approved", "Approved"
        APPLIED = "applied", "Applied"
        FAILED = "failed", "Failed"
        DISMISSED = "dismissed", "Dismissed"

    # where it came from
    mailbox = models.CharField(max_length=120, help_text="info@ or nathan@")
    message_id = models.CharField(max_length=400, unique=True,
                                  help_text="Outlook message id the request was read from")
    received_at = models.DateTimeField()
    customer_email = models.EmailField(db_index=True)
    customer_name = models.CharField(max_length=120, blank=True)
    first_name = models.CharField(max_length=60, blank=True, help_text="for the reply greeting")
    request_text = models.TextField(blank=True, help_text="the member's words, for review")

    # what they asked for
    kind = models.CharField(max_length=20, choices=Kind.choices)
    details = models.JSONField(default=dict, blank=True,
                               help_text="parsed specifics: dates, address, wines, package")
    requested_date = models.DateField(null=True, blank=True,
                                      help_text="deliver-after / hold-until / resume date")

    # what it matched in C7 and shipping
    c7_customer_id = models.CharField(max_length=64, blank=True)
    c7_membership_id = models.CharField(max_length=64, blank=True)
    ship_order = models.ForeignKey("shipping.ShipOrder", null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="club_requests")
    stage = models.CharField(max_length=20, blank=True,
                             help_text="before_build / after_build, when planned")

    # the plan
    actions = models.JSONField(default=list, blank=True,
                               help_text="ordered list of proposed changes (see services.plan)")
    note_text = models.TextField(blank=True, help_text="note written on the C7 customer account")
    block_reasons = models.JSONField(default=list, blank=True)
    manual_steps = models.JSONField(default=list, blank=True,
                                    help_text="what staff must do in C7 admin by hand")
    draft_text = models.TextField(blank=True, help_text="confirmation reply, never sent automatically")
    draft_message_id = models.CharField(max_length=400, blank=True,
                                        help_text="Outlook draft id once the draft is created")

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEW,
                              db_index=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.PROTECT, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-received_at", "-id"]
        permissions = CUSTOM_PERMISSIONS

    def __str__(self):
        return f"{self.get_kind_display()} — {self.customer_name or self.customer_email}"

    @property
    def c7_actions(self):
        return [a for a in self.actions if a.get("target") == "c7"]


class C7Write(models.Model):
    """One call made to Commerce7 on behalf of a request. Insert only."""

    request = models.ForeignKey(ClubRequest, on_delete=models.PROTECT, related_name="writes")
    method = models.CharField(max_length=8)
    path = models.CharField(max_length=200)
    body = models.JSONField(default=dict, blank=True)
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    response = models.JSONField(default=dict, blank=True)
    is_note = models.BooleanField(default=False)
    ok = models.BooleanField(default=False)
    made_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                on_delete=models.PROTECT, related_name="+")
    made_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["made_at", "id"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValueError("C7Write rows are insert-only")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("C7Write rows are never deleted")
