"""
Lot dashboard v2 — full-page-per-tile shell.

The redesign (see Front_end_wireframes_v_2_0): one read-only summary card per lot
with two variants (fermentation vs aging, switched on is_in_bond), a shared
per-lot lifecycle Gantt filtered per tile, an outstanding-tasks widget, and an
8-tile menu — 4 capture (Fermentation, Additions, Movement, Oak) and 4 read
(Composition, Compliance, Cost, Labs).

This module is ADDITIVE and isolated: it defines its own page views and mounts
at /lots/<pk>/d/... . The legacy single-page lot_detail (/lots/<pk>/) is left
untouched so the two can run side by side during evaluation. Once approved, the
legacy route flips to `page_fermentation` (or a mode-aware landing) and the old
tab bar retires.

Read tiles render their body server-side (full page). Capture tiles are full
pages too, but lazy-load their existing HTMX fragment into the body — that reuses
the current fermentation/additions/movement/oak views verbatim (no context
duplication, no risk of drift) until each gets its own redesign slice
(progressive disclosure for Fermentation; folding Sweeten + Re-fortification into
Additions; folding Bottling into Movement; the barrel/rack representation for
Oak, which waits on the seed import).
"""

from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone

from cellar.models.spine import Lot
from cellar.models.reference import Additive, LabAnalyte
from cellar.models.fermentation import LabResult
from cellar.services import bonding as bond_svc
from cellar.services import volumes as vol_svc
from cellar.services import lotmeta
from cellar.web import lotpages


# ---- tile registry --------------------------------------------------------
# key -> (label, group, url-name, gantt-domain or None to hide the gantt).
# group: "capture" (data entry) or "read" (focused summary).
CAPTURE = "capture"
READ = "read"
TILES = [
    ("fermentation", "Fermentation", CAPTURE, "lot2-fermentation", "fermentation"),
    ("additions",    "Additions",    CAPTURE, "lot2-additions",    "additions"),
    ("movement",     "Movement",     CAPTURE, "lot2-movement",     "movement"),
    ("oak",          "Oak",          CAPTURE, "lot2-oak",          "oak"),
    ("composition",  "Composition",  READ,    "lot2-composition",  "composition"),
    ("compliance",   "Compliance",   READ,    "lot2-compliance",   "compliance"),
    ("cost",         "Cost",         READ,    "lot2-cost",         None),
    ("labs",         "Labs",         READ,    "lot2-labs",         None),
]


def _safe(fn, *a, **k):
    """Run a read helper; return (value, error_message) so a data gap shows an
    inline note rather than 500-ing the whole dashboard."""
    try:
        return fn(*a, **k), None
    except Exception as exc:  # noqa: BLE001 - deliberately broad; this is display-only
        return None, str(exc)


# ---- summary-card helpers -------------------------------------------------
def _last_topped_days(lot):
    from cellar.models import ToppingTarget
    tt = (ToppingTarget.objects.filter(placement__lot=lot, voided_at__isnull=True)
          .select_related("event").order_by("-event__topped_at").first())
    if not tt or not tt.event or not tt.event.topped_at:
        return None
    return max((timezone.localdate() - tt.event.topped_at).days, 0)


def _bond_date(lot):
    from cellar.models import BookToBond
    b = (BookToBond.objects.filter(lot=lot, voided_at__isnull=True)
         .order_by("booked_at").first())
    return b.booked_at if b else None


def _task_summary(lot):
    from cellar.services import tasks as tsvc
    open_qs = list(tsvc.open_tasks(lot=lot))
    return {
        "open_count": len(open_qs),
        "overdue_count": sum(1 for t in open_qs if t.is_overdue),
        "next": open_qs[:4],
        "more": max(0, len(open_qs) - 4),
    }


