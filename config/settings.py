"""
Django settings — St. Amant Cellar ERP.

12-factor / environment-driven: runs locally on SQLite with DEBUG, and on Heroku on
Postgres in production, with no code change — only environment variables differ.
Secrets and host config come from the environment; nothing sensitive is committed.
"""
import os
from pathlib import Path

import dj_database_url

try:
    from dotenv import load_dotenv          # load a local .env in development
    load_dotenv()
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_list(name):
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


# --- core -------------------------------------------------------------------
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-key-change-in-production")
DEBUG = os.environ.get("DEBUG", "False").lower() in ("1", "true", "yes", "on")

ALLOWED_HOSTS = _env_list("ALLOWED_HOSTS") + [".herokuapp.com"]
if DEBUG:
    ALLOWED_HOSTS += ["localhost", "127.0.0.1"]

CSRF_TRUSTED_ORIGINS = _env_list("CSRF_TRUSTED_ORIGINS") + ["https://*.herokuapp.com"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",          # API layer
    "rest_framework.authtoken",    # <-- ADD: token auth for iOS / off-origin clients
    "corsheaders",             # so the web / iOS clients can call the API
    "cellar.apps.CellarConfig",
    "cellar.web.apps.CellarWebConfig",   # <-- ADD: HTMX front end
]


MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",       # serve static on Heroku
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]

# --- database ---------------------------------------------------------------
# Heroku injects DATABASE_URL; locally we fall back to SQLite.
DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,                              # respects Essential-0's 20-conn cap
        ssl_require=bool(os.environ.get("DATABASE_URL")),
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "America/Los_Angeles"
USE_I18N = True
LANGUAGE_CODE = "en-us"

# --- static (WhiteNoise) ----------------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# --- API + CORS -------------------------------------------------------------
REST_FRAMEWORK = {
"DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.TokenAuthentication",   # <-- add (iOS/off-origin)
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": (                               # <-- add
        ["rest_framework.renderers.JSONRenderer",
         "rest_framework.renderers.BrowsableAPIRenderer"] if DEBUG
        else ["rest_framework.renderers.JSONRenderer"]
    ),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 50,
}
CORS_ALLOWED_ORIGINS = _env_list("CORS_ALLOWED_ORIGINS")
CORS_ALLOW_CREDENTIALS = True

# --- security (production only) --------------------------------------------
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")   # Heroku terminates TLS
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 3600
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True

# --- logging (Heroku captures stdout) --------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
}

# --- web front end + TTB forms header ---------------------------------------
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"
FORMS_TEMPLATE_DIR = BASE_DIR / "cellar" / "forms_templates"
WINERY = {
    "EIN": os.environ.get("WINERY_EIN", "94-2275571"),
    "REGISTRY": os.environ.get("WINERY_REGISTRY", "BW-CA-5526"),
    "NAME_ADDRESS": os.environ.get(
        "WINERY_NAME_ADDRESS", "St. Amant Winery, 1 Winemaster Way, Lodi, CA 95240"),
}

