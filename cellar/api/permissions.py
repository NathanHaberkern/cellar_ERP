"""
Permissions for the Cellar API.

Compliance and cost data is append-only at the model layer (AppendOnly mixin),
but the API adds a second belt-and-suspenders guard: the ledger/event surfaces
are read + create only, never update/delete over HTTP. Corrections happen the
same way they do in admin -- void + re-add -- so we never expose PATCH/PUT/DELETE
on those resources at all (see ReadCreateOnlyViewSet in views.py).

With 2 trusted in-house staff, everyone is effectively an operator. These classes
exist so the seam is ready the day a role split is wanted (e.g. a tasting-room
account that can read lots but not touch the ledger) without reworking views.
"""

from rest_framework.permissions import BasePermission, SAFE_METHODS


class IsStaff(BasePermission):
    """Authenticated AND is_staff. Both current users are staff; this is the
    default gate for write-capable reference tables."""

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_staff)


class ReadOnlyOrStaff(BasePermission):
    """Any authenticated user may read; only staff may write. Handy later if a
    limited read-only account is added."""

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        if request.method in SAFE_METHODS:
            return True
        return bool(request.user.is_staff)