# ---- shared lifecycle Gantt (v1) ------------------------------------------
# One data source (the merged lot timeline), rendered as a horizontal axis with
# a pre-bond / in-bond band split at book-to-bond, and event markers filtered to
# the active tile's domain. Richer phase bands (primary / MLF / élevage) arrive
# with the capture-tile slice; this v1 gives the honest skeleton every tile shares.
_DOMAIN_KEYWORDS = {
    "fermentation": ("reading", "addition", "inoculat", "press", "destem"),
    "additions":    ("addition", "sweeten", "fortif"),
    "movement":     ("transfer", "split", "blend", "sale", "b2b", "bulk",
                     "must", "bottl", "rack"),
    "oak":          ("barrel", "topping", "rack", "fill"),
    "composition":  ("blend", "split"),
    "compliance":   ("book", "bond", "loss", "removal", "transfer", "bottl", "fortif"),
}


def _in_domain(kind, domain):
    if not domain:
        return True
    kw = _DOMAIN_KEYWORDS.get(domain)
    if not kw:
        return True
    k = (kind or "").lower()
    return any(w in k for w in kw)


def _gantt(lot, domain):
    rows, _ = _safe(lotpages.timeline, lot, 200)
    rows = [r for r in (rows or []) if r.get("date")]
    markers = [r for r in rows if _in_domain(r.get("kind"), domain)]
    dates = [r["date"] for r in rows]
    today = timezone.localdate()
    bond_dt = _bond_date(lot)
    if bond_dt:
        dates.append(bond_dt)
    if not dates:
        return {"empty": True}
    lo = min(dates)
    hi = max(max(dates), today)
    span = max((hi - lo).days, 1)

    def frac(d):
        return round((d - lo).days / span * 100, 2)

    out_markers = []
    for r in markers:
        out_markers.append({
            "x": frac(r["date"]), "kind": r["kind"],
            "label": r["label"], "detail": r.get("detail") or "",
            "date": r["date"],
        })
    # de-dup near-identical x positions is unnecessary for a glance; keep all.
    return {
        "empty": False,
        "start": lo, "end": hi,
        "bond_x": frac(bond_dt) if bond_dt else None,
        "today_x": frac(today),
        "markers": out_markers,
        "domain": domain,
    }


# ---- shell context --------------------------------------------------------
def _shell_ctx(lot, active, *, gantt_domain=None, show_gantt=True):
    in_bond = bond_svc.is_in_bond(lot)
    summary = lotpages.summary(lot)
    ctx = {
        "nav": "lots", "lot": lot, "active": active,
        "in_bond": in_bond, "summary": summary,
        "tiles": [
            {"key": k, "label": lbl, "group": grp,
             "url": reverse(url, args=[lot.pk]), "on": k == active}
            for (k, lbl, grp, url, _dom) in TILES
        ],
        "tasks": _task_summary(lot),
        "show_gantt": show_gantt,
        "gantt": _gantt(lot, gantt_domain) if show_gantt else None,
        "is_port": lotmeta.is_port(lot),
    }
    if in_bond:
        o, _ = _safe(lotpages.oak, lot)
        ctx["aging"] = {
            "barrel_count": (o or {}).get("barrel_count", 0),
            "location": summary.get("location"),
            "last_topped_days": _last_topped_days(lot),
        }
    else:
        prog, _ = _safe(lotpages.ferment_progress, lot)
        ctx["progress"] = prog
    return ctx


def _render(request, lot, active, *, body_include=None, body_htmx=None,
            body_ctx=None, gantt_domain=None, show_gantt=True, sections=None):
    ctx = _shell_ctx(lot, active, gantt_domain=gantt_domain, show_gantt=show_gantt)
    ctx["body_include"] = body_include
    ctx["body_htmx_url"] = reverse(body_htmx, args=[lot.pk]) if body_htmx else None
    ctx["sections"] = sections
    if body_ctx:
        ctx.update(body_ctx)
    return render(request, "web/lot_shell.html", ctx)


