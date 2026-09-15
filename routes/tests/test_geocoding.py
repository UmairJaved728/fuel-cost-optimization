"""Geocoding client tests.  All provider communication is mocked."""

from unittest import mock

import pytest
import requests

from routes.exceptions import GeocodingFailedError
from routes.services.geocoding_client import GeocodingClient, GeocodingResult

US_FEATURE = {
    "lat": "41.8781",
    "lon": "-87.6298",
    "display_name": "Chicago, Cook County, Illinois, United States",
    "address": {"country_code": "us"},
}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, json="ok"):
        self.status_code = status_code
        self._payload = payload
        self._json = json

    def json(self):
        if self._json not in ("ok",):
            raise ValueError(self._json)
        return self._payload


def _patch_session(**kwargs):
    return mock.patch("requests.Session.get", return_value=FakeResponse(**kwargs))


class TestGeocodingClient:
    def test_successful_response(self):
        with _patch_session(payload=[US_FEATURE]):
            result = GeocodingClient().geocode("Chicago, IL")
        assert isinstance(result, GeocodingResult)
        assert result.latitude == 41.8781
        assert result.longitude == -87.6298
        assert result.country_code == "us"
        assert GeocodingClient.is_us_result(result)

    def test_request_uses_usa_constraint(self):
        with mock.patch("requests.Session.get", return_value=FakeResponse(payload=[US_FEATURE])) as get:
            GeocodingClient().geocode("Chicago, IL")
        url, kwargs = get.call_args
        assert kwargs["params"]["countrycodes"] == "us"
        assert kwargs["params"]["limit"] == 1

    def test_no_result(self):
        with _patch_session(payload=[]):
            with pytest.raises(GeocodingFailedError):
                GeocodingClient().geocode("Atlantis")

    def test_non_usa_result(self):
        feature = {
            "lat": "45.5",
            "lon": "-73.5",
            "display_name": "Montreal, Quebec, Canada",
            "address": {"country_code": "ca"},
        }
        with _patch_session(payload=[feature]):
            result = GeocodingClient().geocode("Montreal")
        assert GeocodingClient.is_us_result(result) is False

    def test_timeout(self):
        with mock.patch("requests.Session.get", side_effect=requests.Timeout("timeout")):
            with pytest.raises(GeocodingFailedError):
                GeocodingClient().geocode("Chicago, IL")

    def test_http_failure(self):
        with _patch_session(status_code=403, payload={}):
            with pytest.raises(GeocodingFailedError):
                GeocodingClient().geocode("Chicago, IL")

    def test_malformed_provider_response(self):
        with _patch_session(payload={}, json="not json"):
            with pytest.raises(GeocodingFailedError):
                GeocodingClient().geocode("Chicago, IL")

    def test_feature_missing_fields(self):
        with _patch_session(payload=[{"lat": "not-a-number", "lon": "3"}]):
            with pytest.raises(GeocodingFailedError):
                GeocodingClient().geocode("Chicago, IL")

    def test_connection_error(self):
        with mock.patch("requests.Session.get", side_effect=requests.ConnectionError("down")):
            with pytest.raises(GeocodingFailedError):
                GeocodingClient().geocode("Chicago, IL")