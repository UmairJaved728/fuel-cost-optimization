"""Route-level orchestration.

Responsibilities (specification section 41):

1. resolve location inputs (coordinates or geocoded address)
2. validate USA membership
3. consult the route cache
4. call OSRM only when the route is not cached
5. validate the route response
6. normalize distance/duration/geometry
7. invoke the station service
8. invoke the fuel optimizer
9. construct the final API response data
"""

from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from django.conf import settings

from routes.exceptions import (
    InvalidLocationError,
    InvalidRequestError,
    LocationOutsideUSAError,
)
from routes.services import cache_service
from routes.services.fuel_optimizer import NoFeasiblePlan, optimize
from routes.services.geocoding_client import GeocodingClient, GeocodingResult
from routes.services.routing_client import METERS_TO_MILES, RouteInfo, RoutingClient
from routes.services.station_service import find_candidate_stations, geojson_to_line
from routes.services.usa_boundary import is_within_usa

logger = logging.getLogger(__name__)


class RouteService:
    """Builds a full route + fuel plan for a client request."""

    def __init__(self) -> None:
        self._routing_client = RoutingClient()
        self._geocoding_client = GeocodingClient()

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #
    def plan(self, start: dict, finish: dict) -> dict[str, Any]:
        start_coords = self._resolve_location(start, "start")
        finish_coords = self._resolve_location(finish, "finish")

        self._validate_not_identical(start_coords, finish_coords)

        route_info = self._get_route_info(
            start_lat=start_coords.latitude,
            start_lon=start_coords.longitude,
            finish_lat=finish_coords.latitude,
            finish_lon=finish_coords.longitude,
        )

        distance_miles = route_info.distance_meters / METERS_TO_MILES
        duration_minutes = route_info.duration_seconds / 60.0

        route_line = geojson_to_line(route_info.geometry)

        candidates = find_candidate_stations(
            route_line=route_line,
            corridor_miles=settings.ROUTE_CORRIDOR_MILES,
            route_distance_miles=distance_miles,
        )

        try:
            optimization = optimize(
                stations=candidates,
                destination_position_miles=distance_miles,
                starting_fuel_gallons=settings.STARTING_FUEL_GALLONS,
                tank_capacity_gallons=settings.TANK_CAPACITY_GALLONS,
                mpg=settings.FUEL_MPG,
            )
        except NoFeasiblePlan as exc:
            # Maps NoFeasiblePlan -> HTTP 422 by the API handler
            from routes.exceptions import NoFeasibleFuelPlanError

            raise NoFeasibleFuelPlanError(str(exc)) from exc

        response = self._build_response(
            route_info=route_info,
            distance_miles=distance_miles,
            duration_minutes=duration_minutes,
            optimization=optimization,
        )
        logger.info(
            "plan complete stops=%d total_fuel_cost=%s",
            len(optimization.stops),
            optimization.total_fuel_cost,
        )
        return response

    # ------------------------------------------------------------------ #
    # Location resolution
    # ------------------------------------------------------------------ #
    def _resolve_location(self, location: dict, name: str) -> GeocodingResult:
        if "address" in location:
            address = location.get("address")
            if not isinstance(address, str) or not address.strip():
                raise InvalidLocationError(f"{name} address must be a non-empty string.")
            address = address.strip()
            return self._geocode_with_cache(address)

        latitude = location.get("latitude")
        longitude = location.get("longitude")
        if latitude is None or longitude is None:
            raise InvalidLocationError(
                f"{name} must contain either an address or latitude/longitude."
            )

        try:
            lat = float(latitude)
            lon = float(longitude)
        except (TypeError, ValueError) as exc:
            raise InvalidLocationError(
                f"{name} coordinate values must be numeric."
            ) from exc

        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
            raise InvalidLocationError(
                f"{name} coordinates are out of range."
            )

        if not is_within_usa(lat, lon):
            raise LocationOutsideUSAError(
                f"{name} is outside the USA."
            )

        return GeocodingResult(
            latitude=lat,
            longitude=lon,
            display_name=f"{lat},{lon}",
            country_code="us",
        )

    def _geocode_with_cache(self, address: str) -> GeocodingResult:
        cached = cache_service.get_geocode(address)
        if cached:
            return GeocodingResult(
                latitude=cached["latitude"],
                longitude=cached["longitude"],
                display_name=cached.get("display_name", address),
                country_code=cached.get("country_code", "us"),
            )

        result = self._geocoding_client.geocode(address)
        if not GeocodingClient.is_us_result(result):
            raise LocationOutsideUSAError(
                f"The address {address!r} does not resolve to a location in the USA."
            )
        if not is_within_usa(result.latitude, result.longitude):
            raise LocationOutsideUSAError(
                f"The address {address!r} does not resolve to a location in the USA."
            )

        cache_service.set_geocode(
            address,
            {
                "latitude": result.latitude,
                "longitude": result.longitude,
                "display_name": result.display_name,
                "country_code": result.country_code,
            },
        )
        return result

    @staticmethod
    def _validate_not_identical(
        start: GeocodingResult, finish: GeocodingResult
    ) -> None:
        if (
            abs(start.latitude - finish.latitude) < 0.00001
            and abs(start.longitude - finish.longitude) < 0.00001
        ):
            raise InvalidRequestError("Start and finish cannot be identical.")

    # ------------------------------------------------------------------ #
    # Route retrieval with caching
    # ------------------------------------------------------------------ #
    def _get_route_info(
        self,
        start_lat: float,
        start_lon: float,
        finish_lat: float,
        finish_lon: float,
    ) -> RouteInfo:
        key = cache_service.build_route_cache_key(
            start_lat=start_lat,
            start_lon=start_lon,
            finish_lat=finish_lat,
            finish_lon=finish_lon,
        )
        cached = cache_service.get_route(key)
        if cached:
            return RouteInfo(
                distance_meters=cached["distance_meters"],
                duration_seconds=cached["duration_seconds"],
                geometry=cached["geometry"],
            )

        logger.info("route cache miss key=%s", key)
        info = self._routing_client.get_route(start_lat, start_lon, finish_lat, finish_lon)
        cache_service.set_route(
            key,
            {
                "distance_meters": info.distance_meters,
                "duration_seconds": info.duration_seconds,
                "geometry": info.geometry,
            },
        )
        return info

    # ------------------------------------------------------------------ #
    # Response construction
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_response(
        route_info: RouteInfo,
        distance_miles: float,
        duration_minutes: float,
        optimization,
    ) -> dict[str, Any]:
        vehicle = {
            "mpg": settings.FUEL_MPG,
            "max_range_miles": settings.MAX_RANGE_MILES,
            "tank_capacity_gallons": Decimal(
                str(settings.TANK_CAPACITY_GALLONS)
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            "starting_fuel_gallons": Decimal(
                str(settings.STARTING_FUEL_GALLONS)
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        }

        fuel_stops = []
        for stop in optimization.stops:
            fuel_stops.append(
                {
                    "stop_number": stop.stop_number,
                    "station": stop.station,
                    "route_position_miles": _round(stop.route_position_miles, 2),
                    "distance_from_previous_stop_miles": _round(
                        stop.distance_from_previous_stop_miles, 2
                    ),
                    "retail_price_per_gallon": _round(stop.retail_price_per_gallon, 3),
                    "fuel_before_stop_gallons": _round(stop.fuel_before_stop_gallons, 2),
                    "fuel_purchased_gallons": _round(stop.fuel_purchased_gallons, 2),
                    "fuel_after_stop_gallons": _round(stop.fuel_after_stop_gallons, 2),
                    "estimated_cost": _round(stop.estimated_cost, 2),
                }
            )

        return {
            "route": {
                "distance_miles": round(distance_miles, 2),
                "duration_minutes": round(duration_minutes, 2),
                "geometry": route_info.geometry,
            },
            "vehicle": vehicle,
            "fuel_plan": {
                "total_fuel_purchased_gallons": _round(
                    optimization.total_fuel_purchased_gallons, 2
                ),
                "total_fuel_cost": _round(optimization.total_fuel_cost, 2),
                "currency": "USD",
            },
            "fuel_stops": fuel_stops,
        }


def _round(value, places: int):
    quantum = Decimal("1").scaleb(-places)
    return Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)