def _capture(request, lot, active, specs, gantt_domain):
    """Render a capture tile with an in-tile action switcher.

    `specs`: ordered list of (key, label, fragment_url_name, visible). The
    switcher is server-rendered full-page nav (?action=key) — no top-level
    sub-tabs. The chosen action's existing fragment lazy-loads into the shell
    body (#lot-panel) and its own forms re-render it, so folding the former
    satellite tabs (Sweeten / Re-fortification / Bottling / Book-to-bond) in
    here reuses every existing view verbatim with zero target collisions.
    """
    visible = [s for s in specs if s[3]]
    keys = [s[0] for s in visible]
    action = request.GET.get("action")
    if action not in keys:
        action = keys[0] if keys else None
    chosen = next((s for s in visible if s[0] == action), None)
    sections = [
        {"key": k, "label": lbl,
         "url": f"{reverse(active_url(active), args=[lot.pk])}?action={k}",
         "on": k == action}
        for (k, lbl, _frag, _vis) in visible
    ] if len(visible) > 1 else None
    body_htmx = chosen[2] if chosen else None
    return _render(request, lot, active, body_htmx=body_htmx,
                   gantt_domain=gantt_domain, sections=sections)


def active_url(active):
    return {
        "fermentation": "lot2-fermentation", "additions": "lot2-additions",
        "movement": "lot2-movement", "oak": "lot2-oak",
    }[active]


# ===========================================================================
# Capture tiles — full pages with an in-tile action switcher that folds the
# former satellite tabs into their parent (Sweeten + Re-fortification →
# Additions; Bottling → Movement; Book-to-bond → Fermentation). Each action's
# existing fragment is reused verbatim; the fermentation flow itself already
# does status-driven progressive disclosure (see web/fermentation.ferment_ctx).
# ===========================================================================
@login_required
def page_fermentation(request, pk):
    lot = get_object_or_404(Lot, pk=pk)
    # Book-to-bond is the production declaration that ends primary — it belongs
    # in the fermentation flow, not on the summary card (per the v2 model).
    from cellar.services import bonding as bond
    specs = [
        ("flow", "Crush → ferment → press", "lot-ferment", True),
        ("book", "Book to bond", "lot-bond-card", bond.can_book_to_bond(lot) or bond.is_in_bond(lot)),
    ]
    return _capture(request, lot, "fermentation", specs, "fermentation")


@login_required
def page_additions(request, pk):
    lot = get_object_or_404(Lot, pk=pk)
    is_port = lotmeta.is_port(lot)
    specs = [
        ("add", "Record addition", "lot-additions", True),
        ("sweeten", "Backsweeten", "lot-sweeten", True),
        ("fortify", "Re-fortification", "lot-fortification", is_port),
    ]
    return _capture(request, lot, "additions", specs, "additions")


@login_required
def page_movement(request, pk):
    lot = get_object_or_404(Lot, pk=pk)
    from cellar.services import bottling as bz
    can_bottle = bz.can_split(lot) or bz.is_parcel(lot) or bool(bz.parcels_of(lot))
    specs = [
        ("move", "Rack · transfer · sale · blend", "lot-movement", True),
        ("bottle", "Bottling", "lot-bottling", can_bottle),
    ]
    return _capture(request, lot, "movement", specs, "movement")


@login_required
def page_oak(request, pk):
    lot = get_object_or_404(Lot, pk=pk)
    # Oak stays single-fragment for now; barrel/rack representation lands with
    # the seed import. Topping / rack-out already live inside this fragment.
    return _render(request, lot, "oak", body_htmx="lot-oak", gantt_domain="oak")


# ===========================================================================
# Read tiles — full pages, body rendered server-side.
# ===========================================================================
@login_required
def page_composition(request, pk):
    lot = get_object_or_404(Lot, pk=pk)
    data, err = _safe(lotpages.composition, lot)
    body = {
        "composition": data or {}, "error": err,
        "override": getattr(lot, "composition_override", None),
        "section": "composition", "note": lotpages.section_note(lot, "composition"),
    }
    return _render(request, lot, "composition",
                   body_include="web/_lot_composition.html",
                   body_ctx=body, gantt_domain="composition")


