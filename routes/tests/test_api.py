"""HTTP API tests (DRF).

The external OSRM/Nominatim clients are mocked so the whole test suite stays
offline.  These tests verify the wire contract: HTTP status codes, the unified
error envelope, the OpenAPI schema endpoint, and full request/response shape.
"""

from decimal import Decimal
from unittest import mock

import pytest
from django.contrib.gis.geos import Point
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from routes.exceptions import (
    GeocodingFailedError,
    NoFeasibleFuelPlanError,
    RoutingServiceError,
)
from routes.models import Station
from conftest import dense_line

pytestmark = pytest.mark.django_db

PLAN_URL = reverse("route-plan")


def _make_station(opis_id, name, lat, lon, price="3.50"):
    return Station.objects.create(
        opis_id=opis_id,
        name=name,
        address=f"{name} Rd",
        city="Test",
        state="OK",
        retail_price=price,
        latitude=lat,
        longitude=lon,
        location=Point(lon, lat, srid=4326),
    )


# A ~600 mile straight line (-105,40) -> (-95,28): refuelling is mandatory.
LONG_ROUTE_INFO = {
    "distance_meters": 600.0 * 1609.344,
    "duration_seconds": 30000.0,
    "geometry": {
        "type": "LineString",
        "coordinates": dense_line([(-105.0, 40.0), (-95.0, 28.0)]).coords,
    },
}


def _fake_routing(route_info=LONG_ROUTE_INFO):
    client = mock.MagicMock()
    client.get_route.return_value = mock.MagicMock(
        distance_meters=route_info["distance_meters"],
        duration_seconds=route_info["duration_seconds"],
        geometry=route_info["geometry"],
    )
    return client


def _fake_geocoding():
    client = mock.MagicMock()
    client.geocode.return_value = mock.MagicMock(
        latitude=41.8781,
        longitude=-87.6298,
        display_name="Chicago, IL, USA",
        country_code="us",
    )
    return client


@pytest.fixture
def client():
    return APIClient()


