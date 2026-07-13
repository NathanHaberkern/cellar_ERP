"""
Dashboard mode — auto-detected, not stored.

Crush is characterized by wine actively moving through the pre-barrel stages:
receiving, processing, cold soak, fermenting, pressed, settling. The moment
nothing is in that pipeline, the cellar's daily work shifts to what's already
in barrel/tank aging — topping, SO2, watching VA — which is Cellar Mode.

Deliberately no manual override and nothing persisted: the mode is recomputed
on every dashboard load from `Lot.status`, the same field everything else
already reads. Two dynos, two users, or a lot status changing mid-session all
converge on the same answer with no state to get stale or fight over. If a
per-user override is ever wanted, it layers on top of this function rather
than replacing it — this stays the "what does the data say" baseline.
"""
from cellar.models import Lot

# Anything before DONE_PRIMARY is "in crush" — the wine hasn't reached its
# post-primary home yet. BOTTLED and DONE_PRIMARY don't count: a lot sitting
# in barrel post-primary is exactly the Cellar Mode case, even in October.
CRUSH_STATUSES = (
    Lot.Status.RECEIVING, Lot.Status.PROCESSING, Lot.Status.COLD_SOAK,
    Lot.Status.FERMENTING, Lot.Status.PRESSED, Lot.Status.SETTLING,
)


def crush_lot_count():
    """How many lots are currently in a pre-primary status. The dashboard's
    detection signal; also useful on its own (e.g. a mode-transition banner)."""
    return Lot.objects.filter(status__in=CRUSH_STATUSES).count()


def detect_mode():
    """'crush' if any lot is actively in the pre-primary pipeline, else 'cellar'."""
    return "crush" if crush_lot_count() > 0 else "cellar"
