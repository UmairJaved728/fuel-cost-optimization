"""Spatial station retrieval and route-position projection.

Responsibilities (see specification section 42):

1. Convert OSRM GeoJSON into a usable PostGIS route line.
2. Spatially search stations near the route using the PostGIS index.
3. Calculate each candidate station's position along the route
   (ST_LineLocatePoint over the route geometry).
4. Sort candidates by route position.
5. Remove obvious duplicate candidates.
6. Return clean station objects for the fuel optimizer.

No fuel-purchase business rules live here.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from django.contrib.gis.db.models import GeometryField
from django.contrib.gis.db.models.functions import LineLocatePoint
from django.contrib.gis.geos import LineString
from django.db.models import Value
from django.db.models.functions import Cast

from routes.models import Station

logger = logging.getLogger(__name__)

METERS_TO_MILES = 1609.344

# Two stations whose projected route positions differ by less than this many
# miles are considered the "same" candidate for planning purposes.
_DUPLICATE_POSITION_EPSILON_MILES = 0.05


def geojson_to_line(geometry: dict[str, Any]) -> LineString:
    """Convert an OSRM GeoJSON LineString into a GEOS LineString (SRID 4326)."""
    if not geometry or geometry.get("type") != "LineString":
        raise ValueError("route geometry must be a GeoJSON LineString")
    coordinates = geometry.get("coordinates")
    if not coordinates or len(coordinates) < 2:
        raise ValueError("route geometry must contain at least two coordinates")
    return LineString(coordinates, srid=4326)


def find_candidate_stations(
    route_line: LineString,
    corridor_miles: float | int,
    route_distance_miles: float,
) -> list[dict[str, Any]]:
    """Return candidate stations inside the route corridor, sorted and deduped.

    The corridor filter runs entirely in PostgreSQL using the PostGIS spatial
    index (``__distance_lte`` on a geography column expands to ``ST_DWithin``
    in meters).  Each returned item is a dict suitable for the fuel optimizer.

    Raises ``RuntimeError`` if the route line is degenerate (undefined length).
    """
    corridor_meters = corridor_miles * METERS_TO_MILES
    route_geometry_expr = Value(route_line, output_field=GeometryField(srid=4326))
    point_geometry_expr = Cast("location", output_field=GeometryField(srid=4326))

    queryset = (
        Station.objects.filter(location__isnull=False)
        .filter(location__distance_lte=(route_line, corridor_meters))
        .annotate(
            route_fraction=LineLocatePoint(route_geometry_expr, point_geometry_expr),
        )
    )

    candidates: list[dict[str, Any]] = []
    for station in queryset.iterator(chunk_size=500):
        fraction = float(station.route_fraction)
        if fraction < 0 or fraction > 1:
            logger.warning(
                "station id=%s produced invalid route fraction %s; skipping",
                station.id,
                fraction,
            )
            continue
        position_miles = fraction * route_distance_miles
        candidates.append(
            {
                "route_position_miles": position_miles,
                "price_per_gallon": station.retail_price,
                "station": {
                    "id": station.id,
                    "name": station.name,
                    "address": station.address,
                    "city": station.city,
                    "state": station.state,
                    "latitude": station.latitude,
                    "longitude": station.longitude,
                },
            }
        )

    logger.info("station candidates found=%d for corridor=%s mi", len(candidates), corridor_miles)

    candidates.sort(key=lambda c: (c["route_position_miles"], c["price_per_gallon"]))
    deduped = _deduplicate_candidates(candidates)
    return deduped


def _deduplicate_candidates(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop obvious duplicate candidates.

    Candidates whose projected route positions are essentially identical
    represent the same planning decision; the cheaper one dominates, so the
    more expensive entry is removed.  This never removes genuinely different
    stations (they project to different positions).
    """
    if not candidates:
        return []

    deduped: list[dict[str, Any]] = [candidates[0]]
    for candidate in candidates[1:]:
        last = deduped[-1]
        if candidate["route_position_miles"] - last["route_position_miles"] <= (
            _DUPLICATE_POSITION_EPSILON_MILES
        ):
            # Keep the cheaper candidate.
            if candidate["price_per_gallon"] < last["price_per_gallon"]:
                deduped[-1] = candidate
            continue
        deduped.append(candidate)
    return deduped