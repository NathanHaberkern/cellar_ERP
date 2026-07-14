"""
Glycol setpoint tasks.

Decision table (Nate, Jul 2026) — applies only to vessels with
`Vessel.temp_controlled = True`; nothing fires for an uncontrolled tank:

    Cold soak (all wines)          -> 58F
    Settling (whites)               -> 40F
    Post-press (settling done,
      ready to ferment)             -> 72F, standard for all fermentation activity
    Any other time in tank          -> 72F

Each trigger both (a) writes `Vessel.glycol_setpoint_f` — the ERP's record of
what the dial SHOULD read, so it can be displayed on the tank map / vessel
reference — and (b) opens a Task so a human actually turns the physical dial.
Nothing here reads or controls real hardware.
"""
from decimal import Decimal

from django.utils import timezone

from cellar.services import tasks as tsvc

COLD_SOAK_F = Decimal("58")
SETTLING_F = Decimal("40")
STANDARD_F = Decimal("72")

# True white paths (destemmed or whole-cluster). Rosé (B, C) and reds (D, E)
# are handled by their own branches; this constant only decides the settling
# case. Kept local (string codes) so this module doesn't need to import
# DestemmingEvent just for two letters.
WHITE_PATHS = {"A", "F"}


def _write_setpoint(vessel, target_f):
    vessel.glycol_setpoint_f = target_f
    vessel.save(update_fields=["glycol_setpoint_f"])


def _task(vessel, lot, title, body, key, actor=None):
    return tsvc.create_task(
        title=title, body=body, due_date=timezone.localdate(), lot=lot, actor=actor,
        dedupe_key=f"{key}:{vessel.pk}:{lot.pk}:{timezone.localdate().isoformat()}")


def on_lot_created_into_tank(lot, vessel, path, *, actor=None):
    """Fires right after a fresh lot is assigned to a tank at intake.

    Reds go straight to cold soak (58F); true whites go to settling (40F);
    everything else (rosé) falls back to the standard 72F setpoint until a
    more specific trigger applies. No-op for a non-temp-controlled vessel.
    """
    if vessel is None or not vessel.temp_controlled:
        return None
    path = str(path)
    from cellar.services.operations import RED_PATHS
    if path in {str(p) for p in RED_PATHS}:
        target, label = COLD_SOAK_F, "cold soak"
    elif path in WHITE_PATHS:
        target, label = SETTLING_F, "settling"
    else:
        target, label = STANDARD_F, "standard"
    _write_setpoint(vessel, target)
    _task(vessel, lot,
          title=f"Set glycol — {vessel.code} to {target:g}\u00b0F ({label})",
          body=f"{lot.code} is now in {vessel.code}. Set the glycol setpoint to "
               f"{target:g}\u00b0F for {label}.",
          key="glycol", actor=actor)
    return target


def on_cold_settling_rack(lot, vessel, *, actor=None):
    """Fires when a white is racked/pressed into a tank specifically to cold-settle."""
    if vessel is None or not vessel.temp_controlled:
        return None
    _write_setpoint(vessel, SETTLING_F)
    _task(vessel, lot,
          title=f"Set glycol — {vessel.code} to {SETTLING_F:g}\u00b0F (cold settling)",
          body=f"{lot.code} was racked to {vessel.code} for cold settling. "
               f"Set the glycol setpoint to {SETTLING_F:g}\u00b0F.",
          key="glycol-settle", actor=actor)
    return SETTLING_F


def on_post_press_ferment_start(lot, vessel, *, actor=None):
    """Fires once settled juice is racked off gross lees, ready to inoculate:
    set the glycol to the standard 72F fermentation temperature."""
    if vessel is None or not vessel.temp_controlled:
        return None
    _write_setpoint(vessel, STANDARD_F)
    _task(vessel, lot,
          title=f"Set glycol — {vessel.code} to {STANDARD_F:g}\u00b0F (fermentation)",
          body=f"{lot.code} is ready to inoculate in {vessel.code}. Set the glycol "
               f"setpoint to {STANDARD_F:g}\u00b0F — standard for all fermentation activity.",
          key="glycol-ferment", actor=actor)
    return STANDARD_F
