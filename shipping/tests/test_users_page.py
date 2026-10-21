"""Users page: who can see it, creating logins, assigning roles and permissions."""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import TestCase, override_settings
from django.urls import reverse

User = get_user_model()
STRONG = "Cork-and-Barrel-2026"


# Same pins as cellar's web tests: the test client speaks plain http (production
# settings would 301 it), and plain static storage avoids needing collectstatic.
@override_settings(
    SECURE_SSL_REDIRECT=False,
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class UsersPageTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser("nate", "nate@example.com", STRONG)
        self.staff = User.objects.create_user("laurie", password=STRONG)

    def test_plain_staff_cannot_open_users(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("users")).status_code, 403)
        self.assertNotContains(self.client.get(reverse("dashboard")), 'href="/users/"')

    def test_owner_sees_list_and_nav_link(self):
        self.client.force_login(self.owner)
        resp = self.client.get(reverse("users"))
        self.assertContains(resp, "laurie")
        self.assertContains(resp, 'href="/users/"')

    def test_create_user_with_role(self):
        self.client.force_login(self.owner)
        packer = Group.objects.get(name="Packer")
        resp = self.client.post(reverse("user-create"), {
            "username": "jill", "first_name": "Jill", "password1": STRONG,
            "password2": STRONG, "groups": [packer.pk]})
        self.assertRedirects(resp, reverse("users"))
        jill = User.objects.get(username="jill")
        self.assertTrue(jill.check_password(STRONG))
        self.assertTrue(jill.has_perm("shipping.print_labels"))
        self.assertFalse(jill.has_perm("shipping.void_labels"))

    def test_weak_password_rejected(self):
        self.client.force_login(self.owner)
        resp = self.client.post(reverse("user-create"), {
            "username": "jake", "password1": "wine", "password2": "wine"})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(username="jake").exists())

    def test_duplicate_username_rejected(self):
        self.client.force_login(self.owner)
        resp = self.client.post(reverse("user-create"), {
            "username": "Laurie", "password1": STRONG, "password2": STRONG})
        self.assertContains(resp, "taken")

    def test_grant_direct_permission_and_deactivate(self):
        self.client.force_login(self.owner)
        void = Permission.objects.get(content_type__app_label="shipping", codename="void_labels")
        resp = self.client.post(reverse("user-edit", args=[self.staff.pk]), {
            "first_name": "Laurie", "is_active": "", "direct_perms": [void.pk]})
        self.assertRedirects(resp, reverse("users"))
        self.staff.refresh_from_db()
        self.assertFalse(self.staff.is_active)
        self.assertTrue(self.staff.user_permissions.filter(pk=void.pk).exists())

    def test_cannot_deactivate_self(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("user-edit", args=[self.owner.pk]),
                         {"first_name": "Nate", "is_active": ""})
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_active)

    def test_password_change_is_optional_but_validated(self):
        self.client.force_login(self.owner)
        url = reverse("user-edit", args=[self.staff.pk])
        self.client.post(url, {"is_active": "on"})
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.check_password(STRONG))
        resp = self.client.post(url, {"is_active": "on", "password1": "x"})
        self.assertEqual(resp.status_code, 200)       # second box missing -> error, no save
        new = "Port-Club-Volume-27"
        self.client.post(url, {"is_active": "on", "password1": new, "password2": new})
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.check_password(new))
