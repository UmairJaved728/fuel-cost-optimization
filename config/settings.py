import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def env_bool(key: str, default: bool = False) -> bool:
    value = os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, default))
    except (TypeError, ValueError):
        return default


def env_decimal(key: str, default: str) -> str:
    return os.getenv(key, default)


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "change-me-change-me-change-me")

DEBUG = env_bool("DJANGO_DEBUG", default=False)

ALLOWED_HOSTS = [host for host in os.getenv("DJANGO_ALLOWED_HOSTS", "*").split(",") if host]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.gis",
    "rest_framework",
    "drf_spectacular",
    "routes",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ---------------------------------------------------------------------------
# Database (PostgreSQL + PostGIS)
# ---------------------------------------------------------------------------
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres@localhost:3000/fuel_route_optimizer",
)

import dj_database_url  # noqa: E402

DATABASES = {
    "default": dj_database_url.parse(DATABASE_URL, conn_max_age=60),
}
DATABASES["default"]["ENGINE"] = "django.contrib.gis.db.backends.postgis"
DATABASES["default"].setdefault("CONN_MAX_AGE", 60)
DATABASES["default"].update(dj_database_url.parse(DATABASE_URL).get("TEST", {}))

# NOTE: no explicit TEST["NAME"] is set here.  Django therefore creates a
# dedicated "test_fuel_route_optimizer" database for the pytest suite, so the
# real "fuel_route_optimizer" database is never dropped or polluted.

# ---------------------------------------------------------------------------
# GDAL / GEOS libraries for GeoDjango on Windows (PostGIS bundle ships them
# inside the PostgreSQL bin directory).
# ---------------------------------------------------------------------------
_POSTGIS_BIN = Path(os.getenv("POSTGIS_BIN_PATH", r"C:\Program Files\PostgreSQL\16\bin"))

_gdal_candidates = [
    os.getenv("GDAL_LIBRARY_PATH"),
    str(_POSTGIS_BIN / "libgdal-35.dll"),
    str(_POSTGIS_BIN / "gdal.dll"),
]
for _gdal in _gdal_candidates:
    if _gdal and Path(_gdal).is_file():
        GDAL_LIBRARY_PATH = _gdal
        break
else:
    GDAL_LIBRARY_PATH = None

_geos_candidates = [
    os.getenv("GEOS_LIBRARY_PATH"),
    str(_POSTGIS_BIN / "libgeos_c.dll"),
    str(_POSTGIS_BIN / "geos_c.dll"),
]
for _geos in _geos_candidates:
    if _geos and Path(_geos).is_file():
        GEOS_LIBRARY_PATH = _geos
        break
else:
    GEOS_LIBRARY_PATH = None

if GDAL_LIBRARY_PATH and GEOS_LIBRARY_PATH:
    os.environ["GDAL_LIBRARY_PATH"] = GDAL_LIBRARY_PATH
    os.environ["GEOS_LIBRARY_PATH"] = GEOS_LIBRARY_PATH

# ---------------------------------------------------------------------------
# Cache (PostgreSQL-backed, no Redis)
# ---------------------------------------------------------------------------
CACHE_TTL_SECONDS = env_int("CACHE_TTL_SECONDS", 86400)

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": os.getenv("DJANGO_CACHE_TABLE", "django_cache"),
        "TIMEOUT": CACHE_TTL_SECONDS,
        "OPTIONS": {"MAX_ENTRIES": 5000},
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.getenv("DJANGO_TIME_ZONE", "America/New_York")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Application configuration
# ---------------------------------------------------------------------------
ROUTE_CORRIDOR_MILES = env_int("ROUTE_CORRIDOR_MILES", 10)
FUEL_MPG = env_int("FUEL_MPG", 10)
MAX_RANGE_MILES = env_int("MAX_RANGE_MILES", 500)

TANK_CAPACITY_GALLONS = env_decimal("TANK_CAPACITY_GALLONS", "50")
STARTING_FUEL_GALLONS = env_decimal("STARTING_FUEL_GALLONS", "50")

ROUTING_CLIENT_TIMEOUT_SECONDS = env_int("ROUTING_CLIENT_TIMEOUT_SECONDS", 15)
ROUTING_CLIENT_RETRIES = env_int("ROUTING_CLIENT_RETRIES", 1)

GEOCODING_TIMEOUT_SECONDS = env_int("GEOCODING_TIMEOUT_SECONDS", 10)
GEOCODING_RETRIES = env_int("GEOCODING_RETRIES", 1)

OSRM_BASE_URL = os.getenv("OSRM_BASE_URL", "https://router.project-osrm.org")
GEOCODER_BASE_URL = os.getenv(
    "GEOCODER_BASE_URL", "https://nominatim.openstreetmap.org"
)
GEOCODER_USER_AGENT = os.getenv(
    "GEOCODER_USER_AGENT", "fuel-route-optimizer/1.0 (local demo)"
)

# ---------------------------------------------------------------------------
# REST Framework / OpenAPI
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "routes.exceptions.api_exception_handler",
    "UNAUTHENTICATED_USER": None,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Fuel Route Optimizer API",
    "DESCRIPTION": (
        "Given a start and finish within the USA, returns the fastest OSRM "
        "driving route and a mathematically optimal fixed-route fuel purchase "
        "plan built from nearby truck-stop fuel prices."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "loggers": {
        "routes.services": {
            "handlers": ["console"],
            "level": os.getenv("DJANGO_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
        "routes": {
            "handlers": ["console"],
            "level": os.getenv("DJANGO_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
    },
}