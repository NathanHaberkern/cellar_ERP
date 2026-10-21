"""Users page: staff logins and what each one is allowed to do.

ERP-wide (not shipping-specific), so it lives in the web layer. Access needs
Django's own `auth.change_user` permission -- superusers have it implicitly,
so in practice this is Nate unless he grants it to someone.

Two ways to grant access, both shown on the edit form:
  * groups  -- roles like Packer / Shipping Admin (the normal way)
  * direct  -- a single shipping permission for one person, as an exception

Superuser status is deliberately NOT editable here. It's an all-access key;
promoting someone is rare enough to do in /admin/, where it's harder to click
by accident.
"""
from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.auth.forms import SetPasswordMixin
from django.contrib.auth.models import Group, Permission
from django.db.models import Case, IntegerField, Value, When
from django.shortcuts import get_object_or_404, redirect, render

from shipping.permissions import APP as SHIPPING_APP, CUSTOM_PERMISSIONS

User = get_user_model()
MANAGE = "auth.change_user"


def _shipping_perms():
    order = {code: i for i, (code, _) in enumerate(CUSTOM_PERMISSIONS)}
    perms = Permission.objects.filter(content_type__app_label=SHIPPING_APP,
                                      codename__in=order)
    return sorted(perms, key=lambda p: order[p.codename])


class _AccessFields(forms.Form):
    groups = forms.ModelMultipleChoiceField(
        queryset=Group.objects.order_by("name"), required=False,
        widget=forms.CheckboxSelectMultiple, label="Roles")
    direct_perms = forms.ModelMultipleChoiceField(
        queryset=Permission.objects.none(), required=False,
        widget=forms.CheckboxSelectMultiple, label="Extra shipping permissions",
        help_text="Only for exceptions — normally grant access through a role.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        ordered = _shipping_perms()
        self.fields["direct_perms"].queryset = Permission.objects.filter(
            pk__in=[p.pk for p in ordered]).order_by(
            Case(*[When(pk=p.pk, then=Value(i)) for i, p in enumerate(ordered)],
                 output_field=IntegerField()))
        self.fields["direct_perms"].label_from_instance = lambda p: p.name


class UserCreateForm(SetPasswordMixin, _AccessFields):
    username = forms.CharField(max_length=150)
    first_name = forms.CharField(max_length=150, required=False)
    last_name = forms.CharField(max_length=150, required=False)
    email = forms.EmailField(required=False, widget=forms.TextInput)
    password1, password2 = SetPasswordMixin.create_password_fields()

    field_order = ["username", "first_name", "last_name", "email", "password1", "password2",
                   "groups", "direct_perms"]

    def clean_username(self):
        name = self.cleaned_data["username"].strip()
        if User.objects.filter(username__iexact=name).exists():
            raise forms.ValidationError("That username is taken.")
        return name

    def clean(self):
        cleaned = super().clean()
        self.validate_passwords()
        probe = User(username=cleaned.get("username", ""), email=cleaned.get("email", ""),
                     first_name=cleaned.get("first_name", ""),
                     last_name=cleaned.get("last_name", ""))
        self.validate_password_for_user(probe)
        return cleaned


class UserEditForm(SetPasswordMixin, _AccessFields):
    first_name = forms.CharField(max_length=150, required=False)
    last_name = forms.CharField(max_length=150, required=False)
    email = forms.EmailField(required=False, widget=forms.TextInput)
    is_active = forms.BooleanField(required=False, label="Active (can sign in)")
    password1, password2 = SetPasswordMixin.create_password_fields(
        label1="New password (leave blank to keep)", label2="Confirm new password")

    field_order = ["first_name", "last_name", "email", "is_active", "groups", "direct_perms",
                   "password1", "password2"]

    def __init__(self, *args, target=None, editing_self=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.target = target
        self.fields["password1"].required = False
        self.fields["password2"].required = False
        if editing_self:
            # You can't lock yourself out from this page.
            self.fields["is_active"].disabled = True

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("password1") or cleaned.get("password2"):
            if not (cleaned.get("password1") and cleaned.get("password2")):
                self.add_error("password2", "Enter the new password twice.")
            else:
                self.validate_passwords()
                self.validate_password_for_user(self.target)
        return cleaned


def _apply_access(user, form):
    user.groups.set(form.cleaned_data["groups"])
    keep = user.user_permissions.exclude(content_type__app_label=SHIPPING_APP)
    user.user_permissions.set(list(keep) + list(form.cleaned_data["direct_perms"]))


@login_required
@permission_required(MANAGE, raise_exception=True)
def users_list(request):
    users = User.objects.order_by("-is_active", "username").prefetch_related("groups")
    return render(request, "web/users.html", {"nav": "users", "users": users,
                                              "groups": Group.objects.order_by("name")})


@login_required
@permission_required(MANAGE, raise_exception=True)
def user_create(request):
    form = UserCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data
        user = User.objects.create_user(username=cd["username"], email=cd["email"],
                                        password=cd["password1"], first_name=cd["first_name"],
                                        last_name=cd["last_name"])
        _apply_access(user, form)
        messages.success(request, f"Created {user.username}.")
        return redirect("users")
    return render(request, "web/user_form.html", {"nav": "users", "form": form, "creating": True})


@login_required
@permission_required(MANAGE, raise_exception=True)
def user_edit(request, pk):
    target = get_object_or_404(User, pk=pk)
    editing_self = target.pk == request.user.pk
    initial = {"first_name": target.first_name, "last_name": target.last_name,
               "email": target.email, "is_active": target.is_active,
               "groups": target.groups.all(),
               "direct_perms": target.user_permissions.filter(
                   content_type__app_label=SHIPPING_APP)}
    form = UserEditForm(request.POST or None, initial=initial, target=target,
                        editing_self=editing_self)
    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data
        target.first_name, target.last_name, target.email = (
            cd["first_name"], cd["last_name"], cd["email"])
        if not editing_self:
            target.is_active = cd["is_active"]
        if cd.get("password1"):
            target.set_password(cd["password1"])
        target.save()
        _apply_access(target, form)
        messages.success(request, f"Saved {target.username}.")
        return redirect("users")
    return render(request, "web/user_form.html", {
        "nav": "users", "form": form, "creating": False, "target": target,
        "editing_self": editing_self})
