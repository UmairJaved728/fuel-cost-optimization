"""Cache key generation and cache-service behaviour."""

import pytest

from routes.services import cache_service

pytestmark = pytest.mark.django_db


class TestRouteCacheKey:
    def test_same_request_same_key(self):
        k1 = cache_service.build_route_cache_key(41.8781, -87.6298, 40.7128, -74.0060)
        k2 = cache_service.build_route_cache_key(41.8781, -87.6298, 40.7128, -74.0060)
        assert k1 == k2

    def test_coordinate_normalization(self):
        k1 = cache_service.build_route_cache_key(41.878100, -87.629800, 40.7128, -74.0060)
        k2 = cache_service.build_route_cache_key(41.8781, -87.6298, 40.7128, -74.0060)
        assert k1 == k2

    def test_different_start(self):
        k1 = cache_service.build_route_cache_key(41.8781, -87.6298, 40.7128, -74.0060)
        k2 = cache_service.build_route_cache_key(42.3601, -71.0589, 40.7128, -74.0060)
        assert k1 != k2

    def test_different_finish(self):
        k1 = cache_service.build_route_cache_key(41.8781, -87.6298, 40.7128, -74.0060)
        k2 = cache_service.build_route_cache_key(41.8781, -87.6298, 34.0522, -118.2437)
        assert k1 != k2

    def test_profile_included(self):
        k1 = cache_service.build_route_cache_key(41.8781, -87.6298, 40.7128, -74.0060, "driving")
        k2 = cache_service.build_route_cache_key(41.8781, -87.6298, 40.7128, -74.0060, "biking")
        assert k1 != k2


class TestRouteCacheStore:
    def test_set_then_get(self):
        key = cache_service.build_route_cache_key(33.0, -100.0, 34.0, -101.0)
        cache_service.set_route(key, {"distance_meters": 400000})
        assert cache_service.get_route(key) == {"distance_meters": 400000}

    def test_missing_key_returns_none(self):
        assert cache_service.get_route("route:driving:nonexistent") is None


class TestGeocodeCache:
    def test_geocode_cache_round_trip(self):
        address = "  Chicago, IL  "
        result = {
            "latitude": 41.8781,
            "longitude": -87.6298,
            "display_name": "Chicago, IL, USA",
            "country_code": "us",
        }
        cache_service.set_geocode(address, result)
        cached = cache_service.get_geocode("Chicago, IL")
        assert cached == result

    def test_geocode_cache_miss(self):
        assert cache_service.get_geocode("not-cached-address-zzz") is None