class TestValidationStatusCodes:
    def test_missing_start(self, client):
        resp = client.post(PLAN_URL, {"finish": {"latitude": 29.76, "longitude": -95.37}}, format="json")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.data["error"]["code"] == "INVALID_REQUEST"

    def test_invalid_coordinates(self, client):
        resp = client.post(
            PLAN_URL,
            {
                "start": {"latitude": 200.0, "longitude": -105.0},
                "finish": {"latitude": 29.76, "longitude": -95.37},
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.data["error"]["code"] == "INVALID_REQUEST"

    def test_address_and_coordinates_rejected(self, client):
        resp = client.post(
            PLAN_URL,
            {
                "start": {"address": "Chicago", "latitude": 1.0, "longitude": 2.0},
                "finish": {"latitude": 29.76, "longitude": -95.37},
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_stray_unknown_field_ignored(self, client):
        with mock.patch("routes.services.route_service.RoutingClient", return_value=_fake_routing()):
            resp = client.post(
                PLAN_URL,
                {
                    "start": {"latitude": 40.0, "longitude": -105.0, "extra": 1},
                    "finish": {"latitude": 29.76, "longitude": -95.37},
                },
                format="json",
            )
        # "extra" is an unexpected field; DRF ignores it and the request is
        # otherwise well formed, so this is not a 400.
        assert resp.status_code in (status.HTTP_200_OK, status.HTTP_422_UNPROCESSABLE_ENTITY)


class TestHappyPath:
    def test_full_plan_response(self, client):
        _make_station(1, "Mid", 34.0, -100.0)
        with mock.patch("routes.services.route_service.RoutingClient", return_value=_fake_routing()):
            resp = client.post(
                PLAN_URL,
                {
                    "start": {"latitude": 40.0, "longitude": -105.0},
                    "finish": {"latitude": 29.76, "longitude": -95.37},
                },
                format="json",
            )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.data
        assert set(data.keys()) == {"route", "vehicle", "fuel_plan", "fuel_stops"}
        assert data["route"]["geometry"]["type"] == "LineString"
        assert data["route"]["distance_miles"] == round(600.0, 2)
        assert data["fuel_plan"]["currency"] == "USD"
        assert Decimal(data["fuel_plan"]["total_fuel_cost"]) > 0
        assert len(data["fuel_stops"]) >= 1
        stop = data["fuel_stops"][0]
        assert stop["station"]["name"] == "Mid"
        assert set(stop.keys()) == {
            "stop_number",
            "station",
            "route_position_miles",
            "distance_from_previous_stop_miles",
            "retail_price_per_gallon",
            "fuel_before_stop_gallons",
            "fuel_purchased_gallons",
            "fuel_after_stop_gallons",
            "estimated_cost",
        }


class TestErrorMapping:
    def test_no_feasible_fuel_plan(self, client):
        with mock.patch("routes.views.RouteService.plan", side_effect=NoFeasibleFuelPlanError("no fuel")):
            resp = client.post(
                PLAN_URL,
                {
                    "start": {"latitude": 40.0, "longitude": -105.0},
                    "finish": {"latitude": 29.76, "longitude": -95.37},
                },
                format="json",
            )
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert resp.data["error"]["code"] == "NO_FEASIBLE_FUEL_PLAN"

    def test_routing_service_error(self, client):
        with mock.patch("routes.views.RouteService.plan", side_effect=RoutingServiceError("osrm down")):
            resp = client.post(
                PLAN_URL,
                {
                    "start": {"latitude": 40.0, "longitude": -105.0},
                    "finish": {"latitude": 29.76, "longitude": -95.37},
                },
                format="json",
            )
        assert resp.status_code == status.HTTP_502_BAD_GATEWAY
        assert resp.data["error"]["code"] == "ROUTING_SERVICE_ERROR"

    def test_geocoding_error(self, client):
        with mock.patch("routes.views.RouteService.plan", side_effect=GeocodingFailedError("nope")):
            resp = client.post(
                PLAN_URL,
                {
                    "start": {"address": "Atlantis"},
                    "finish": {"latitude": 29.76, "longitude": -95.37},
                },
                format="json",
            )
        assert resp.status_code == status.HTTP_502_BAD_GATEWAY
        assert resp.data["error"]["code"] == "GEOCODING_FAILED"

    def test_error_envelope_shape(self, client):
        with mock.patch("routes.views.RouteService.plan", side_effect=GeocodingFailedError("gone")):
            resp = client.post(
                PLAN_URL,
                {
                    "start": {"address": "Atlantis"},
                    "finish": {"latitude": 29.76, "longitude": -95.37},
                },
                format="json",
            )
        assert set(resp.data.keys()) == {"error"}
        assert set(resp.data["error"].keys()) == {"code", "message"}


class TestDocsEndpoints:
    def test_openapi_schema_available(self, client):
        resp = client.get("/api/schema/")
        assert resp.status_code == status.HTTP_200_OK
        assert resp.data["info"]["title"]
        assert "/api/v1/routes/plan/" in resp.data["paths"]

    def test_swagger_ui_available(self, client):
        resp = client.get("/api/docs/")
        assert resp.status_code == status.HTTP_200_OK

    def test_demo_page_available(self, client):
        resp = client.get("/demo/")
        assert resp.status_code == status.HTTP_200_OK


class TestVerbMethods:
    def test_get_not_allowed(self, client):
        resp = client.get(PLAN_URL)
        assert resp.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

    def test_empty_body_rejected(self, client):
        resp = client.post(PLAN_URL, {}, format="json")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_non_json_content_type_rejected(self, client):
        resp = client.post(PLAN_URL, "start=chicago", content_type="text/plain")
        assert resp.status_code == status.HTTP_415_UNSUPPORTED_MEDIA_TYPE or resp.status_code == status.HTTP_400_BAD_REQUEST