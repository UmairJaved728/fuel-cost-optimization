"""Station service tests.

These need a real PostgreSQL/PostGIS test database (pytest-django creates it),
because the corridor filter runs inside PostGIS using the spatial index.
"""

import pytest
from django.contrib.gis.geos import LineString, Point

from routes.models import Station
from routes.services.station_service import (
    _deduplicate_candidates,
    find_candidate_stations,
    geojson_to_line,
)
from conftest import dense_line

pytestmark = pytest.mark.django_db


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


# A straight-line "route" of roughly 440 miles: (-105, 40) -> (-95, 34).
# Midpoint is (-100, 37); every 0.1 degree of latitude ~ 6.9 mi.
ROUTE = dense_line([(-105.0, 40.0), (-95.0, 34.0)])
ROUTE_DISTANCE_MILES = 440.0


class TestGeojsonToLine:
    def test_converts_valid_geometry(self):
        geometry = {
            "type": "LineString",
            "coordinates": [[-105, 40], [-95, 34]],
        }
        line = geojson_to_line(geometry)
        assert isinstance(line, LineString)
        assert line.srid == 4326
        assert len(line.coords) == 2

    def test_rejects_non_linestring(self):
        with pytest.raises(ValueError):
            geojson_to_line({"type": "Point", "coordinates": [1, 2]})

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            geojson_to_line(None)
        with pytest.raises(ValueError):
            geojson_to_line({"type": "LineString", "coordinates": [[-105, 40]]})


class TestFindCandidateStations:
    def test_station_on_route_is_included(self):
        _make_station(1, "OnRoute", 37.0, -100.0)
        candidates = find_candidate_stations(ROUTE, 10.0, ROUTE_DISTANCE_MILES)
        assert len(candidates) == 1
        assert candidates[0]["station"]["name"] == "OnRoute"
        assert candidates[0]["station"]["latitude"] == 37.0

    def test_station_inside_corridor_is_included(self):
        # ~5 miles perpendicular to the midpoint -> inside the 10-mile corridor.
        _make_station(2, "Near", 37.1, -100.0)
        candidates = find_candidate_stations(ROUTE, 10.0, ROUTE_DISTANCE_MILES)
        assert any(c["station"]["name"] == "Near" for c in candidates)

    def test_station_outside_corridor_is_excluded(self):
        _make_station(3, "Far", 38.0, -100.0)  # ~69 miles from the route.
        candidates = find_candidate_stations(ROUTE, 10.0, ROUTE_DISTANCE_MILES)
        assert all(c["station"]["name"] != "Far" for c in candidates)

    def test_route_position_before_start_projects_to_start(self):
        _make_station(4, "Before", 42.0, -105.0)  # 2 degrees north of start.
        candidates = find_candidate_stations(ROUTE, 10.0, ROUTE_DISTANCE_MILES)
        assert all(c["station"]["name"] != "Before" for c in candidates)  # outside corridor, actually

    def test_position_ordering_matches_route_order_not_straight_line(self):
        # A hairpin route: start at A(-110,40), down to B(-105,30), back up
        # almost to the origin at C(-112,39).  A station near the end (Late)
        # is straight-line CLOSER to the origin than a station on the outbound
        # leg (Early), but it is FARTHER along the route.  Ordering by route
        # position must yield Early first; ordering by straight-line distance
        # from the origin would wrongly yield Late first.
        loopy = dense_line([(-110.0, 40.0), (-105.0, 30.0), (-112.0, 39.0)])
        _make_station(10, "Early", 35.0, -107.5)   # on segment A->B
        _make_station(11, "Late", 38.1, -111.3)    # on segment B->C, near the end
        candidates = find_candidate_stations(loopy, 10.0, 1000.0)
        names = [c["station"]["name"] for c in candidates]
        assert names == ["Early", "Late"]
        positions = [c["route_position_miles"] for c in candidates]
        assert positions[0] < positions[1]


class TestDeduplicateCandidates:
    def test_removes_obvious_duplicates(self):
        candidates = [
            {"route_position_miles": 100.0, "price_per_gallon": "4.00", "station": {"name": "A"}},
            {"route_position_miles": 100.02, "price_per_gallon": "3.50", "station": {"name": "B"}},
            {"route_position_miles": 150.0, "price_per_gallon": "3.00", "station": {"name": "C"}},
        ]
        deduped = _deduplicate_candidates(candidates)
        assert len(deduped) == 2
        assert deduped[0]["station"]["name"] == "B"  # cheaper of the duplicate pair
        assert deduped[1]["station"]["name"] == "C"

    def test_empty_list(self):
        assert _deduplicate_candidates([]) == []