@login_required
def page_cost(request, pk):
    from cellar.services import costing as costing_svc
    lot = get_object_or_404(Lot, pk=pk)
    breakdown, err = _safe(lambda l: {
        "fruit": costing_svc.fruit_cost(l),
        "additions": costing_svc.addition_cost(l),
        "spirit": costing_svc.spirit_cost(l),
        "oak_depreciation": costing_svc.lot_oak_depreciation(l),
        "total": costing_svc.lot_cost(l),
        "per_gal": costing_svc.lot_cost_per_gal(l),
    }, lot)
    body = {"cost": breakdown or {}, "error": err,
            "section": "cost", "note": lotpages.section_note(lot, "cost")}
    # Cost has no lifecycle Gantt in the wireframe (its own pie is the viz).
    return _render(request, lot, "cost",
                   body_include="web/_lot_cost.html",
                   body_ctx=body, show_gantt=False)


@login_required
def page_labs(request, pk):
    lot = get_object_or_404(Lot, pk=pk)
    groups, err = _safe(lotpages.labs, lot)
    body = {
        "groups": groups or [], "error": err,
        "sources": LabResult.Source.choices,
        "analytes": LabAnalyte.objects.order_by("name"),
        "now_local": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
        "section": "labs", "note": lotpages.section_note(lot, "labs"),
        "read_only": True,  # v2: Labs is read-only; entry lives on the winery-level lab page
    }
    # Labs has its own FSO2 / VA timelines, not the lifecycle Gantt.
    return _render(request, lot, "labs",
                   body_include="web/_lot_labs.html",
                   body_ctx=body, show_gantt=False)


@login_required
def page_compliance(request, pk):
    """New read tile. Per-lot in-bond balance built entirely on the existing
    volumes.lot_balance_detail decomposition — no new model, no new math.
    v1 shows the signed decomposition as a running-balance ledger + bond status;
    the chronological per-event enrichment is a fast-follow."""
    lot = get_object_or_404(Lot, pk=pk)
    detail, err = _safe(vol_svc.lot_balance_detail, lot)
    detail = detail or {}
    in_bond = bond_svc.is_in_bond(lot)

    # Build a signed ledger from the decomposition, in production order, with a
    # running balance. Increases first (booked, inbound, added, gains), then the
    # decreases (losses, outbound, bulk/tax-paid removals, bond transfers, must
    # sales, bottling). Each row: label, increase, decrease, balance.
    def g(key):
        v = detail.get(key)
        return Decimal(v) if v is not None else Decimal("0")

    rows = []
    running = Decimal("0")

    def add(label, amount, *, increase):
        nonlocal running
        if amount == 0:
            return
        running += amount if increase else -amount
        rows.append({
            "label": label,
            "increase": amount if increase else None,
            "decrease": amount if not increase else None,
            "balance": running,
        })

    add("Booked to bond", g("booked"), increase=True)
    add("Inbound (blend/transfer in)", g("inbound"), increase=True)
    add("Volume added (water / sweetening)", g("volume_added"), increase=True)
    add("Losses (evaporation / spillage)", g("losses"), increase=False)
    add("Outbound (blend / transfer out)", g("outbound"), increase=False)
    add("Bulk tax-paid removals", g("bulk_removed"), increase=False)
    add("Bond transfers out (B2B)", g("bond_transferred_out"), increase=False)
    add("Must sales", g("must_sold"), increase=False)
    add("Bottled", g("bottled"), increase=False)

    body = {
        "detail": detail, "ledger_rows": rows,
        "balance": detail.get("balance"),
        "in_bond": in_bond,
        "bond_status": "In bond" if in_bond else "Tax paid / not yet bonded",
        "error": err,
        "section": "compliance", "note": lotpages.section_note(lot, "compliance"),
    }
    return _render(request, lot, "compliance",
                   body_include="web/_lot_compliance.html",
                   body_ctx=body, gantt_domain="compliance")
