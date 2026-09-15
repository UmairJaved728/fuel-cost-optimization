"""Route service orchestration tests.

The OSRM and Nominatim clients are mocked so no external network is touched.
Stations live in the PostGIS test database.
"""

from decimal import Decimal
from unittest import mock

import pytest
from django.conf import settings
from django.contrib.gis.geos import Point

from routes.exceptions import (
    InvalidLocationError,
    InvalidRequestError,
    LocationOutsideUSAError,
    NoFeasibleFuelPlanError,
)
from routes.models import Station
from routes.services.route_service import RouteService
from conftest import dense_line

pytestmark = pytest.mark.django_db


# Synthetic OSRM result for an ~220 mile straight line from (-105, 40) to (-95, 34).
ROUTE_DISTANCE_METERS = 220.0 * 1609.344
ROUTE_GEOMETRY = {
    "type": "LineString",
    "coordinates": dense_line([(-105.0, 40.0), (-95.0, 34.0)]).coords,
}
ROUTE_INFO = {
    "distance_meters": ROUTE_DISTANCE_METERS,
    "duration_seconds": 12345.0,
    "geometry": ROUTE_GEOMETRY,
}

CHICAGO = {"latitude": 41.8781, "longitude": -87.6298, "country_code": "us"}

# A ~600 mile straight line: exceeds the 500-mile tank range, so refuelling is
# mandatory and a station is required for a feasible plan.
LONG_ROUTE_DISTANCE_METERS = 600.0 * 1609.344
LONG_ROUTE_INFO = {
    "distance_meters": LONG_ROUTE_DISTANCE_METERS,
    "duration_seconds": 30000.0,
    "geometry": {
        "type": "LineString",
        "coordinates": dense_line([(-105.0, 40.0), (-95.0, 28.0)]).coords,
    },
}


def _fake_routing_client(_self=None, route_info=None):
    info = route_info or ROUTE_INFO

    def get_route(*args, **kwargs):
        return mock.MagicMock(
            distance_meters=info["distance_meters"],
            duration_seconds=info["duration_seconds"],
            geometry=info["geometry"],
        )

    return mock.MagicMock(get_route=mock.MagicMock(side_effect=get_route))


def _fake_geocoding_client(*_args, **_kwargs):
    def geocode(address):
        if "chicago" in address.lower():
            return _Result(41.8781, -87.6298, "Chicago, IL, USA", "us")
        if "montreal" in address.lower():
            return _Result(45.5, -73.5, "Montreal, QC, Canada", "ca")
        raise LocationOutsideUSAError("no")

    return mock.MagicMock(geocode=mock.MagicMock(side_effect=geocode))


class _Result:
    def __init__(self, lat, lon, name, country_code):
        self.latitude = lat
        self.longitude = lon
        self.display_name = name
        self.country_code = country_code


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


@pytest.fixture
def service():
    svc = RouteService()
    svc._routing_client = _fake_routing_client()
    svc._geocoding_client = _fake_geocoding_client()
    return svc


class TestPlanCoordinates:
    def _plan_long(self, svc, stations=None):
        # Long route: the tank alone is not enough, refuelling is mandatory.
        svc._routing_client = _fake_routing_client(route_info=LONG_ROUTE_INFO)
        return svc.plan(
            start={"latitude": 40.0, "longitude": -105.0},
            finish={"latitude": 29.76, "longitude": -95.37},
        )

    def test_plan_with_no_stations_raises_no_feasible(self, service):
        with pytest.raises(NoFeasibleFuelPlanError):
            self._plan_long(service)

    def test_plan_with_station_on_route(self, service):
        # Station near the midpoint (~300 mi).  Arrive with 20 gal, need 30 for
        # the destination -> buy 10 gal at the station.
        _make_station(1, "Mid", 34.0, -100.0)
        result = self._plan_long(service)
        assert result["route"]["distance_miles"] == round(
            LONG_ROUTE_DISTANCE_METERS / 1609.344, 2
        )
        assert result["vehicle"]["mpg"] == settings.FUEL_MPG
        assert result["fuel_plan"]["currency"] == "USD"
        assert len(result["fuel_stops"]) == 1
        stop = result["fuel_stops"][0]
        assert stop["station"]["name"] == "Mid"
        assert Decimal(stop["fuel_purchased_gallons"]) > 0

    def test_route_info_uses_cache_on_second_call(self, service):
        svc2 = RouteService()
        svc2._routing_client = _fake_routing_client()
        svc2._geocoding_client = _fake_geocoding_client()

        start = {"latitude": 41.8781, "longitude": -87.6298}
        finish = {"latitude": 40.7128, "longitude": -74.0060}
        svc2.plan(start=start, finish=finish)
        svc2.plan(start=start, finish=finish)
        assert svc2._routing_client.get_route.call_count == 1


class TestPlanAddresses:
    def test_address_geocoded_and_usable(self, service):
        _make_station(1, "Mid", 37.0, -100.0)
        result = service.plan(
            start={"address": "Chicago, IL"},
            finish={"latitude": 34.0, "longitude": -95.37},
        )
        assert isinstance(result["fuel_plan"]["total_fuel_cost"], Decimal)

    def test_geocode_result_outside_usa_rejected(self, service):
        with pytest.raises(LocationOutsideUSAError):
            service.plan(
                start={"address": "Montreal, QC"},
                finish={"latitude": 34.0, "longitude": -95.37},
            )

    def test_empty_address_rejected(self, service):
        with pytest.raises(InvalidLocationError):
            service._resolve_location({"address": "   "}, "start")


class TestValidation:
    def test_missing_both_forms_rejected(self, service):
        service._geocoding_client = None
        with pytest.raises(InvalidLocationError):
            service._resolve_location({}, "start")

    def test_identical_points_rejected(self, service):
        with pytest.raises(InvalidRequestError):
            service.plan(
                start={"latitude": 40.0, "longitude": -104.0},
                finish={"latitude": 40.0, "longitude": -104.0},
            )

    def test_non_numeric_coordinate_rejected(self, service):
        with pytest.raises(InvalidLocationError):
            service._resolve_location({"latitude": "abc", "longitude": 40.0}, "start")

    def test_out_of_range_coordinate_rejected(self, service):
        with pytest.raises(InvalidLocationError):
            service._resolve_location({"latitude": 120.0, "longitude": 40.0}, "start")

    def test_coordinate_outside_usa_rejected(self, service):
        # (0, 0) is in the Gulf of Guinea, definitely not the USA.
        with pytest.raises(LocationOutsideUSAError):
            service._resolve_location({"latitude": 0.0, "longitude": 0.0}, "start")


class TestResponseShape:
    def test_response_structure(self, service):
        _make_station(1, "Mid", 34.0, -100.0)
        service._routing_client = _fake_routing_client(route_info=LONG_ROUTE_INFO)
        result = service.plan(
            start={"latitude": 40.0, "longitude": -105.0},
            finish={"latitude": 29.76, "longitude": -95.37},
        )
        keys = set(result.keys())
        assert {"route", "vehicle", "fuel_plan", "fuel_stops"} <= keys
        assert set(result["route"].keys()) == {
            "distance_miles",
            "duration_minutes",
            "geometry",
        }
        assert set(result["vehicle"].keys()) == {
            "mpg",
            "max_range_miles",
            "tank_capacity_gallons",
            "starting_fuel_gallons",
        }
        assert set(result["fuel_plan"].keys()) == {
            "total_fuel_purchased_gallons",
            "total_fuel_cost",
            "currency",
        }