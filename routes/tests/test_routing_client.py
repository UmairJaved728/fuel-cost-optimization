"""Routing client tests.  All OSRM calls are mocked; nothing leaves the box."""

from unittest import mock

import pytest
import requests

from routes.exceptions import NoRouteFoundError, RoutingServiceError
from routes.services.routing_client import RoutingClient

OSRM_OK_PAYLOAD = {
    "code": "Ok",
    "routes": [
        {
            "distance": 1275213.4,
            "duration": 79520.3,
            "geometry": {
                "type": "LineString",
                "coordinates": [[-87.6, 41.9], [-74.0, 40.7]],
            },
        }
    ],
}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, json=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self._json = json
        self.text = text

    def json(self):
        if self._json is not None:
            raise ValueError(self._json)
        return self._payload


def _patch_session(**kwargs):
    return mock.patch("requests.Session.get", return_value=FakeResponse(**kwargs))


class TestRoutingClient:
    def test_constructs_expected_request(self):
        # The URL must use the driving profile and pass the required params.
        with mock.patch("requests.Session.get", return_value=FakeResponse(payload=OSRM_OK_PAYLOAD)) as get:
            RoutingClient().get_route(41.8781, -87.6298, 40.7128, -74.0060)

        assert get.call_count == 1
        url, call_kwargs = get.call_args
        assert url[0].startswith("https://router.project-osrm.org/route/v1/driving/")
        assert "-87.6298,41.8781;-74.006,40.7128" in url[0]
        params = call_kwargs["params"]
        assert params["alternatives"] == "false"
        assert params["steps"] == "false"
        assert params["geometries"] == "geojson"
        assert params["overview"] == "full"
        assert call_kwargs.get("timeout") is not None

    def test_parses_successful_response(self):
        with _patch_session(payload=OSRM_OK_PAYLOAD):
            route = RoutingClient().get_route(41.8781, -87.6298, 40.7128, -74.0060)
        assert route.distance_meters == 1275213.4
        assert route.duration_seconds == 79520.3
        assert route.geometry["type"] == "LineString"
        assert route.geometry["coordinates"][0] == [-87.6, 41.9]

    def test_non_200_status_raises_routing_error(self):
        with _patch_session(status_code=500, payload={}):
            with pytest.raises(RoutingServiceError):
                RoutingClient().get_route(41.8781, -87.6298, 40.7128, -74.0060)

    def test_no_route_raises_controlled_error(self):
        payload = {"code": "NoRoute", "message": "No route"}
        with _patch_session(payload=payload):
            with pytest.raises(NoRouteFoundError):
                RoutingClient().get_route(41.8781, -87.6298, 40.7128, -74.0060)

    def test_empty_routes_raises_controlled_error(self):
        payload = {"code": "Ok", "routes": []}
        with _patch_session(payload=payload):
            with pytest.raises(NoRouteFoundError):
                RoutingClient().get_route(41.8781, -87.6298, 40.7128, -74.0060)

    def test_malformed_json_raises_routing_error(self):
        with _patch_session(payload={}, json="not json"):
            with pytest.raises(RoutingServiceError):
                RoutingClient().get_route(41.8781, -87.6298, 40.7128, -74.0060)

    def test_non_linestring_geometry_raises(self):
        payload = {"code": "Ok", "routes": [{"distance": 1, "duration": 1, "geometry": {"type": "Point", "coordinates": [1, 2]}}]}
        with _patch_session(payload=payload):
            with pytest.raises(RoutingServiceError):
                RoutingClient().get_route(41.8781, -87.6298, 40.7128, -74.0060)

    def test_connection_error_raises_routing_error(self):
        with mock.patch("requests.Session.get", side_effect=requests.Timeout("timed out")):
            with pytest.raises(RoutingServiceError):
                RoutingClient().get_route(41.8781, -87.6298, 40.7128, -74.0060)