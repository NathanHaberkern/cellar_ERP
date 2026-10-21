"""Buy and void labels, and record them.

Order of operations matters here because the carrier call costs money and
can't be rolled back by a database transaction:

  buy:  carrier call -> DB write.  If the DB write fails after FedEx has
        created the label, we log the tracking number at ERROR so it can be
        voided by hand; we never silently lose a label we paid for.
  void: carrier call -> DB write.  If the carrier refuses, nothing is marked
        void here, so the record never claims a label is dead when it isn't.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from shipping.carriers import CarrierAdapter, CarrierError, LabelRequest, get_adapter
from shipping.models import Label, LabelDocument, Package

log = logging.getLogger(__name__)


class LabelRefused(Exception):
    """A business rule blocked the purchase before the carrier was called."""


def buy_label(request: LabelRequest, *, carrier: str = "fedex",
              package: Package | None = None, operator=None,
              adapter: CarrierAdapter | None = None) -> Label:
    if package is not None:
        live = package.live_label
        if live is not None:
            raise LabelRefused(
                f"Package {package.pk} already has live label {live.tracking_number}; "
                f"void it before buying another.")

    adapter = adapter or get_adapter(carrier)
    result = adapter.create_label(request)          # raises CarrierError; nothing written

    try:
        with transaction.atomic():
            label = Label.objects.create(
                package=package, carrier=result.carrier, environment=result.environment,
                service=result.service, tracking_number=result.tracking_number,
                master_tracking_number=result.master_tracking_number,
                net_charge=result.net_charge, currency=result.currency,
                weight_lb=request.parcel.weight_lb,
                response_summary=result.response_summary, operator=operator,
            )
            for i, doc in enumerate(result.documents):
                LabelDocument.objects.create(label=label, kind=doc.kind,
                                             content_type=doc.content_type,
                                             content=doc.content, sequence=i)
    except Exception:
        log.error("Label %s (%s, %s) was CREATED at the carrier but not saved -- "
                  "void it manually.", result.tracking_number, result.carrier,
                  result.environment)
        raise
    return label


def void_label(label: Label, *, operator=None,
               adapter: CarrierAdapter | None = None) -> Label:
    if label.voided_at is not None:
        raise LabelRefused(f"{label.tracking_number} is already void.")
    adapter = adapter or get_adapter(label.carrier)
    if adapter.environment != label.environment:
        raise LabelRefused(
            f"{label.tracking_number} was bought in {label.environment}; the "
            f"{label.carrier} adapter is set to {adapter.environment}.")
    adapter.void_label(label.tracking_number)       # raises CarrierError; nothing written
    label.voided_at = timezone.now()
    label.voided_by = operator
    label.save()
    return label


__all__ = ["buy_label", "void_label", "LabelRefused", "CarrierError"]
