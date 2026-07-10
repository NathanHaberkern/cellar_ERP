"""
Dashboard tank map — read model.

Builds the three-room map (Old Tank / New Tank / New Barrel) from the same
append-only ledger everything else reads. A vessel's *current* contents come
from its open TankAssignment: the most recent one with voided_at AND emptied_at
both null. No open assignment ⇒ the vessel is empty.

Placement is data-driven (Vessel.room + map_row/map_col), seeded by
`manage.py seed_vessel_layout`. Bin ferments carry no fixed placement — any
bin-type vessel with an open assignment is drawn in the barrel-room bin strip,
and hidden the moment it empties (per the feedback).

Shape rule mirrors the drawings: tank rooms draw circles, the barrel room draws
squares. Size buckets echo real capacity so the big fermenters read as bigger.
"""
from cellar.models.reference import Vessel
from cellar.models.fermentation import TankAssignment

# Lot statuses that get their own highlight color on the map. Everything else
# (planned / receiving / processing / settling / done_primary) renders neutral.
HIGHLIGHT = {"cold_soak": "cold", "fermenting": "ferment", "pressed": "pressed"}

_BIN_TYPES = (Vessel.Type.MACRO_BIN, Vessel.Type.ONE_TON_BIN)
_ROOM_ORDER = [
    (Vessel.Room.OLD_TANK, "Old Tank Room", "circle"),
    (Vessel.Room.NEW_TANK, "New Tank Room", "circle"),
    (Vessel.Room.NEW_BARREL, "New Barrel Room", "square"),
]


def _size_bucket(capacity_gal):
    if not capacity_gal:
        return "md"
    cap = float(capacity_gal)
    if cap >= 2000:
        return "lg"
    if cap >= 900:
        return "md"
    return "sm"


def _open_assignments():
    """vessel_id -> Lot for every vessel with an open (unvacated) assignment.
    One query, newest-first, first-seen-wins so a vessel maps to its latest
    open occupant even if stray older opens exist."""
    qs = (TankAssignment.objects
          .filter(voided_at__isnull=True, emptied_at__isnull=True)
          .select_related("lot", "lot__current_designation")
          .order_by("-assigned_at"))
    current = {}
    for a in qs:
        current.setdefault(a.vessel_id, a.lot)
    return current


def _vessel_cell(vessel, shape, lot):
    occupied = lot is not None
    status = lot.status if occupied else ""
    return {
        "code": vessel.code,
        "shape": shape,
        "size": _size_bucket(vessel.capacity_gal),
        "row": vessel.map_row,
        "col": vessel.map_col,
        "occupied": occupied,
        "lot_code": (lot.code if occupied else ""),
        "status": status,
        "status_label": (lot.get_status_display() if occupied else "Empty"),
        # css hook: cold / ferment / pressed for the big three, else 'other',
        # 'empty' when nothing is in the vessel.
        "status_class": HIGHLIGHT.get(status, "other") if occupied else "empty",
    }


def build_tank_map():
    current = _open_assignments()
    placed = (Vessel.objects
              .exclude(room="")
              .exclude(map_row__isnull=True)
              .order_by("room", "map_row", "map_col"))

    by_room = {}
    for v in placed:
        by_room.setdefault(v.room, []).append(v)

    rooms = []
    for room_key, label, shape in _ROOM_ORDER:
        vessels = by_room.get(room_key, [])
        cells = [_vessel_cell(v, shape, current.get(v.id)) for v in vessels]
        cols = max((c["col"] for c in cells), default=-1) + 1
        rows = max((c["row"] for c in cells), default=-1) + 1
        room = {"key": room_key, "label": label, "shape": shape,
                "cols": cols, "rows": rows, "cells": cells, "bins": []}
        # Bin strip lives in the barrel room: any bin-type vessel currently
        # holding a lot, drawn as a square, hidden when empty.
        if room_key == Vessel.Room.NEW_BARREL:
            bin_vessels = (Vessel.objects
                           .filter(type__in=_BIN_TYPES, id__in=current.keys())
                           .order_by("code"))
            room["bins"] = [_vessel_cell(v, "square", current.get(v.id))
                            for v in bin_vessels]
        rooms.append(room)
    return rooms
