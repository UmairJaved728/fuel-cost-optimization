"""Cache handling for external service responses.

Routes and geocoding lookups are cached in Django's PostgreSQL-backed cache to
avoid repeating external HTTP calls.  Route cache keys incorporate normalized
start/finish coordinates and the routing profile.
"""

from __future__ import annotations

import hashlib
import json
import logging

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

_DEFAULT_PROFILE = "driving"


def _normalize(value: float) -> str:
    # 6 decimal places (~0.1 m) is far more precise than the geocoded input.
    return f"{float(value):.6f}"


def build_route_cache_key(
    start_lat: float,
    start_lon: float,
    finish_lat: float,
    finish_lon: float,
    profile: str = _DEFAULT_PROFILE,
) -> str:
    """Build a deterministic route cache key.

    The key depends on start/finish coordinates, the routing profile, and the
    configured corridor (a corridor change invalidates cached candidate sets).
    """
    payload = {
        "profile": profile,
        "start": (_normalize(start_lat), _normalize(start_lon)),
        "finish": (_normalize(finish_lat), _normalize(finish_lon)),
        "corridor_miles": settings.ROUTE_CORRIDOR_MILES,
        "fuel_mpg": settings.FUEL_MPG,
        "max_range_miles": settings.MAX_RANGE_MILES,
    }
    key = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"route:{profile}:{key}"


def get_route(key: str) -> dict | None:
    value = cache.get(key)
    if value:
        logger.info("route cache hit key=%s", key)
    return value


def set_route(key: str, route_data: dict) -> None:
    cache.set(key, route_data, timeout=settings.CACHE_TTL_SECONDS)


def build_geocode_cache_key(address: str) -> str:
    normalized = " ".join(address.strip().lower().split())
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"geocode:{digest}"


def get_geocode(address: str) -> dict | None:
    key = build_geocode_cache_key(address)
    value = cache.get(key)
    if value:
        logger.info("geocode cache hit address=%r key=%s", address, key)
    return value


def set_geocode(address: str, result: dict) -> None:
    key = build_geocode_cache_key(address)
    cache.set(key, result, timeout=settings.CACHE_TTL_SECONDS)