"""
Self-hosted hybrid authentication for the Cellar API.

Design (locked with Nate, 2 in-house users, nobody external):
    - WEB clients (browser / future React or HTMX SPA on the same origin) use
      Django's SESSION auth. The session cookie is HttpOnly + SameSite, CSRF is
      enforced on unsafe methods. Nothing to store client-side; logout is a
      server round-trip that kills the session.
    - NATIVE / OFF-ORIGIN clients (future iOS app, a PC hitting the API directly)
      use DRF TOKEN auth. The client obtains a token once at /auth/token/ and
      sends `Authorization: Token <key>` thereafter. Tokens are exempt from CSRF.

Both authenticators are registered globally in settings (SessionAuthentication
first, TokenAuthentication second), so every endpoint accepts either transport
with no per-view work. This module only adds the login/logout/identity plumbing
that DRF doesn't ship out of the box.

Why built-in DRF authtoken and not Auth0/Knox/JWT:
    - 2 users, no external parties -> an identity provider is overkill and adds a
      dependency + monthly cost + an outside party holding auth for compliance data.
    - authtoken is one migration, zero new packages beyond DRF (already installed),
      and you own 100% of it. Upgrade path if the user base ever grows past staff:
      swap TokenAuthentication for django-rest-knox (hashed tokens, per-device
      logout, expiry) -- it's a drop-in change here, the rest of the API is unaffected.
"""

from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.views.decorators.csrf import ensure_csrf_cookie
from django.utils.decorators import method_decorator

from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.authtoken.views import ObtainAuthToken
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView


def _user_payload(user: User) -> dict:
    """Shape of the identity object every auth endpoint returns."""
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "is_staff": user.is_staff,
        "is_superuser": user.is_superuser,
    }


@method_decorator(ensure_csrf_cookie, name="get")
class CsrfView(APIView):
    """
    GET /api/auth/csrf/

    Web clients call this once on load. It sets the `csrftoken` cookie so the
    SPA can echo it back in the X-CSRFToken header on session-authenticated
    writes. Token clients never need this.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"detail": "CSRF cookie set."})


class SessionLoginView(APIView):
    """
    POST /api/auth/login/   {"username": ..., "password": ...}

    Establishes a Django session (web transport). Returns the identity object.
    Rejects inactive users. Deliberately generic error text (no user enumeration).
    """

    permission_classes = [AllowAny]

    def post(self, request):
        username = (request.data.get("username") or "").strip()
        password = request.data.get("password") or ""
        if not username or not password:
            return Response(
                {"detail": "Username and password are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        user = authenticate(request, username=username, password=password)
        if user is None:
            return Response(
                {"detail": "Invalid credentials."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        login(request, user)  # rotates session key, sets cookie
        return Response(_user_payload(user))


class SessionLogoutView(APIView):
    """POST /api/auth/logout/ — ends the session (web transport)."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        logout(request)
        return Response({"detail": "Logged out."})


class ObtainTokenView(ObtainAuthToken):
    """
    POST /api/auth/token/   {"username": ..., "password": ...}

    Native/off-origin transport. Returns a long-lived token plus the identity
    object. get_or_create means re-calling returns the SAME token (idempotent),
    so an iOS app that lost its stored key can re-fetch without orphaning tokens.
    """

    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        serializer = self.serializer_class(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        token, _ = Token.objects.get_or_create(user=user)
        return Response({"token": token.key, **_user_payload(user)})


class RevokeTokenView(APIView):
    """
    POST /api/auth/token/logout/

    Deletes the caller's token (native "sign out on this device"). Works only
    when called with a token; a fresh token is minted on the next /auth/token/.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        Token.objects.filter(user=request.user).delete()
        return Response({"detail": "Token revoked."})


class WhoAmIView(APIView):
    """GET /api/auth/whoami/ — identity of whoever the request authenticated as."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(_user_payload(request.user))
