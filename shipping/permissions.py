"""Shipping permissions and the two starter groups.

Permissions are declared on ShipOrder.Meta (Django needs a model to hang custom
permissions on); this module is the single place that names them, so views and
templates never spell a codename by hand.

Groups are a starting point, not policy. They are created only if missing; once
a group exists its permissions belong to Nate (edit them on the Users page or in
/admin/auth/group/) and a later migrate never resets them.
"""
from django.db import DEFAULT_DB_ALIAS

APP = "shipping"

SYNC_ORDERS = "sync_orders"
BUILD_POOLS = "build_pools"
PRINT_LABELS = "print_labels"
VOID_LABELS = "void_labels"
MANAGE_SETTINGS = "manage_shipping_settings"

# (codename, human label) -- order is the order shown on the Users page.
CUSTOM_PERMISSIONS = [
    (SYNC_ORDERS, "Can sync orders from Commerce7"),
    (BUILD_POOLS, "Can build pack pools and boxes"),
    (PRINT_LABELS, "Can buy and print shipping labels"),
    (VOID_LABELS, "Can void shipping labels"),
    (MANAGE_SETTINGS, "Can manage box sizes, bottle weights and shipping settings"),
]


def perm(codename):
    """Full 'app.codename' string for user.has_perm()."""
    return f"{APP}.{codename}"


DEFAULT_GROUPS = {
    "Packer": [SYNC_ORDERS, BUILD_POOLS, PRINT_LABELS],
    "Shipping Admin": [SYNC_ORDERS, BUILD_POOLS, PRINT_LABELS, VOID_LABELS, MANAGE_SETTINGS],
}


def ensure_default_groups(sender=None, using=DEFAULT_DB_ALIAS, **kwargs):
    """post_migrate hook: create the starter groups if they don't exist yet."""
    from django.contrib.auth.models import Group, Permission

    for name, codenames in DEFAULT_GROUPS.items():
        group, created = Group.objects.using(using).get_or_create(name=name)
        if not created:
            continue
        perms = Permission.objects.using(using).filter(
            content_type__app_label=APP, codename__in=codenames)
        group.permissions.set(perms)
