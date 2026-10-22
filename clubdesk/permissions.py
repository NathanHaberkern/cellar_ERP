"""Club desk permissions and the starter group.

Same pattern as shipping.permissions: declared on ClubRequest.Meta, named only
here. Approving is separate from viewing on purpose: Nate approves every change
that is written to Commerce7, so only he holds APPROVE_CHANGES to start with.
"""
from django.db import DEFAULT_DB_ALIAS

APP = "clubdesk"

VIEW_REQUESTS = "view_club_requests"
APPROVE_CHANGES = "approve_club_changes"

CUSTOM_PERMISSIONS = [
    (VIEW_REQUESTS, "Can see club requests and proposed changes"),
    (APPROVE_CHANGES, "Can approve and apply club changes to Commerce7"),
]


def perm(codename):
    return f"{APP}.{codename}"


DEFAULT_GROUPS = {
    "Club Desk": [VIEW_REQUESTS],
}


def ensure_default_groups(sender=None, using=DEFAULT_DB_ALIAS, **kwargs):
    from django.contrib.auth.models import Group, Permission

    for name, codenames in DEFAULT_GROUPS.items():
        group, created = Group.objects.using(using).get_or_create(name=name)
        if not created:
            continue
        group.permissions.set(Permission.objects.using(using).filter(
            content_type__app_label=APP, codename__in=codenames))
